from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core import pathplan, task_pool, zones

logger = logging.getLogger(__name__)

# 目标点容差：进入该半径即视为"已抵达"。
ARRIVAL_TOLERANCE = 3.0
# 多机共用一个目标点时的搜索队形半径：0 号机占目标点中心，其余分机按该半径
# 围绕目标点均匀排布，避免同一任务的多架无人机全程重叠成一点。
_SEPARATION_RADIUS = 22.0
# 最外圈（含第 0 号机）可容纳的分机数；超出后按半径逐圈外扩。
_FORMATION_SLOTS_PER_RING = 8

# 速度（每 tick 位移）按分区降级：无干扰全速，有干扰减速，难以通行明显减速，
# 危险区仅用于"退出"，因此最慢。
_DEFAULT_STEP = 4.8
_STEP_BY_ZONE = {
    zones.GREEN: 4.8,
    zones.YELLOW: 3.6,
    zones.RED: 2.4,
    zones.PURPLE: 2.0,
}
_PERSPECTIVE_STEP = 3.6

# 黄色区"在不影响任务的前提下靠近绿色"的偏转权重。
_GREEN_SEEK_WEIGHT = 0.55
# 红色区回退到黄/绿后的降速比。
_FALLBACK_SPEED_FACTOR = 0.7
# 停滞检测：连续这么多 tick 未能刷新"离目标最近距离"即判定卡死。
# 单看分区会在分区边界出现极限环（红->黄->红 反复），必须用进度而不是分区判定。
_STALL_TICKS_LIMIT = 18
# 判定"有进展"的最小距离改善量。
_PROGRESS_EPSILON = 0.6
# 红色区回退搜索窗口：range(-2, 2) 恰好覆盖 4x4 个区块。
_FALLBACK_WINDOW = 2
# 步进被紫色危险区域拦下时，先尝试重新规划的次数，之后沿边界侧滑。
_BLOCKED_REPLAN_LIMIT = 2

# 记录路段信号时的采样间隔（小于区块边长，保证每个经过的区块都被采集）。
SIGNAL_SEGMENT_STEP = pathplan.BLOCK_SIZE / 3.0
# 基站附近的区块视为已知信号，无需探测。
_BASE_KNOWN_RADIUS = 110.0

# 状态 -> 中文标签（监控页列表用）。
_STATUS_LABELS = {
    "idle": "空闲（基站）",
    "awaiting": "原地待命",
    "assigned": "已分配",
    "planning": "重新规划中",
    "moving": "执行中",
    "holding": "红色区等待",
    "avoiding": "本地避障中",
    "exiting": "退出危险区域",
    "blocked": "受阻",
    "returning": "返航中",
}

# 状态 -> "正在干嘛"的中文描述（监控页列表用）。
_STATUS_ACTIVITY = {
    "idle": "在基站待命",
    "awaiting": "原地待命，等待下一个任务",
    "assigned": "已接任务，准备起飞",
    "planning": "重新规划路线",
    "moving": "飞往任务点",
    "holding": "信号难以通行，暂停并搜索回退点",
    "avoiding": "本地避障，绕开阻塞区块",
    "exiting": "落在危险区，正在退出",
    "blocked": "被拦截，等待服务端重规划",
    "returning": "按任务要求返航",
}


def _unit(dx: float, dy: float) -> Tuple[float, float]:
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return 0.0, 0.0
    return dx / length, dy / length
def _direction_name(heading: Tuple[float, float]) -> str:
    if abs(heading[0]) > abs(heading[1]):
        return "right" if heading[0] > 0 else "left"
    return "up" if heading[1] > 0 else "down"


# 自然语言地点 → 任务点字母。支持 "E点"、"点E"、"任务点e"、"目标点E"。
_POINT_LETTER_RE = re.compile(r"([A-Za-z])\s*(?:点|区|地|位置)|(?:点|区|地|位置)\s*([A-Za-z])")

# AI 解析出的地点常直接是坐标字符串："(150, 60)"、"150,60"、"（-60，120）"。
# 若不识别，这些地点会被静默回退到默认目标点，任务就会跑到错误的位置。
_COORD_RE = re.compile(
    r"^[（(]?\s*(-?\d+(?:\.\d+)?)\s*[，,]\s*(-?\d+(?:\.\d+)?)\s*[)）]?$"
)


def _match_coordinates(text: str) -> Optional[Tuple[float, float]]:
    """解析 ``(150, 60)`` 这类坐标写法，识别不到返回 None。"""
    match = _COORD_RE.match(text.strip())
    if match is None:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except (TypeError, ValueError):
        return None


# 自然语言里指向基站本身的写法。
_BASE_ALIASES = ("基地", "总部", "起飞点", "基站", "base", "home", "origin")

# 沙盘有效范围（与热力图 625 格网格一致）。自然语言坐标需要夹取到该范围，
# 否则 (5000, 5000) 这类输入会让无人机永远飞不到目标。
WORLD_BOUND = 220.0


def _match_point_letter(text: str) -> Optional[str]:
    """从自然语言地点里抽出任务点字母（A-E），识别不到返回 None。"""
    for match in _POINT_LETTER_RE.finditer(text):
        letter = (match.group(1) or match.group(2) or "").upper()
        if letter:
            return letter
    stripped = text.strip().strip("点区地位置目标任务 ")
    if len(stripped) == 1 and stripped.isalpha():
        return stripped.upper()
    return None


@dataclass
class DroneState:
    drone_id: str
    x: float
    y: float
    status: str = "idle"
    signal_strength: float = 0.0
    task_id: Optional[str] = None
    child_id: Optional[str] = None
    execution_version: int = 0
    task_phase: str = "end"
    task_type: str = "recon"
    returning_to_base: bool = False
    # return_to_base：本机当前任务完成后是否返航；False 表示原地待命等下一个任务。
    # 与 returning_to_base（"正在返航"的瞬时状态）区分开。
    return_to_base: bool = True
    planned_route: List[Tuple[float, float]] = field(default_factory=list)

    # 线路规划状态
    route_index: int = 0
    task_start: Optional[Tuple[float, float]] = None
    task_end: Optional[Tuple[float, float]] = None
    task_priority: str = "normal"
    needs_replan: bool = False
    ai_route_active: bool = False
    blocked_streak: int = 0
    recent_moves: List[str] = field(default_factory=list)
    failed_moves: List[str] = field(default_factory=list)
    # 停滞检测（按"离当前航点最近距离"计算），仅在航点推进/重新绑定任务时清零，
    # 否则无人机会在分区边界反复"停滞-回退"，永远无法收敛。
    stall_ticks: int = 0
    best_distance: Optional[float] = None
    # 每段航程允许因停滞而重新规划的次数；用尽后降级为限速直行。
    leg_replans: int = 0
    # 降级通过标记：本地避障与重新规划都无法绕开时，允许按分区限速直行。
    red_bypass: bool = False

    # 观测状态：observed_zone 是自身所在区块的真实条件，zone 是（必要时借用的）集群信号条件
    zone: str = "green"
    observed_zone: str = "green"
    borrowed_signal: bool = False
    borrow_radius: float = 0.0
    prev_position: Optional[Tuple[float, float]] = None


class SignalField:
    """按规则约束和稳定伪随机权重生成四档信号场。

    基站与任务点周围是固定的满信号区；其他区块按到这些锚点的距离加权，
    再用区块级稳定随机值抽取信号档位。使用稳定值而不是 ``random``，保证
    同一坐标在不同 tick 中不会闪烁，也不会让路径规划结果每次都变化。
    """

    def __init__(
        self,
        base_station: Tuple[float, float],
        decay: float = 0.18,
        safe_radius: float = 35.0,
        task_safe_radius: float = 28.0,
        protected_points: Optional[List[Tuple[float, float]]] = None,
    ):
        self.base_station = base_station
        self.decay = decay
        self.safe_radius = safe_radius
        self.task_safe_radius = task_safe_radius
        self.protected_points = list(protected_points or [])

    def add_protected_point(self, point: Tuple[float, float]) -> None:
        """登记任务点，使其周围成为固定满信号区。"""
        if point not in self.protected_points:
            self.protected_points.append(point)

    @staticmethod
    def _stable_random(x: float, y: float) -> float:
        """根据区块坐标生成 [0, 1) 的稳定伪随机值。"""
        value = math.sin(x * 12.9898 + y * 78.233) * 43758.5453
        return value - math.floor(value)

    def _nearest_anchor(self, x: float, y: float) -> Tuple[float, float]:
        points = [self.base_station, *self.protected_points]
        return min(points, key=lambda point: math.hypot(x - point[0], y - point[1]))

    def _zone_by_weight(self, x: float, y: float) -> str:
        """按距离权重抽取信号档位，越靠近锚点越偏向绿色。"""
        anchor = self._nearest_anchor(x, y)
        distance = math.hypot(x - anchor[0], y - anchor[1])
        green_weight = max(0.18, 1.0 - distance / 260.0)
        yellow_weight = 0.40 + distance / 700.0
        red_weight = 0.18 + distance / 800.0
        purple_weight = 0.02 + distance / 1600.0
        total = green_weight + yellow_weight + red_weight + purple_weight
        block_x, block_y = pathplan.block_of(x, y)
        draw = self._stable_random(block_x, block_y) * total

        for weight, zone in (
            (green_weight, zones.GREEN),
            (yellow_weight, zones.YELLOW),
            (red_weight, zones.RED),
            (purple_weight, zones.PURPLE),
        ):
            draw -= weight
            if draw < 0.0:
                return zone
        return zones.PURPLE

    def strength_at(self, x: float, y: float) -> float:
        base_distance = math.hypot(x - self.base_station[0], y - self.base_station[1])
        if base_distance <= self.safe_radius:
            return 100.0

        for point in self.protected_points:
            if math.hypot(x - point[0], y - point[1]) <= self.task_safe_radius:
                return 100.0

        zone = self._zone_by_weight(x, y)
        ranges = {
            zones.GREEN: (80.0, 100.0),
            zones.YELLOW: (55.0, 79.9),
            zones.RED: (30.0, 54.9),
            zones.PURPLE: (0.0, 29.9),
        }
        low, high = ranges[zone]
        block_x, block_y = pathplan.block_of(x, y)
        random_part = self._stable_random(block_x + 17.0, block_y - 31.0)
        return low + random_part * (high - low)

    def zone_at(self, x: float, y: float) -> str:
        return zones.zone_of(self.strength_at(x, y))

    def region_color(self, signal_strength: float) -> str:
        return zones.color_for_strength(signal_strength)

    def local_observation(self, drone: DroneState) -> Dict[str, float | str]:
        return {
            "drone_id": drone.drone_id,
            "signal_strength": drone.signal_strength,
            "zone": drone.zone,
            "x": drone.x,
            "y": drone.y,
            "observed_zone": drone.observed_zone,
            "borrowed_signal": drone.borrowed_signal,
            "borrow_radius": drone.borrow_radius,
            "status": drone.status,
            "task_id": drone.task_id,
        }


class SimulationEngine:
    def __init__(self, base_station=(0.0, 0.0), drone_count: int = 3):
        self.base_station = base_station
        self.signal_field = SignalField(base_station)
        self.drones: Dict[str, DroneState] = {}
        self.explored_cells = set()
        self._known_unknown_cache: Optional[Dict[str, int]] = None
        self._cached_grid_bounds = None

        # 多机搜索队形编队表：任务号 -> 该任务的分机顺序（drone_id 列表）。
        # 序号 0 占目标点中心，其余按 _SEPARATION_RADIUS 环形分散，避免同任务多机重叠。
        self._formation: Dict[str, List[str]] = {}

        # 同任务共用的飞行道基准轴（任务号 -> (起点, 未偏移的目标点)）。同任务多机必须
        # 绕同一条轴排道，否则各机按自身"起→终"取法向，偏移方向随航向旋转而交叠。
        self._formation_axis: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]] = {}

        # 服务端本地热力图：只能由无人机经过的路段与 3×3 区块观测累积而成。
        self.observed_heatmap: Dict[Tuple[int, int], Dict[str, object]] = {}
        self.heatmap_version = 0
        self._last_zone_by_block: Dict[Tuple[int, int], str] = {}
        self.heatmap_samples = 0

        self.task_targets = {
            "task_default_1": (120, -120),
            "task_default_2": (-150, 80),
            "task_default_3": (170, 140),
            "task_default_4": (-200, -160),
            "task_A": (120, -120),
            "task_B": (-150, 80),
            "task_C": (170, 140),
            "task_D": (-200, -160),
            "task_E": (-40, 195),
        }
        for task_point in self.task_targets.values():
            self.signal_field.add_protected_point(task_point)
        self._seed_drones(drone_count)

    def _target_candidates(self) -> List[Tuple[float, float]]:
        return list(self.task_targets.values())

    def _indexed_candidate(self, key: str, *, offset: int) -> Optional[Tuple[float, float]]:
        suffix = key.rsplit("_", 1)[-1]
        try:
            index = int(suffix) - offset
        except (TypeError, ValueError):
            logger.warning("无法解析任务编号，回退到默认目标点: %s", key)
            return None
        candidates = self._target_candidates()
        if not candidates:
            return None
        return candidates[index % len(candidates)]

    def resolve_task_target(self, task_target: Optional[str | Tuple[float, float]]) -> Tuple[float, float]:
        if isinstance(task_target, tuple):
            return task_target
        if task_target is None or (isinstance(task_target, str) and not task_target.strip()):
            logger.warning("任务缺少有效目标点，回退到默认目标: %r", task_target)
            candidates = self._target_candidates()
            return candidates[0] if candidates else self.base_station

        key = str(task_target).strip()

        if key in self.task_targets:
            return self.task_targets[key]

        normalized = key.lower()
        for canonical_key, coord in self.task_targets.items():
            if canonical_key.lower() == normalized:
                return coord

        # 自然语言坐标："(150, 60)" → 直接定位到该坐标（夹取到沙盘范围内）。
        coordinates = _match_coordinates(key)
        if coordinates is not None:
            return (
                max(-WORLD_BOUND, min(WORLD_BOUND, coordinates[0])),
                max(-WORLD_BOUND, min(WORLD_BOUND, coordinates[1])),
            )

        # 自然语言里的地点写法："E点"、"点E"、"任务点e" → 归一到 task_*。
        letter = _match_point_letter(key)
        if letter is not None:
            canonical = f"task_{letter}"
            if canonical in self.task_targets:
                return self.task_targets[canonical]

        # "基地/总部/起飞点" 等写法指向基站本身，而不是某个任务点。
        if normalized in _BASE_ALIASES:
            return self.base_station

        if normalized.startswith("task_default_"):
            candidate = self._indexed_candidate(normalized, offset=1)
            if candidate is not None:
                return candidate

        if normalized.startswith("task_generated_"):
            candidate = self._indexed_candidate(normalized, offset=0)
            if candidate is not None:
                return candidate

        if normalized.startswith("task_user_"):
            candidate = self._indexed_candidate(normalized, offset=1)
            if candidate is not None:
                return candidate

        candidates = self._target_candidates()
        return candidates[0] if candidates else self.base_station

    def _seed_drones(self, count: int):
        self.drones.clear()
        self._formation.clear()
        self._formation_axis.clear()
        count = max(1, min(int(count), 12))
        for idx in range(count):
            drone = DroneState(
                drone_id=f"drone_{idx + 1}",
                x=self.base_station[0],
                y=self.base_station[1],
                status="idle",
            )
            drone.signal_strength = self.signal_field.strength_at(drone.x, drone.y)
            drone.zone = zones.zone_of(drone.signal_strength)
            drone.observed_zone = drone.zone
            drone.prev_position = (drone.x, drone.y)
            self.drones[drone.drone_id] = drone

    # ------------------------------------------------------------------ 分配辅助

    def drone_distance_to(self, drone: DroneState, point: Tuple[float, float]) -> float:
        return math.hypot(drone.x - point[0], drone.y - point[1])

    def nearest_available_drone_for(
        self,
        task_target: Optional[str | Tuple[float, float]],
        *,
        prefer_near: Optional[Tuple[float, float]] = None,
        idle_only: bool = False,
    ):
        """选择执行任务的无人机。

        ``prefer_near`` 用于"同等级按空闲且离起点近排队"：起点由任务指定，
        未指定时由调用方传入无人机自身位置。``idle_only`` 用于非紧急任务
        —— 不允许打断正在执行的任务。
        """
        tx, ty = self.resolve_task_target(task_target)
        reference = prefer_near if prefer_near is not None else (tx, ty)

        candidates = []
        for drone in self.drones.values():
            if drone.task_id is not None:
                # 已抵达目标、等待返航的无人机优先回收：它离目标最近，
                # 若不先处理，下一轮会重新选中即将归巢的无人机，导致任务反复易手。
                if drone.status != "returning" or drone.returning_to_base:
                    continue
            if idle_only and drone.task_id is not None:
                continue
            distance = math.hypot(drone.x - reference[0], drone.y - reference[1])
            # 同距离时优先空闲机，其次优先编号小的，保证调度结果可复现。
            candidates.append((round(distance, 3), drone.task_id is not None, drone.drone_id, drone))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[:3])[3]

    # ------------------------------------------------------------------ 本地热力图

    def _local_zone(self, block: Tuple[int, int]) -> Optional[str]:
        entry = self.observed_heatmap.get(block)
        return entry["zone"] if entry else None

    def record_heatmap_sample(self, x: float, y: float, zone: Optional[str] = None) -> None:
        """记录一次区块观测。只有新增区块或分区发生变化时才推进版本号，
        这样 GUI 可以按版本号做"有变化才重绘"的节流更新。"""
        block = pathplan.block_of(x, y)
        center = pathplan.block_center(block)
        strength = self.signal_field.strength_at(center[0], center[1])
        resolved_zone = zone or zones.zone_of(strength)

        entry = self.observed_heatmap.get(block)
        self.heatmap_samples += 1
        if entry is None:
            self.observed_heatmap[block] = {
                "x": center[0],
                "y": center[1],
                "strength": round(strength, 2),
                "zone": resolved_zone,
                "visits": 1,
            }
            self._last_zone_by_block[block] = resolved_zone
            self.heatmap_version += 1
            return

        entry["visits"] = int(entry["visits"]) + 1
        entry["strength"] = round(strength, 2)
        if entry["zone"] != resolved_zone:
            entry["zone"] = resolved_zone
            self._last_zone_by_block[block] = resolved_zone
            self.heatmap_version += 1

    def block_observations(self, x: float, y: float, radius: float) -> List[Dict[str, object]]:
        """返回以 (x, y) 为中心、边长 ``radius`` 的区块观测窗口。

        服务端拿不到原始热力图，只能按区块轮询 3×3 范围的信号条件；
        radius=0 时退化为仅观测自身所在区块。
        """
        block = pathplan.block_of(x, y)
        span = max(0, int(radius))
        samples: List[Dict[str, object]] = []
        for bx in range(block[0] - span, block[0] + span + 1):
            for by in range(block[1] - span, block[1] + span + 1):
                center = pathplan.block_center((bx, by))
                strength = self.signal_field.strength_at(center[0], center[1])
                samples.append(
                    {
                        "block": (bx, by),
                        "x": center[0],
                        "y": center[1],
                        "signal_strength": strength,
                        "zone": zones.zone_of(strength),
                    }
                )
        return samples

    def _record_traversed_segments(self, drone: DroneState) -> None:
        """记录无人机本 tick 经过的所有路段的信号，累积成服务端本地热力图。"""
        previous = drone.prev_position
        if previous is None:
            drone.prev_position = (drone.x, drone.y)
            self.record_heatmap_sample(drone.x, drone.y)
            return

        travelled = math.hypot(drone.x - previous[0], drone.y - previous[1])
        if travelled <= 1e-6:
            self.record_heatmap_sample(drone.x, drone.y)
            return

        steps = max(1, int(travelled / SIGNAL_SEGMENT_STEP) + 1)
        for index in range(1, steps + 1):
            ratio = index / steps
            sample_x = previous[0] + (drone.x - previous[0]) * ratio
            sample_y = previous[1] + (drone.y - previous[1]) * ratio
            self.record_heatmap_sample(sample_x, sample_y)
        drone.prev_position = (drone.x, drone.y)

    def get_heatmap_blocks(self) -> List[Dict[str, object]]:
        """服务端本地热力图的完整快照，用于每次 AI 调度时传输。"""
        blocks = []
        for (bx, by), entry in sorted(self.observed_heatmap.items()):
            blocks.append(
                {
                    "block": [bx, by],
                    "x": entry["x"],
                    "y": entry["y"],
                    "strength": entry["strength"],
                    "zone": entry["zone"],
                    "visits": entry["visits"],
                }
            )
        return blocks

    def get_heatmap_payload(self) -> Dict[str, object]:
        counts = {zone: 0 for zone in zones.ZONE_ORDER}
        for entry in self.observed_heatmap.values():
            counts[str(entry["zone"])] = counts.get(str(entry["zone"]), 0) + 1
        return {
            "heatmap_version": self.heatmap_version,
            "observed_blocks": len(self.observed_heatmap),
            "zone_counts": counts,
            "blocks": self.get_heatmap_blocks(),
        }

    def mark_explored(self, x: float, y: float) -> None:
        self.explored_cells.add(self._coord_key(x, y))
        self._known_unknown_cache = None

    def _coord_key(self, x: float, y: float) -> Tuple[int, int]:
        return (int(round(x / 5.0)), int(round(y / 5.0)))

    def knowledge_status_at(self, x: float, y: float) -> str:
        """GUI 热力图沿用与服务端本地热力图一致的"已知/未探测"判定。"""
        if pathplan.block_of(x, y) in self.observed_heatmap:
            return "known"
        if self._coord_key(x, y) in self.explored_cells:
            return "known"
        distance = math.hypot(x - self.base_station[0], y - self.base_station[1])
        if distance <= _BASE_KNOWN_RADIUS:
            return "known"
        return "unknown"

    def get_known_unknown_summary(self) -> Dict[str, int]:
        """统计 AI 视野内的已确认通行／未观测区块数。

        采样点走 ``pathplan.block_center``，与热力图区块模型保持对齐
        （旧实现使用 ``range(-220, 221, 18)``，与区块中心相差 4 个单位）。
        """
        if self._known_unknown_cache is not None:
            return dict(self._known_unknown_cache)

        observed = len(self.observed_heatmap)
        summary = {"known": 0, "unknown": 0, "observed": observed}
        bound = pathplan.block_of(WORLD_BOUND, WORLD_BOUND)[0]
        for bx in range(-bound, bound + 1):
            for by in range(-bound, bound + 1):
                center_x, center_y = pathplan.block_center((bx, by))
                status = self.knowledge_status_at(center_x, center_y)
                if status == "known":
                    summary["known"] += 1
                else:
                    summary["unknown"] += 1
        self._known_unknown_cache = dict(summary)
        return dict(summary)

    def scan_local_observations(self) -> List[Dict[str, float | str]]:
        return [self.signal_field.local_observation(drone) for drone in self.drones.values()]

    def get_drones_state(self):
        return [self._drone_snapshot(drone) for drone in self.drones.values()]

    def _drone_snapshot(self, drone: DroneState) -> Dict[str, object]:
        """单机运行状态快照：供监控页的状态列表与地图标注直接使用。"""
        target = drone.task_end
        return {
            "id": drone.drone_id,
            "x": drone.x,
            "y": drone.y,
            "status": drone.status,
            "status_label": _STATUS_LABELS.get(drone.status, drone.status),
            "task_id": drone.task_id,
            "busy_with": self._describe_activity(drone),
            "signal_strength": round(drone.signal_strength, 1),
            "zone": drone.zone,
            "zone_label": zones.label_for_zone(drone.zone),
            "observed_zone": drone.observed_zone,
            "observed_zone_label": zones.label_for_zone(drone.observed_zone),
            "borrowed_signal": drone.borrowed_signal,
            "borrow_radius": drone.borrow_radius,
            "task_priority": drone.task_priority,
            "task_priority_label": task_pool.priority_label(drone.task_priority),
            "task_type": drone.task_type,
            "task_target": None if target is None else "({:.0f}, {:.0f})".format(target[0], target[1]),
            "return_to_base": drone.return_to_base,
            "returning_to_base": drone.returning_to_base,
            "needs_replan": drone.needs_replan,
            "position": "({:.0f}, {:.0f})".format(drone.x, drone.y),
            "route_length": len(drone.planned_route),
            "route_index": drone.route_index,
        }

    def _describe_activity(self, drone: DroneState) -> str:
        """把内部状态翻译成"正在干嘛"的中文一句话描述。"""
        base = _STATUS_ACTIVITY.get(drone.status)
        if base is None:
            return "空闲" if drone.task_id is None else "执行任务 {}".format(drone.task_id)
        if drone.task_id is None:
            return base
        if drone.status == "holding":
            return "{}（{}，{}等待）".format(base, drone.task_id, zones.label_for_zone(drone.observed_zone))
        if drone.borrowed_signal:
            return "{}（{}，借用 3×3 集群信号）".format(base, drone.task_id)
        return "{}（{}）".format(base, drone.task_id)

    # ------------------------------------------------------------------ 多机编队

    def _leave_formation(self, drone: DroneState) -> None:
        """把无人机从所有任务的编队表里摘掉，空表连同飞行道基准一并清理。"""
        drone_id = drone.drone_id
        for task_id in [key for key, members in self._formation.items() if drone_id in members]:
            members = self._formation[task_id]
            members.remove(drone_id)
            if not members:
                del self._formation[task_id]
                self._formation_axis.pop(task_id, None)

    def _formation_slot(self, drone: DroneState) -> int:
        """登记无人机在当前任务里的分机序号（同一任务内先到先得）。

        先摘掉旧登记再重新入列，使 ``_preempt`` 造成的"先解绑再绑定"不会吃掉名次。
        无任务时不参与编队，序号固定为 0。
        """
        self._leave_formation(drone)
        if drone.task_id is None:
            return 0
        members = self._formation.setdefault(str(drone.task_id), [])
        members.append(drone.drone_id)
        return len(members) - 1

    def _formation_slot_of(self, drone: DroneState) -> int:
        """查询无人机已有的分机序号，不产生登记副作用。"""
        if drone.task_id is None:
            return 0
        members = self._formation.get(str(drone.task_id))
        if not members or drone.drone_id not in members:
            return 0
        return members.index(drone.drone_id)

    def _task_axis(self, drone: DroneState, start: Tuple[float, float],
                   goal: Tuple[float, float]) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        """同一任务共用的飞行道基准轴（任务首个规划时的"起点→目标点"）。

        同任务多机必须绕同一条基准轴排道：若各机用自己"当前起→终"算偏移方向，两机方向
        的细微差异会让偏移方向随之旋转，平移后的航线反而交叉——必须共轴才真正平行。
        """
        if drone.task_id is None:
            return (start, goal)
        task_id = str(drone.task_id)
        axis = self._formation_axis.get(task_id)
        if axis is None:
            axis = (start, goal)
            self._formation_axis[task_id] = axis
        return axis

    def _lane_offset(self, drone: DroneState, start: Tuple[float, float],
                     goal: Tuple[float, float]) -> Tuple[float, float]:
        """本机相对任务基准轴的横向飞行道偏移（垂直方向，两侧交替，间距 `_SEPARATION_RADIUS`）。

        分机按 0、+1、-1、+2、-2… 交错排布，任意两机的横向偏移互不相同，因此同任务
        多机在垂直基准轴的方向上恒有排距，不会因为"同起点同航向同速"而途中重合。
        """
        slot = self._formation_slot_of(drone)
        if slot <= 0:
            return (0.0, 0.0)
        (ax, ay), (bx, by) = self._task_axis(drone, start, goal)
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return (0.0, 0.0)
        normal = (-dy / length, dx / length)
        lateral = _SEPARATION_RADIUS * ((slot + 1) // 2) * (1 if slot % 2 else -1)
        return (normal[0] * lateral, normal[1] * lateral)

    def _lane_route(self, drone: DroneState, route: List[Tuple[float, float]],
                    start: Tuple[float, float], goal: Tuple[float, float]) -> List[Tuple[float, float]]:
        """把中间航点整体平移到本机飞行道上，避免同任务多机共线飞行。

        终点航点已带搜索队形偏移，保持不动（`plan_route` 的最后一点严格等于目标点，
        动了会破坏到达判定）；起点不动，否则各机从不同位置起飞、起飞段反而交叉。
        平移后落入紫色危险区块的航点放弃偏移——危险区域禁入优先于排距。
        """
        offset = self._lane_offset(drone, start, goal)
        if offset == (0.0, 0.0) or len(route) <= 1:
            return route
        shifted: List[Tuple[float, float]] = []
        for waypoint in route[:-1]:
            bounded = self._offset_within_bounds(waypoint, offset)
            candidate = (waypoint[0] + bounded[0], waypoint[1] + bounded[1])
            if zones.is_forbidden(self.signal_field.zone_at(candidate[0], candidate[1])):
                shifted.append(waypoint)
            else:
                shifted.append(candidate)
        shifted.append(route[-1])
        return shifted

    def _formation_offset(self, slot: int) -> Tuple[float, float]:
        """分机序号 -> 相对目标点的搜索队形偏移。

        序号 0 占目标点中心；其余按 ``_SEPARATION_RADIUS`` 环形均匀排布，单圈排满后
        逐圈外扩，使同任务多机既不重叠成一点，也不会互相挡路。
        """
        if slot <= 0:
            return (0.0, 0.0)
        ring, position = divmod(slot - 1, _FORMATION_SLOTS_PER_RING)
        angle = 2.0 * math.pi * position / _FORMATION_SLOTS_PER_RING
        radius = _SEPARATION_RADIUS * (1 + ring)
        return (radius * math.cos(angle), radius * math.sin(angle))

    @staticmethod
    def _offset_within_bounds(target: Tuple[float, float], offset: Tuple[float, float]) -> Tuple[float, float]:
        """把偏移整体缩放到沙盘边界内。

        等比缩放不改变方向，因此同一圈的各个槽位仍互不重合；直接夹取坐标反而会把
        外圈槽位压到同一点上。
        """
        scale = 1.0
        for target_value, offset_value in zip(target, offset):
            if offset_value > 0.0:
                scale = min(scale, (WORLD_BOUND - target_value) / offset_value)
            elif offset_value < 0.0:
                scale = min(scale, (-WORLD_BOUND - target_value) / offset_value)
        scale = max(0.0, min(1.0, scale))
        return (offset[0] * scale, offset[1] * scale)

    # ------------------------------------------------------------------ 任务绑定

    def bind_task(
        self,
        drone: DroneState,
        task_id: Optional[str] = None,
        *,
        task_type: Optional[str] = None,
        child_id: Optional[str] = None,
        task_end: Optional[Tuple[float, float]] = None,
        task_start: Optional[Tuple[float, float]] = None,
        priority: Optional[str] = None,
        return_to_base: Optional[bool] = None,
    ) -> None:
        if task_id is not None:
            drone.task_id = str(task_id)
        drone.child_id = child_id
        drone.execution_version += 1
        drone.task_phase = "start" if task_start is not None else "end"
        if task_type is not None:
            drone.task_type = str(task_type)
        if task_end is not None:
            drone.task_end = task_end
        elif drone.task_id is not None:
            drone.task_end = self.resolve_task_target(drone.task_id)
        if drone.task_end is not None:
            self.signal_field.add_protected_point(drone.task_end)
        # 多机共用一个目标点时按搜索队形分散：0 号机占目标点中心，其余环绕排布。
        slot = self._formation_slot(drone)
        if drone.task_end is not None and slot > 0:
            offset = self._offset_within_bounds(drone.task_end, self._formation_offset(slot))
            drone.task_end = (drone.task_end[0] + offset[0], drone.task_end[1] + offset[1])
        # 未指定出发地点时，默认以无人机自身位置为起点。
        drone.task_start = task_start if task_start is not None else (drone.x, drone.y)
        # 飞行道基准轴取任务的首个"起点→未偏移目标点"，之后同任务各机共用同一条轴。
        if drone.task_id is not None and drone.task_end is not None:
            self._formation_axis.setdefault(
                str(drone.task_id), (drone.task_start, task_end if task_end is not None else drone.task_end)
            )
        if priority is not None:
            drone.task_priority = str(priority)
        # return_to_base=None 时沿用任务池默认（原地待命），显式传值以任务为准。
        if return_to_base is not None:
            drone.return_to_base = bool(return_to_base)
        drone.status = "assigned"
        drone.returning_to_base = False
        self._reset_leg(drone)
        drone.leg_replans = 0
        drone.red_bypass = False
        drone.blocked_streak = 0
        drone.planned_route = []
        drone.route_index = 0
        drone.needs_replan = True
        drone.ai_route_active = False
        self._observe(drone)

    def _rest_status(self, drone: DroneState) -> str:
        """无任务时的状态：停在基站视作空闲，停在别处视作原地待命。"""
        distance = math.hypot(drone.x - self.base_station[0], drone.y - self.base_station[1])
        return "idle" if distance <= ARRIVAL_TOLERANCE else "awaiting"

    def release_task(self, drone: DroneState, *, status: Optional[str] = None) -> None:
        """解绑任务并清空线路状态，但**不改动坐标**。

        原地待命（``status="awaiting"``）与归巢后闲置（``status="idle"``）都靠它兜底：
        任务相关的字段一次性清干净，避免残留的 ``task_end``/``route_index`` 让
        下一轮分配把上一任务的航点当成"已规划路线"继续飞。
        """
        self._leave_formation(drone)
        drone.execution_version += 1
        drone.child_id = None
        drone.task_phase = "end"
        drone.task_id = None
        drone.task_end = None
        drone.task_start = None
        drone.task_priority = "normal"
        drone.planned_route = []
        drone.route_index = 0
        drone.needs_replan = False
        drone.ai_route_active = False
        drone.returning_to_base = False
        drone.leg_replans = 0
        drone.red_bypass = False
        self._reset_leg(drone)
        drone.status = status or self._rest_status(drone)

    def apply_strategy(self, strategy: Dict[str, object]) -> bool:
        assignments = strategy.get("assignments", {})
        if strategy.get("status", "hold") == "hold":
            return False

        task_types = strategy.get("task_types", {}) or {}
        priorities = strategy.get("task_priorities", {}) or {}
        # 紧急任务可以抢占正在执行的任务，因此不作为"已被占用"处理。
        claimed_tasks = {
            drone.task_id
            for drone in self.drones.values()
            if drone.task_id is not None and drone.task_priority != "urgent"
        }
        for drone_id, task_id in assignments.items():
            if drone_id not in self.drones:
                continue
            task_name = str(task_id)
            if task_name in claimed_tasks:
                continue
            drone = self.drones[drone_id]
            self.bind_task(
                drone,
                task_name,
                task_type=str(task_types.get(drone_id, drone.task_type)),
                priority=str(priorities.get(drone_id, drone.task_priority)),
            )
            claimed_tasks.add(task_name)
        return True

    # ------------------------------------------------------------------ 线路规划

    def task_goal(self, drone: DroneState) -> Optional[Tuple[float, float]]:
        if drone.task_phase == "start" and drone.task_start is not None:
            return drone.task_start
        if drone.task_end is not None:
            return drone.task_end
        if drone.task_id is None:
            return None
        return self.resolve_task_target(drone.task_id)

    def ensure_route(self, drone: DroneState, force: bool = False) -> bool:
        """基于服务端本地热力图规划航点序列（紫色危险区块不可通行）。"""
        goal = self.task_goal(drone)
        if goal is None:
            return False
        if drone.task_phase == "end":
            drone.task_end = goal
        if drone.planned_route and not force and not drone.needs_replan:
            return True
        route = pathplan.plan_route((drone.x, drone.y), goal, self._local_zone)
        if not route:
            drone.planned_route = []
            drone.route_index = 0
            drone.needs_replan = True
            drone.status = "blocked"
            return False
        drone.planned_route = self._lane_route(drone, route, (drone.x, drone.y), goal)
        drone.route_index = 0
        drone.needs_replan = False
        self._reset_leg(drone)
        if drone.status in {"idle", "awaiting", "planning"}:
            drone.status = "assigned"
        return True




    def apply_ai_route(self, drone: DroneState, waypoints: List[List[float]]) -> bool:
        """直接采用 AI 的局部路线，基础格式错误时才拒绝。"""
        goal = self.task_goal(drone)
        if goal is None or not waypoints:
            return False
        if len(waypoints) < 2:
            return False
        route: List[Tuple[float, float]] = []
        for raw_point in waypoints:
            if not isinstance(raw_point, list) or len(raw_point) != 2:
                return False
            try:
                point = (float(raw_point[0]), float(raw_point[1]))
            except (TypeError, ValueError):
                return False
            if not all(-WORLD_BOUND <= value <= WORLD_BOUND for value in point):
                return False
            route.append(point)
        # AI 返回的是当前局部决策，原样写入；不再用本地 A* 或编队偏移改写它。
        drone.planned_route = route
        drone.route_index = 0
        drone.needs_replan = False
        drone.ai_route_active = True
        drone.blocked_streak = 0
        drone.status = "assigned"
        self._reset_leg(drone)
        return True

    def current_goal(self, drone: DroneState) -> Tuple[float, float]:
        if drone.planned_route and drone.route_index < len(drone.planned_route):
            return drone.planned_route[drone.route_index]
        return self.task_goal(drone) or (drone.x, drone.y)

    def is_final_leg(self, drone: DroneState) -> bool:
        return drone.route_index >= len(drone.planned_route) - 1

    # ------------------------------------------------------------------ 观测

    def _borrow_radius_for(self, drone: DroneState) -> float:
        """紧急任务可借用 3×3 范围内任意无人机的信号（紫色危险区块不参与）。

        radius=1 表示 3×3 区块；非紧急任务只能看到自身所在区块。
        """
        if drone.returning_to_base or drone.task_id is None:
            return 0.0
        return 1.0 if task_pool.may_borrow_signal(drone.task_priority) else 0.0

    def _observe(self, drone: DroneState) -> None:
        self._record_traversed_segments(drone)

        if drone.returning_to_base:
            drone.borrowed_signal = False
            drone.borrow_radius = 0.0
            drone.signal_strength = self.signal_field.strength_at(drone.x, drone.y)
            drone.zone = zones.zone_of(drone.signal_strength)
            drone.observed_zone = drone.zone
            return

        radius = self._borrow_radius_for(drone)
        samples = self.block_observations(drone.x, drone.y, radius)
        for sample in samples:
            self.record_heatmap_sample(sample["x"], sample["y"], str(sample["zone"]))
        # 先记录前方邻区，再规划当前航段，避免下一步才首次发现紫色区块。
        lookahead_samples = self.block_observations(drone.x, drone.y, 1)
        for sample in lookahead_samples:
            self.record_heatmap_sample(sample["x"], sample["y"], str(sample["zone"]))

        drone.borrow_radius = radius
        drone.borrowed_signal = radius > 0.0
        borrowed_strength = max(float(sample["signal_strength"]) for sample in samples)
        local_strength = self.signal_field.strength_at(drone.x, drone.y)
        # 借用时取窗口内最强信号作为"集群信号条件"，但紫色危险区块不参与借用。
        usable = [float(s["signal_strength"]) for s in samples if not zones.is_forbidden(str(s["zone"]))]
        drone.signal_strength = max(usable) if (drone.borrowed_signal and usable) else local_strength
        drone.zone = zones.zone_of(drone.signal_strength)
        drone.observed_zone = zones.zone_of(local_strength)

    # ------------------------------------------------------------------ 运动

    def _step_for(self, drone: DroneState) -> float:
        zone = drone.zone
        return _STEP_BY_ZONE.get(zone, _PERSPECTIVE_STEP)
    def step_size_for(self, drone: DroneState) -> float:
        """对外暴露的单步步长，供 backend 拼 AI 请求上下文。"""
        return self._step_for(drone)

    def _apply_step(self, drone: DroneState, heading: Tuple[float, float], step: float) -> bool:
        if heading[0] == 0.0 and heading[1] == 0.0:
            return False
        next_x = drone.x + heading[0] * step
        next_y = drone.y + heading[1] * step
        samples = max(1, math.ceil(abs(step) / (pathplan.BLOCK_SIZE / 12.0)))
        if any(zones.is_forbidden(self.signal_field.zone_at(
                drone.x + (next_x - drone.x) * i / samples,
                drone.y + (next_y - drone.y) * i / samples)) for i in range(1, samples + 1)):
            d = _direction_name(heading)
            if d not in drone.failed_moves:
                drone.failed_moves.append(d)
            return False
        drone.x = next_x
        drone.y = next_y
        drone.blocked_streak = 0
        drone.recent_moves.append(_direction_name(heading))
        if len(drone.recent_moves) > 5:
            drone.recent_moves.pop(0)
        drone.failed_moves.clear()
        return True

    def _green_seek_heading(self, drone: DroneState, hx: float, hy: float) -> Tuple[float, float]:
        """黄色区在不影响任务的前提下向附近绿色区块靠拢。"""
        if self.is_final_leg(drone) or drone.observed_zone != zones.YELLOW:
            return hx, hy

        block = pathplan.block_of(drone.x, drone.y)
        best = None
        for dx, dy in pathplan.NEIGHBOURS:
            entry = self.observed_heatmap.get((block[0] + dx, block[1] + dy))
            if entry is None or zones.zone_rank(str(entry["zone"])) >= zones.zone_rank(zones.YELLOW):
                continue
            score = (zones.zone_rank(str(entry["zone"])), abs(dx) + abs(dy))
            if best is None or score < best[0]:
                best = (score, dx, dy)
        if best is None:
            return hx, hy
        _, dx, dy = best
        biased = _unit(hx + _GREEN_SEEK_WEIGHT * dx, hy + _GREEN_SEEK_WEIGHT * dy)
        return biased if biased != (0.0, 0.0) else (hx, hy)

    def _search_fallback_block(self, drone: DroneState) -> Optional[Tuple[int, int]]:
        """红色区停滞时，在 4x4 邻域内搜索可回退的黄色/绿色区块。"""
        block = pathplan.block_of(drone.x, drone.y)
        goal = self.task_goal(drone)
        goal_block = pathplan.block_of(*goal) if goal else block
        best = None
        for dx in range(-_FALLBACK_WINDOW, _FALLBACK_WINDOW):
            for dy in range(-_FALLBACK_WINDOW, _FALLBACK_WINDOW):
                if dx == 0 and dy == 0:
                    continue
                candidate = (block[0] + dx, block[1] + dy)
                entry = self.observed_heatmap.get(candidate)
                if entry is None:
                    continue
                zone = str(entry["zone"])
                if zones.is_forbidden(zone) or zones.must_hold(zone):
                    continue
                real_center = pathplan.block_center(candidate)
                real_zone = self.signal_field.zone_at(real_center[0], real_center[1])
                if zones.is_forbidden(real_zone) or zones.must_hold(real_zone):
                    continue
                # 排除口袋：回退点朝 goal 方向的下一个区块不能是红/紫，否则进去后又被堵回来
                toward_goal = (
                    1 if candidate[0] < goal_block[0] else (-1 if candidate[0] > goal_block[0] else 0),
                    1 if candidate[1] < goal_block[1] else (-1 if candidate[1] > goal_block[1] else 0),
                )
                next_block = (candidate[0] + toward_goal[0], candidate[1] + toward_goal[1])
                next_entry = self.observed_heatmap.get(next_block)
                if next_entry is not None:
                    next_zone = str(next_entry["zone"])
                    if zones.is_forbidden(next_zone) or zones.must_hold(next_zone):
                        continue
                score = (
                    zones.zone_rank(zone),
                    math.hypot(candidate[0] - goal_block[0], candidate[1] - goal_block[1]),
                )
                if best is None or score < best[0]:
                    best = (score, candidate)
        return best[1] if best else None

    def _exit_forbidden(self, drone: DroneState) -> None:
        """紫色危险区域完全禁入：先退出到 8 邻域中信号最好的区块，再重新规划路径。"""
        block = pathplan.block_of(drone.x, drone.y)
        candidates = []
        for dx, dy in pathplan.NEIGHBOURS:
            center = pathplan.block_center((block[0] + dx, block[1] + dy))
            strength = self.signal_field.strength_at(center[0], center[1])
            if zones.is_forbidden(zones.zone_of(strength)):
                continue
            candidates.append((strength, center))
        if not candidates:
            drone.status = "blocked"
            drone.needs_replan = True
            return
        step = self._step_for(drone)
        # 已经在紫色区时逐方向探测实际落点，不能只相信邻区中心的颜色。
        exits = []
        for strength, center in candidates:
            heading = _unit(center[0] - drone.x, center[1] - drone.y)
            for multiplier in range(1, 13):
                distance = step * multiplier
                candidate = (drone.x + heading[0] * distance, drone.y + heading[1] * distance)
                if not zones.is_forbidden(self.signal_field.zone_at(candidate[0], candidate[1])):
                    exits.append((multiplier, -strength, candidate))
                    break
        if not exits:
            drone.status = "blocked"
            drone.needs_replan = True
            return
        _, _, exit_point = min(exits)
        drone.x, drone.y = exit_point
        drone.status = "exiting"
        drone.needs_replan = True

    def _slide_along_forbidden(self, drone: DroneState, heading: Tuple[float, float]) -> bool:
        """沿紫色危险区域边界侧滑，避免与规划器来回抖动导致原地不动。"""
        step = max(self._step_for(drone) * 2.5, pathplan.BLOCK_SIZE * 0.6)
        for side in (1, -1):
            slide = (-heading[1] * side, heading[0] * side)
            if self._apply_step(drone, _unit(slide[0], slide[1]), step):
                return True
        return False

    def _reset_leg(self, drone: DroneState) -> None:
        """换航点/重新绑定任务时重置"停滞-回退"相关的本地状态。
        ``leg_replans`` 与 ``red_bypass`` 不在每段航程内清零，避免"重新规划-立刻又停滞"
        的死循环；它们分别在 ``_finish_leg`` 与 ``bind_task`` 中重置。
        """
        drone.stall_ticks = 0
        drone.best_distance = None
        

    def _register_progress(self, drone: DroneState, distance: float) -> bool:
        """记录相对当前航点的最近距离；返回本 tick 是否有实质进展。"""
        if drone.best_distance is None:
            drone.best_distance = distance
            drone.stall_ticks = 0
            return True
        if distance < drone.best_distance - _PROGRESS_EPSILON:
            drone.best_distance = distance
            drone.stall_ticks = 0
            return True
        drone.stall_ticks += 1
        return False

    def _finish_leg(self, drone: DroneState) -> None:
        if self.is_final_leg(drone) and drone.ai_route_active:
            goal = self.task_goal(drone)
            if goal is not None and math.hypot(drone.x - goal[0], drone.y - goal[1]) > ARRIVAL_TOLERANCE:
                # AI 航点只是局部建议，到达后继续由本地路线前往真实任务终点。
                drone.ai_route_active = False
                drone.planned_route = []
                drone.route_index = 0
                drone.needs_replan = True
                drone.status = "assigned"
                self._reset_leg(drone)
                return
        if self.is_final_leg(drone) and drone.task_phase == "start":
            drone.task_phase = "end"
            drone.execution_version += 1
            drone.planned_route = []
            drone.route_index = 0
            drone.needs_replan = True
            drone.ai_route_active = False
            drone.status = "assigned"
            self._reset_leg(drone)
            return
        if self.is_final_leg(drone):
            if drone.return_to_base:
                drone.status = "returning"
                drone.returning_to_base = True
            else:
                # 到达任务终点：原地待命等下一个任务，任务结算由后端负责。
                drone.status = "awaiting"
                drone.returning_to_base = False
            drone.planned_route = []
            drone.route_index = 0
            drone.needs_replan = False
        else:
            drone.route_index += 1
        # 换航点即视为新的一段航程，红色区重新规划预算与降级标记全部重置。
        drone.leg_replans = 0
        drone.red_bypass = False
        drone.blocked_streak = 0
        self._reset_leg(drone)

    def _advance(self, drone: DroneState) -> None:
        if not drone.ai_route_active:
            # 进入新区块时强制重规划：A* 基于最新观测（可能刚发现红色/紫色）重新选路，
            # 避免沿旧路线穿过规划时还未观测到的危险区。
            current_block = pathplan.block_of(drone.x, drone.y)
            if getattr(drone, "_last_planned_block", None) != current_block:
                drone._last_planned_block = current_block
                drone.needs_replan = True
            if not self.ensure_route(drone):
                return
        goal = self.current_goal(drone)
        dx = goal[0] - drone.x
        dy = goal[1] - drone.y
        distance = math.hypot(dx, dy)

        if distance <= ARRIVAL_TOLERANCE:
            self._finish_leg(drone)
            return

        heading = _unit(dx, dy)
        step = self._step_for(drone)
        final_leg = self.is_final_leg(drone)
        # 终点区块本身可能落在红色/紫色危险区（它就是目的地），此时以借用的集群信号作为准入门槛。
        judge_zone = drone.zone if (final_leg and drone.observed_zone != zones.GREEN) else drone.observed_zone

        # 紫色完全禁入：先退出到 8 邻域内信号最好的区块，再交回服务端重新规划。
        if zones.is_forbidden(judge_zone):
            self._exit_forbidden(drone)
            return

        if drone.ai_route_active and drone.needs_replan:
            drone.status = "planning"
            return

        if drone.red_bypass:
            # 本地避障与重新规划都已用尽，按分区限速直行通过，避免在分区边界无限振荡。
            if self._apply_step(drone, heading, min(step, distance)):
                drone.status = "moving"
            else:
                drone.status = "blocked"
                drone.needs_replan = True
            return

        progressed = self._register_progress(drone, distance)
        if zones.must_hold(judge_zone) and not progressed:
            if drone.stall_ticks < _STALL_TICKS_LIMIT:
                if self._apply_step(drone, heading, min(step, distance)):
                    drone.status = "moving"
                    return
                # 直行被紫色拦：直接本地侧滑，不等 AI（AI 太慢，等返回位置都变了）
                if self._slide_along_forbidden(drone, heading):
                    drone.status = "avoiding"
                    return
            fallback = self._search_fallback_block(drone)
            if fallback is not None:
                center = pathplan.block_center(fallback)
                fallback_heading = _unit(center[0] - drone.x, center[1] - drone.y)
                fallback_distance = math.hypot(center[0] - drone.x, center[1] - drone.y)
                if fallback_heading == (0.0, 0.0) or fallback_distance < 1.0:
                    drone.status = "avoiding"
                    self._reset_leg(drone)
                    return
                mid_x = drone.x + fallback_heading[0] * fallback_distance * 0.5
                mid_y = drone.y + fallback_heading[1] * fallback_distance * 0.5
                if (
                    not zones.is_forbidden(self.signal_field.zone_at(mid_x, mid_y))
                    and not zones.is_forbidden(self.signal_field.zone_at(center[0], center[1]))
                    and not zones.must_hold(self.signal_field.zone_at(center[0], center[1]))
                    and self._apply_step(drone, fallback_heading, fallback_distance)
                ):
                    drone.status = "avoiding"
                    self._reset_leg(drone)
                    return
            drone.status = "holding"
            return


        if not progressed and drone.stall_ticks >= _STALL_TICKS_LIMIT:
            # 绿色区没有干扰时，距离改善阈值不应单独把正常飞行判成受阻。
            # 先按目标方向继续走，只有实际步进被拦截时才触发重规划。
            if drone.observed_zone == zones.GREEN and self._apply_step(drone, heading, min(step, distance)):
                drone.status = "moving"
                self._reset_leg(drone)
                return

            # 停滞超时且确实无法继续前进：本地已无计可施，重新规划路线。
            if drone.leg_replans < _BLOCKED_REPLAN_LIMIT:
                drone.leg_replans += 1
                drone.needs_replan = True
                if not drone.ai_route_active:
                    self.ensure_route(drone, force=True)
                drone.status = "planning"
                return
            drone.red_bypass = True
            if self._apply_step(drone, heading, min(step, distance)):
                drone.status = "moving"
            else:
                drone.status = "blocked"
                drone.needs_replan = True
            return

        heading = self._green_seek_heading(drone, heading[0], heading[1])
        if self._apply_step(drone, heading, min(step, distance)):
            drone.status = "moving"
            return

        # 紫色危险区域拦住了这一步：先尝试重新规划，仍不行则沿边界侧滑。
        drone.blocked_streak += 1
        if drone.blocked_streak <= _BLOCKED_REPLAN_LIMIT:
            if not drone.ai_route_active:
                self.ensure_route(drone, force=True)
            drone.needs_replan = True
            drone.status = "planning"
            return
        if self._slide_along_forbidden(drone, heading):
            drone.status = "avoiding"
            return
        drone.status = "blocked"
        drone.needs_replan = True

    def _return_to_base(self, drone: DroneState) -> None:
        tx, ty = self.base_station
        heading = _unit(tx - drone.x, ty - drone.y)
        distance = math.hypot(tx - drone.x, ty - drone.y)
        if distance <= ARRIVAL_TOLERANCE:
            drone.x, drone.y = self.base_station
            self.release_task(drone, status="idle")
            return
        # 归巢段始终按 3×3 借用的集群信号条件判断，保证"不打断当前进程"。
        self._apply_step(drone, heading, min(_PERSPECTIVE_STEP, distance))
        drone.status = "returning"

    def tick(self) -> List[Dict[str, float | str]]:
        for drone in self.drones.values():
            self._observe(drone)

            if drone.returning_to_base:
                self._return_to_base(drone)
                continue

            if drone.task_id is None or drone.status == "awaiting":
                # 不无脑返航：留在原地待命，只在基站附近才标为空闲。
                # status == "awaiting" 表示已抵达任务终点但后端还没结算（同一 tick 内完成），
                # 这里直接停住，避免把终点当成新航点反复"重新抵达"。
                drone.planned_route = []
                drone.route_index = 0
                drone.status = self._rest_status(drone)
                continue

            self._advance(drone)

        self._known_unknown_cache = None
        return self.scan_local_observations()
