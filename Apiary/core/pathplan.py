"""本地线路规划（A*，跑在区块网格上）。

服务端拿不到原始全局热力图，只能拿到无人机 3×3 区块范围内的信号条件，
因此规划器在一张"逐步观测出来"的本地热力图上搜索：

* 绿色/黄色：优先通行
* 红色：高代价，能绕则绕（本地避障仍会在必要时停滞并回退）
* 紫色：代价无穷大（完全禁入）
* 未探测：中等代价，鼓励先沿已知绿色走廊走，但不会因此完全不敢探索

规划结果受"限制绕路"约束：路径长度超过直线距离的 ``MAX_DETOUR_RATIO`` 倍时，
退化为直线航向，把决定权交回给本地避障 + 重新调度。
"""

from __future__ import annotations

import heapq
import math
from typing import Dict, List, Optional, Sequence, Tuple

from core import zones

# 与 GUI 热力图、本地观测共用的区块边长。
BLOCK_SIZE = 18.0
# 8 邻域（dx, dy）。
NEIGHBOURS = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)
_DIAGONAL_COST = math.sqrt(2.0)

# 各分区的通行代价；紫色不可通行。
ZONE_COST = {
    zones.GREEN: 1.0,
    zones.YELLOW: 1.8,
    zones.RED: 20.0,
}
# 未探测区块的代价：高于黄色、低于红色。
UNKNOWN_COST = 2.0
IMPASSABLE = float("inf")

# 限制绕路：超过该倍数即放弃绕行方案。
MAX_DETOUR_RATIO = 5.0
# 搜索范围上限（区块数），防止在空旷地图上做无谓的大范围搜索。
MAX_EXPANSIONS = 4000


def block_of(x: float, y: float) -> Tuple[int, int]:
    return int(round(x / BLOCK_SIZE)), int(round(y / BLOCK_SIZE))


def block_center(block: Tuple[int, int]) -> Tuple[float, float]:
    return block[0] * BLOCK_SIZE, block[1] * BLOCK_SIZE


def zone_cost(zone: Optional[str]) -> float:
    if zone is None:
        return UNKNOWN_COST
    if zones.is_forbidden(zone):
        return IMPASSABLE
    return ZONE_COST.get(zone, UNKNOWN_COST)


def plan_route(
    start: Tuple[float, float],
    goal: Tuple[float, float],
    zone_lookup,
    max_detour_ratio: float = MAX_DETOUR_RATIO,
) -> List[Tuple[float, float]]:
    """在区块网格上规划 start -> goal 的航点序列。

    ``zone_lookup(block) -> zone | None`` 由调用方提供，通常是本地热力图。
    返回的航点不含起点，最后一个航点一定是 goal 本身（保证任务一定有终点）。
    """
    start_block = block_of(*start)
    goal_block = block_of(*goal)
    if zones.is_forbidden(zone_lookup(goal_block)):
        return []
    if start_block == goal_block:
        return [goal]

    straight = math.hypot(goal[0] - start[0], goal[1] - start[1])
    if straight <= 1e-6:
        return [goal]

    came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
    best_cost: Dict[Tuple[int, int], float] = {start_block: 0.0}
    counter = 0
    frontier: List[Tuple[float, int, Tuple[int, int]]] = [
        (_heuristic(start_block, goal_block), counter, start_block)
    ]
    expansions = 0
    found = False

    while frontier and expansions < MAX_EXPANSIONS:
        _, _, current = heapq.heappop(frontier)
        expansions += 1
        if current == goal_block:
            found = True
            break
        current_cost = best_cost.get(current, IMPASSABLE)
        for dx, dy in NEIGHBOURS:
            neighbour = (current[0] + dx, current[1] + dy)
            if dx and dy and (zones.is_forbidden(zone_lookup((current[0] + dx, current[1])))
                              or zones.is_forbidden(zone_lookup((current[0], current[1] + dy)))):
                continue
            step = _DIAGONAL_COST if dx and dy else 1.0
            if neighbour == goal_block:
                # 终点区块必须可达：任务终点落在红色区是允许的（否则任务永远无法完成）。
                added = step
            else:
                added = step * zone_cost(zone_lookup(neighbour))
                if added == IMPASSABLE:
                    continue
            tentative = current_cost + added
            if tentative < best_cost.get(neighbour, IMPASSABLE):
                best_cost[neighbour] = tentative
                came_from[neighbour] = current
                counter += 1
                heapq.heappush(
                    frontier,
                    (tentative + _heuristic(neighbour, goal_block), counter, neighbour),
                )

    if not found:
        return []

    blocks = _reconstruct(came_from, start_block, goal_block)
    waypoints = [block_center(block) for block in blocks[1:]]
    waypoints[-1] = goal
    if path_length(start, waypoints) > straight * max_detour_ratio:
        # 绕路代价过高，交给本地避障按直线推进。
        return []
    return _simplify(waypoints)


def path_length(start: Tuple[float, float], waypoints: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    previous = start
    for point in waypoints:
        total += math.hypot(point[0] - previous[0], point[1] - previous[1])
        previous = point
    return total


def _heuristic(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _reconstruct(
    came_from: Dict[Tuple[int, int], Tuple[int, int]],
    start: Tuple[int, int],
    goal: Tuple[int, int],
) -> List[Tuple[int, int]]:
    blocks = [goal]
    current = goal
    while current != start:
        current = came_from[current]
        blocks.append(current)
    blocks.reverse()
    return blocks


def _simplify(waypoints: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """合并共线航点，减少需要向无人机下发的转折点数量。"""
    if len(waypoints) <= 2:
        return waypoints
    simplified = [waypoints[0]]
    for index in range(1, len(waypoints) - 1):
        previous = simplified[-1]
        current = waypoints[index]
        following = waypoints[index + 1]
        first_angle = math.atan2(current[1] - previous[1], current[0] - previous[0])
        second_angle = math.atan2(following[1] - current[1], following[0] - current[0])
        if abs(first_angle - second_angle) > 1e-3:
            simplified.append(current)
    simplified.append(waypoints[-1])
    return simplified
