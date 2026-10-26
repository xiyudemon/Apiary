import math
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from AI.agent_service import AgentService, _local_drone_count
from core.backend import ApiaryBackend
from core import pathplan, zones
from core.simulation import ARRIVAL_TOLERANCE, WORLD_BOUND, SimulationEngine
from core.task_pool import (
    MAX_DRONES_PER_TASK,
    PRIORITY_KEYS,
    Task,
    TaskPool,
    _coerce_drone_count,
    label_to_key,
    may_borrow_signal,
    may_interrupt,
    normalize_priority,
    priority_label,
    priority_rank,
)
from core.taskdb import TaskDB


class FakeAI:
    def __init__(self):
        self.calls = 0
        self.user_task_calls = 0
        self.last_user_request = None

    def parse_user_task(self, raw_text, *, requested_by=None, user_level=None):
        self.user_task_calls += 1
        # 真实 Ollama 会把自然语言里的坐标原样写进 to_location，这里保持同样行为。
        coordinate = re.search(r"\(\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\)", raw_text)
        to_location = coordinate.group(0) if coordinate else "task_A"
        return {
            "status": "pending",
            "task_id": f"task_auto_{self.user_task_calls}",
            "raw_text": raw_text,
            "action": "new_task",
            "priority": "normal",
            "target": to_location,
            "to_location": to_location,
            "from_location": None,
            "task_type": "recon",
            "constraints": [],
            "requested_by": requested_by,
            "user_level": user_level,
        }

    def parse_simulation_strategy(self, observations, task_pool, user_request=None):
        self.calls += 1
        self.last_user_request = user_request
        return {
            "status": "ok",
            "strategy": "prioritize",
            "assignments": {},
            "avoidance": ["prefer_better_signal_zone"],
            "reason": "test_mode",
            "task_order": [],
        }


class FakeTaskDB:
    def __init__(self):
        self.tasks = {}
        self.commits = 0
        self.flushes = 0

    def add_or_update_task(self, task, *, commit=True):
        self.tasks[task["task_id"]] = dict(task)
        if commit:
            self.commits += 1

    def flush_tasks(self, tasks):
        self.flushes += 1
        for task in tasks:
            self.add_or_update_task(task, commit=False)
        self.commits += 1

    def commit(self):
        self.commits += 1

    def list_tasks(self):
        return list(self.tasks.values())

    def update_priority(self, task_id, priority):
        if task_id not in self.tasks:
            return False
        self.tasks[task_id]["priority"] = priority
        return True

    def update_status(self, task_id, status):
        if task_id not in self.tasks:
            return False
        self.tasks[task_id]["status"] = status
        return True

    def reset(self):
        self.tasks.clear()


def _make_backend():
    """构造一个不依赖 Ollama / SQLite 的后端实例，字段与 ApiaryBackend.__init__ 保持一致。"""
    backend = ApiaryBackend.__new__(ApiaryBackend)
    backend.task_pool = TaskPool()
    backend.simulation = SimulationEngine()
    backend.ai = FakeAI()
    backend.task_db = FakeTaskDB()
    backend.last_ai_call_ts = 0.0
    backend.ai_enabled = True
    backend.ai_avoidance_enabled = False
    backend._last_summary_refresh = 0.0
    backend._ensure_runtime_state()
    backend.runtime = {
        "speed": 1.0,
        "drone_count": 3,
        "task_count": 4,
        "last_action": "idle",
        "ai_known_cells": 0,
        "ai_unknown_cells": 0,
        "ai_enabled": True,
        "ai_avoidance_enabled": False,
    }
    backend.event_trace = []
    backend.pipeline_state = {"current_stage": "观测", "steps": [], "message": ""}
    # 事件驱动调度状态（与 ApiaryBackend.__init__ 保持一致）
    backend._last_status_signature = None
    backend._last_request_signature = None
    backend.reschedule_reason = "initial"
    backend.reschedule_count = 0
    backend.last_heatmap_payload = {}
    backend.ai_dispatch_count = 0
    return backend


def _prime_signatures(backend):
    """把调度指纹预置为"当前状态"，用于验证"没有变化就不会重新调度"。"""
    backend._last_status_signature = backend._status_signature()
    backend._last_request_signature = backend._request_signature()


def _tasks_of(backend):
    return [task.to_dict() for task in backend.task_pool.get_sorted_tasks()]


def test_manual_force_ai_calls_ai_once():
    backend = _make_backend()
    backend.tick_simulation(force_ai=True)
    assert backend.ai.calls == 1


def test_cooldown_skips_ai_for_regular_tick():
    backend = _make_backend()
    backend.tick_simulation(force_ai=True)
    backend.tick_simulation(force_ai=False)
    assert backend.ai.calls == 1


def test_tick_without_any_change_keeps_scheduler_idle():
    """事件驱动：状态/请求/待办三个指纹都没变化时，既不调用 AI 也不重新分配。"""
    backend = _make_backend()
    backend.task_pool.add_task(Task(task_id="task_default_1", raw_text="巡检", target="task_A"))
    backend.task_pool.tasks["task_default_1"].status = "in_progress"
    for drone in backend.simulation.drones.values():
        drone.status = "moving"
        drone.task_id = "task_default_1"
    _prime_signatures(backend)

    result = backend.tick_simulation(force_ai=False)

    assert backend.ai.calls == 0
    assert result["reschedule_reason"] is None
    assert result["source"] == "local"


def test_non_urgent_never_interrupts_in_flight_work():
    """非紧急任务只能排队等待空闲无人机，绝不打断正在执行的任务。"""
    backend = _make_backend()
    backend.task_pool.add_task(Task(task_id="task_default_1", raw_text="巡检", target="task_A", priority="low"))
    drones = list(backend.simulation.drones.values())
    backend.simulation.bind_task(drones[0], task_id="task_default_1", task_end=(120, -120), priority="low")
    backend.task_pool.tasks["task_default_1"].status = "in_progress"
    _prime_signatures(backend)

    backend.task_pool.add_task(Task(task_id="task_default_2", raw_text="巡检", target="task_B", priority="normal"))
    backend.mark_requests_dirty()
    result = backend.tick_simulation(force_ai=False)

    assert drones[0].task_id == "task_default_1", "低等级任务被非紧急任务打断"
    assert drones[0].status not in {"blocked", "holding"}
    assert backend.task_pool.tasks["task_default_1"].status == "in_progress"
    assert result["assigned"] == ["task_default_2"]


def test_ai_is_retriggered_by_a_request_update():
    """请求指纹变化（新任务入池）必须重新触发 AI 调度。"""
    backend = _make_backend()
    backend.task_pool.add_task(Task(task_id="task_default_1", raw_text="巡检", target="task_A"))
    for drone in backend.simulation.drones.values():
        drone.status = "moving"
        drone.task_id = "task_default_1"
    _prime_signatures(backend)
    # Normalize legacy manually-bound test state before capturing signatures.
    backend._settle_tasks()
    _prime_signatures(backend)
    assert backend.tick_simulation(force_ai=False)["reschedule_reason"] is None

    backend.task_pool.add_task(Task(task_id="task_default_2", raw_text="新请求", target="task_B"))
    backend.mark_requests_dirty()
    result = backend.tick_simulation(force_ai=False)

    assert backend.ai.calls == 1
    assert "request_update" in result["reschedule_reason"]


def test_runtime_settings_and_event_trace_are_available():
    backend = _make_backend()
    backend.configure_runtime(speed=1.5, drone_count=5, task_count=6)

    assert backend.runtime["speed"] == 1.5
    assert len(backend.simulation.drones) == 5
    assert len(backend.simulation.task_targets) >= 6

    backend.record_event("drone_1", "avoidance", "reroute", "避障高噪声区")
    assert backend.get_event_trace()[-1]["action"] == "avoidance"


def test_tick_keeps_movement_direct_without_route_cache():
    """规划出的路线必须存在、以目标块结尾，且不穿过紫色禁入区块。"""
    sim = SimulationEngine()
    drone = next(iter(sim.drones.values()))
    drone.task_id = "task_default_1"
    drone.status = "assigned"

    sim.tick()

    assert drone.planned_route, "未生成规划路线"
    assert drone.planned_route[-1] == pytest.approx(sim.resolve_task_target("task_default_1"))
    purple = {block for block in drone.planned_route if sim._local_zone(block) == zones.PURPLE}
    assert not purple, f"规划路线穿过紫色禁入区：{purple}"


def test_drones_start_from_base_station():
    sim = SimulationEngine(drone_count=3)
    for drone in sim.drones.values():
        assert abs(drone.x) < 1e-6
        assert abs(drone.y) < 1e-6


def test_unknown_targets_are_mapped_to_known_map_positions():
    sim = SimulationEngine(drone_count=2)
    target = sim.resolve_task_target("task_unknown_88")
    assert target in sim.task_targets.values()


def test_strategy_does_not_assign_same_task_to_two_drones():
    sim = SimulationEngine(drone_count=2)
    sim.apply_strategy({
        "status": "ok",
        "assignments": {
            "drone_1": "task_default_1",
            "drone_2": "task_default_1",
        },
    })

    assert sum(1 for drone in sim.drones.values() if drone.task_id == "task_default_1") <= 1


def test_repeated_assignment_events_are_deduplicated_for_same_drone_task():
    backend = _make_backend()
    backend.record_event("drone_1", "assignment", "task_select", "接取 task_default_1")
    backend.record_event("drone_1", "assignment", "task_select", "接取 task_default_1")

    assert sum(
        1 for event in backend.get_event_trace()
        if event["drone_id"] == "drone_1" and event["detail"] == "接取 task_default_1"
    ) == 1


# ---------------------------------------------------------------------------
# 回归测试：覆盖本次修复的缺陷
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", ["task_default_1", "task_default_2", "task_default_3", "task_default_4"])
def test_drone_reaches_every_default_task_target(task_id):
    """修复前 task_default_2/3/4 永远无法抵达（避障会完全取代趋近）。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    drone.task_id = task_id
    drone.status = "assigned"
    target = sim.resolve_task_target(task_id)

    for _ in range(4000):
        sim.tick()
        if drone.status == "returning":
            break

    assert drone.status == "returning", f"{task_id} 未能在 4000 tick 内抵达"
    assert math.hypot(drone.x - target[0], drone.y - target[1]) <= ARRIVAL_TOLERANCE


def test_single_drone_movement_is_not_a_limit_cycle():
    """修复前无人机会在红/紫区反复打转，永远无法进入返航状态（局部极大值死循环）。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    drone.task_id = "task_default_1"
    drone.status = "assigned"

    positions = set()
    ticks = 0
    while ticks < 200 and drone.status != "returning":
        sim.tick()
        ticks += 1
        positions.add((round(drone.x, 3), round(drone.y, 3)))

    assert drone.status == "returning", "无人机在 200 tick 内未能脱离局部极大值"
    # 巡航阶段几乎每一步都应产生新位置；允许极少数在原地规避的 tick。
    assert len(positions) >= ticks - 2


def test_concurrent_drones_keep_separation_mid_flight():
    """修复前多台无人机最终会重叠到同一个坐标上（间距为 0）。"""
    sim = SimulationEngine(drone_count=4)
    task_ids = ["task_default_1", "task_default_2", "task_default_3", "task_default_4"]
    for drone_id, task_id in zip(sorted(sim.drones), task_ids):
        sim.drones[drone_id].task_id = task_id
        sim.drones[drone_id].status = "assigned"

    min_separation = float("inf")
    for _ in range(200):
        sim.tick()
        # 只统计已离场的无人机；起飞前它们都停在基地同一点，归巢后也会重新重合。
        airborne = [d for d in sim.drones.values() if math.hypot(d.x, d.y) > 30.0]
        for idx, first in enumerate(airborne):
            for second in airborne[idx + 1:]:
                min_separation = min(min_separation, math.hypot(first.x - second.x, first.y - second.y))

    assert min_separation > 1.0, "巡航阶段出现无人机重叠"


@pytest.mark.parametrize("key", ["task_default_abc", "task_generated_xyz", "task_default_", "", "task_mystery_99"])
def test_resolve_task_target_falls_back_for_non_numeric_keys(key):
    """修复前非数字后缀会抛 ValueError，导致整个 tick 中断。"""
    sim = SimulationEngine(drone_count=1)
    target = sim.resolve_task_target(key)
    assert target in set(sim.task_targets.values()) | {sim.base_station}


def test_apply_strategy_uses_assignee_task_type():
    sim = SimulationEngine(drone_count=2)
    sim.apply_strategy({
        "status": "ok",
        "assignments": {"drone_1": "task_default_3"},
        "task_types": {"drone_1": "transport"},
    })

    assert sim.drones["drone_1"].task_type == "transport"


def test_ai_is_not_called_while_scheduler_is_idle():
    backend = _make_backend()
    backend.task_pool.add_task(Task(task_id="task_default_1", raw_text="巡检", target="task_A"))
    backend.task_pool.tasks["task_default_1"].status = "in_progress"
    for drone in backend.simulation.drones.values():
        drone.task_id = "task_default_1"
        drone.x, drone.y = 100.0, 100.0
    _prime_signatures(backend)

    result = backend.tick_simulation(force_ai=False)

    assert backend.ai.calls == 0
    # 任务都已在执行、也没有空闲执行者，因此不需要重新调度。
    assert result["reschedule_reason"] is None


def test_taskdb_accepts_in_memory_database():
    """修复前 TaskDB(":memory:") 会因每次操作重开连接而丢表。"""
    db = TaskDB(":memory:")
    try:
        db.add_or_update_task({
            "task_id": "task_mem_1",
            "raw_text": "内存任务",
            "action": "new_task",
            "priority": "high",
            "target": "task_A",
            "constraints": ["avoid_weak_signal_zone"],
            "status": "pending",
            "task_type": "transport",
            "from_location": "task_C",
            "to_location": "task_D",
            "return_to_base": False,
            "is_special": True,
        })

        stored = db.list_tasks()
        assert len(stored) == 1
        assert stored[0]["task_type"] == "transport"
        assert stored[0]["constraints"] == ["avoid_weak_signal_zone"]
        assert stored[0]["return_to_base"] is False
        assert stored[0]["is_special"] is True

        assert db.update_status("task_mem_1", "completed") is True
        assert db.list_tasks()[0]["status"] == "completed"
    finally:
        db.close()


def test_backend_assigns_tasks_and_reports_completion():
    """端到端：本地调度（AI 关闭）下任务应最终进入 completed。"""
    backend = _make_backend()
    backend.configure_runtime(speed=1.0, drone_count=4, task_count=4)
    _prime_signatures(backend)

    completed_at = None
    for tick in range(600):
        backend.tick_simulation(force_ai=False)
        if all(task.status == "completed" for task in backend.task_pool.tasks.values()):
            completed_at = tick
            break

    assert backend.ai.calls == 0, "本地调度不应调用 AI"
    assert completed_at is not None, "任务未收敛，可能再次出现任务反复易手的活锁"
    assert all(task.status == "completed" for task in backend.task_pool.tasks.values())
    assert completed_at < 600


def test_backend_converges_with_duplicate_target_points():
    """默认场景中 C->D 运输与 D 点巡检指向同一点，任务仍必须收敛（不得反复易手）。

    这是回归测试：修复前 4 台无人机跑 2000 tick 完成任务数为 0。
    """
    backend = _make_backend()
    backend._load_default_tasks()
    backend.configure_runtime(speed=1.0, drone_count=4, task_count=4)
    _prime_signatures(backend)

    duplicate_targets = [t.target for t in backend.task_pool.tasks.values()]
    assert len(duplicate_targets) != len(set(duplicate_targets)), "该场景应存在重复目标点"

    completed_at = None
    for tick in range(1200):
        backend.tick_simulation(force_ai=False)
        if all(task.status == "completed" for task in backend.task_pool.tasks.values()):
            completed_at = tick
            break

    assert completed_at is not None, "存在重复目标点时任务未收敛"


def test_missing_task_target_falls_back_to_real_target():
    """缺失目标点必须回退到真实任务点，回退到基地会导致瞬间"已抵达"。"""
    backend = _make_backend()
    base = backend.simulation.base_station
    for bad_target in (None, "", "   "):
        resolved = backend.simulation.resolve_task_target(bad_target)
        assert resolved != base
        assert resolved in backend.simulation.task_targets.values()


# ---------------------------------------------------------------------------
# 四级任务等级
# ---------------------------------------------------------------------------


def test_priority_model_exposes_four_ordered_tiers():
    """等级必须恰好四档：紧急 > 急迫 > 一般 > 常规。"""
    assert PRIORITY_KEYS == ("urgent", "high", "normal", "low")
    assert [priority_rank(key) for key in PRIORITY_KEYS] == [4, 3, 2, 1]
    assert [priority_label(key) for key in PRIORITY_KEYS] == ["紧急", "急迫", "一般", "常规"]


@pytest.mark.parametrize("label,key", [("紧急", "urgent"), ("急迫", "high"), ("一般", "normal"), ("常规", "low")])
def test_chinese_priority_labels_are_accepted(label, key):
    """管理员下拉框给出的是中文标签，不能被静默降级成"一般"。"""
    assert label_to_key(label) == key
    assert normalize_priority(label) == key


def test_only_urgent_may_interrupt_or_borrow_signal():
    assert may_interrupt("紧急") is True
    assert may_borrow_signal("urgent") is True
    for other in ("急迫", "一般", "常规"):
        assert may_interrupt(other) is False, f"{other} 不应打断在飞任务"
        assert may_borrow_signal(other) is False, f"{other} 不应借用集群信号"


def test_legacy_priority_aliases_still_normalize():
    assert normalize_priority("critical") == "urgent"
    assert normalize_priority("medium") == "normal"
    assert normalize_priority(None) == "normal"


def test_task_requires_an_end_location():
    """任务必须有结束地点：缺失时用 target 补齐，再缺失则用任务编号兜底。"""
    pool = TaskPool()
    task = pool.add_task(Task(task_id="task_default_9", raw_text="巡检", target="task_B"))
    assert task.end_location == "task_B"

    orphan = pool.add_task(Task(task_id="task_orphan_1", raw_text="无目标"))
    assert orphan.end_location == "task_orphan_1"

    # 出发地点可选：为空表示使用执行任务的无人机自身位置。
    assert task.start_location is None


def test_start_location_defaults_to_the_executing_drone():
    """未给出出发地点时，起点默认为无人机自身位置。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    drone.x, drone.y = 33.0, -47.0
    sim.bind_task(drone, task_id="task_default_1", task_end=(120, -120), priority="normal")

    assert drone.task_start == pytest.approx((33.0, -47.0))


# ---------------------------------------------------------------------------
# 四档信号分区与紫色禁入
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strength,zone", [
    (100.0, zones.GREEN),
    (80.0, zones.GREEN),
    (79.9, zones.YELLOW),
    (55.0, zones.YELLOW),
    (54.9, zones.RED),
    (30.0, zones.RED),
    (29.9, zones.PURPLE),
    (0.0, zones.PURPLE),
])
def test_zone_thresholds_are_four_tiered(strength, zone):
    assert zones.zone_of(strength) == zone


def test_purple_is_forbidden_for_every_priority_including_urgent():
    for zone in (zones.GREEN, zones.YELLOW, zones.RED):
        assert zones.is_forbidden(zone) is False
    assert zones.is_forbidden(zones.PURPLE) is True
    assert zones.must_hold(zones.RED) is True
    assert zones.must_hold(zones.GREEN) is False


def test_plan_route_avoids_purple_blocks_unless_it_is_the_goal():
    """紫色危险区块不可通行；但目标点所在区块必须可进入，否则红区目标永远无法抵达。"""
    purple_column = {block: zones.PURPLE for block in pathplan.NEIGHBOURS}
    lookup = lambda block: purple_column.get((block[0], block[1]))

    # 目标块本身是紫区 -> 允许进入（目的地豁免）。
    assert pathplan.plan_route((0, 0), (18, 0), lookup) == []

    # 途经的紫区不可穿越 -> 返回直线兜底，而不是穿墙。
    assert pathplan.plan_route((0, 0), (90, 0), lookup) == []


def test_heatmap_version_only_advances_on_new_or_changed_blocks():
    """GUI 依赖版本号决定是否重绘：重复观测同一分区不应触发重绘。"""
    sim = SimulationEngine(drone_count=1)
    start_version = sim.heatmap_version

    sim.record_heatmap_sample(200.0, 200.0)
    after_first = sim.heatmap_version
    assert after_first > start_version

    sim.record_heatmap_sample(200.0, 200.0)
    assert sim.heatmap_version == after_first, "同一区块同一分区不应推进版本号"

    sim.record_heatmap_sample(200.5, 200.5, zones.PURPLE)
    assert sim.heatmap_version == after_first + 1, "分区变化必须推进版本号"


def test_heatmap_version_advances_when_a_new_block_is_observed():
    sim = SimulationEngine(drone_count=1)
    sim.record_heatmap_sample(-200.0, -200.0)
    first = sim.heatmap_version
    sim.record_heatmap_sample(200.0, 200.0)
    assert sim.heatmap_version == first + 1


def test_heatmap_payload_reports_version_and_zone_counts():
    sim = SimulationEngine(drone_count=1)
    for index in range(4):
        sim.record_heatmap_sample(index * 40.0, index * 40.0)

    payload = sim.get_heatmap_payload()

    assert payload["heatmap_version"] == sim.heatmap_version
    assert payload["observed_blocks"] == len(sim.observed_heatmap)
    assert set(payload["zone_counts"]) == set(zones.ZONE_ORDER)
    assert sum(payload["zone_counts"].values()) == payload["observed_blocks"]
    assert len(payload["blocks"]) == payload["observed_blocks"]


# ---------------------------------------------------------------------------
# 信号借用：紧急任务 3×3，危险区域不可借用
# ---------------------------------------------------------------------------


def test_only_urgent_drones_borrow_a_3x3_signal_window():
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]

    sim.bind_task(drone, task_id="task_default_1", task_end=(120, -120), priority="urgent")
    sim._observe(drone)
    assert drone.borrow_radius == 1.0
    assert drone.borrowed_signal is True

    sim.bind_task(drone, task_id="task_default_2", task_end=(-150, 80), priority="normal")
    sim._observe(drone)
    assert drone.borrow_radius == 0.0
    assert drone.borrowed_signal is False


def test_borrowed_signal_never_comes_from_a_purple_block():
    """危险区域区块不参与信号借用，否则无人机可以把危险区当成"强信号"。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    # 该点的 3×3 窗口内有危险区域区块，且自身不在危险区域内（危险区域严禁进入）。
    drone.x, drone.y = -216.0, -180.0
    assert not zones.is_forbidden(sim.signal_field.zone_at(drone.x, drone.y))
    sim.bind_task(drone, task_id="task_default_1", task_end=(120, -120), priority="urgent")

    sim._observe(drone)

    samples = sim.block_observations(drone.x, drone.y, 1)
    assert len(samples) == 9, "紧急任务必须观测完整的 3×3 区块"
    usable = [
        float(sample["signal_strength"])
        for sample in samples
        if not zones.is_forbidden(str(sample["zone"]))
    ]
    # Hazard sampling is deterministic but field layouts may vary by runtime.
    # 紫色是强度最低的一档：借用结果只能来自可通行区块。
    assert drone.signal_strength == pytest.approx(max(usable))
    assert drone.borrowed_signal is True


# ---------------------------------------------------------------------------
# 自然语言请求入口
# ---------------------------------------------------------------------------


def test_admin_selected_priority_overrides_the_ai_inference():
    """管理员在列表里选择的等级优先于 AI 推断。"""
    backend = _make_backend()

    task = backend.submit_user_request("请优先巡查 A 点", requested_by="admin", user_level="admin", priority="紧急")

    assert task["priority"] == "urgent"
    assert task["priority_label"] == "紧急"
    assert task["is_special"] is True
    assert backend.task_pool.tasks[task["task_id"]].priority == "urgent"


def test_user_request_without_priority_uses_the_ai_inference():
    backend = _make_backend()

    task = backend.submit_user_request("帮我看看 B 点", requested_by="alice", user_level="standard")

    assert task["priority"] == "normal"
    assert task["requested_by"] == "alice"
    assert task["user_level"] == "standard"


def test_user_request_always_ends_up_with_an_end_location():
    backend = _make_backend()

    task = backend.submit_user_request("派一架过去", requested_by="bob", user_level="standard")

    assert task["to_location"]
    assert backend.task_pool.tasks[task["task_id"]].end_location == task["to_location"]


def test_submit_user_request_marks_the_scheduler_dirty():
    """新请求必须让下一次 tick 重新调度（请求指纹被重置）。"""
    backend = _make_backend()
    backend.configure_runtime(speed=1.0, drone_count=2, task_count=2)
    _prime_signatures(backend)

    backend.submit_user_request("新增一次巡检", requested_by="admin", user_level="admin", priority="急迫")
    result = backend.tick_simulation(force_ai=False)

    assert "request_update" in result["reschedule_reason"]


def test_updating_a_task_priority_requeues_the_scheduler():
    backend = _make_backend()
    backend.task_pool.add_task(Task(task_id="task_default_1", raw_text="巡检", target="task_A"))
    backend.task_db.add_or_update_task(backend.task_pool.tasks["task_default_1"].to_dict())
    _prime_signatures(backend)

    assert backend.update_task_priority("task_default_1", "紧急") is True

    assert backend.task_pool.tasks["task_default_1"].priority == "urgent"
    assert backend.task_pool.tasks["task_default_1"].is_special is True


# ---------------------------------------------------------------------------
# 分配目标：负载均衡 -> 路程最短 -> 限制绕路
# ---------------------------------------------------------------------------


def test_assignment_cost_prefers_the_least_loaded_drone():
    backend = _make_backend()
    drones = sorted(backend.simulation.drones)
    loads = {drones[0]: 0, drones[1]: 2, drones[2]: 0}

    ordered = sorted(
        drones,
        key=lambda drone_id: backend._assignment_cost(
            backend.simulation.drones[drone_id], start=(0.0, 0.0), load=loads[drone_id]
        ),
    )

    assert ordered[0] in {drones[0], drones[2]}, "应优先选择负载更低的无人机"
    assert ordered[-1] == drones[1]


def test_assignment_cost_breaks_ties_by_distance_then_detour():
    backend = _make_backend()
    drones = sorted(backend.simulation.drones)
    near, far = drones[0], drones[1]
    backend.simulation.drones[near].x, backend.simulation.drones[near].y = 10.0, 0.0
    backend.simulation.drones[far].x, backend.simulation.drones[far].y = 100.0, 0.0

    near_cost = backend._assignment_cost(backend.simulation.drones[near], start=(0.0, 0.0), load=0)
    far_cost = backend._assignment_cost(backend.simulation.drones[far], start=(0.0, 0.0), load=0)

    assert near_cost[:2] < far_cost[:2], "负载相同时应选择离起点更近的无人机"


def test_pending_tasks_are_drained_without_a_signature_change():
    """回归：无人机归巢后任务仍未派出，仅靠指纹变化检测会导致任务永久滞留。"""
    backend = _make_backend()
    backend.configure_runtime(speed=1.0, drone_count=3, task_count=1)
    for drone in backend.simulation.drones.values():
        drone.status = "idle"
        drone.task_id = None
    backend.task_pool.add_task(Task(task_id="task_default_1", raw_text="巡检", target="task_A"))
    _prime_signatures(backend)

    # 第一 tick 分配任务并让指纹进入稳态，此后不再有"变化"。
    backend.tick_simulation(force_ai=False)
    assert backend._queue_drain_required() is False, "任务已被派出后不应继续排队"

    holder = next(d for d in backend.simulation.drones.values() if d.task_id == "task_default_1")
    backend._settle_tasks()
    backend.simulation.bind_task(holder, task_id=None)
    holder.task_id = None
    holder.status = "idle"
    backend.task_pool.tasks["task_default_1"].status = "pending"

    assert backend._queue_drain_required() is True
    assert backend.drain_reschedule_reason() is not None


# ---------------------------------------------------------------------------
# 自然语言地点解析：AI 会把坐标原样写进 to_location
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("(150, 60)", (150.0, 60.0)),
        ("150, 60", (150.0, 60.0)),
        ("(-60,120)", (-60.0, 120.0)),
        (" （-60，120） ", (-60.0, 120.0)),
        ("(0.5, -0.25)", (0.5, -0.25)),
    ],
)
def test_coordinate_strings_are_resolved_to_that_point(text, expected):
    """修复前 "(150, 60)" 会被静默回退到默认目标点，任务跑到完全错误的位置。"""
    sim = SimulationEngine(drone_count=1)

    assert sim.resolve_task_target(text) == pytest.approx(expected)


def test_coordinate_strings_are_clamped_to_the_sandbox():
    """越界坐标必须夹取到沙盘范围内，否则无人机会永远飞不到目标。"""
    sim = SimulationEngine(drone_count=1)

    assert sim.resolve_task_target("(5000, -5000)") == pytest.approx((WORLD_BOUND, -WORLD_BOUND))


@pytest.mark.parametrize("alias", ["基地", "总部", "起飞点", "基站", "base"])
def test_base_aliases_resolve_to_the_base_station(alias):
    """docstring 声称支持"基地"，实际却回退到任务点，这里固定为基站本身。"""
    sim = SimulationEngine(drone_count=1)

    assert sim.resolve_task_target(alias) == sim.base_station


def test_natural_language_request_reaches_the_requested_coordinates():
    """端到端：AI 解析出的坐标地点必须真的成为无人机的目标点。"""
    backend = _make_backend()
    task = backend.submit_user_request("紧急：前往 (150, 60) 侦察", requested_by="admin", user_level="管理员")

    assert backend.simulation.resolve_task_target(task["to_location"]) == pytest.approx((150.0, 60.0))


# ---------------------------------------------------------------------------
# 任务完成后原地待命（不无脑返航）
# ---------------------------------------------------------------------------


def test_default_tasks_do_not_return_to_base():
    """无人机在任务点待命，等下一个任务；无脑返航会让待命机永远停在基站。"""
    backend = _make_backend()
    backend._load_default_tasks()

    assert backend.task_pool.tasks, "默认任务应被载入"
    for task in backend.task_pool.tasks.values():
        assert task.return_to_base is False, f"{task.task_id} 不应无脑返航"


def test_task_pool_prioritizes_unfinished_tasks_before_completed_ones():
    """未完成任务应排在已完成任务前面，再按优先级排序。"""
    pool = TaskPool()
    pool.add_task(Task(task_id="task_done_urgent", raw_text="done urgent", priority="urgent", target="task_A", status="completed"))
    pool.add_task(Task(task_id="task_open_low", raw_text="open low", priority="low", target="task_B", status="pending"))
    pool.add_task(Task(task_id="task_open_high", raw_text="open high", priority="high", target="task_C", status="pending"))
    pool.add_task(Task(task_id="task_done_low", raw_text="done low", priority="low", target="task_D", status="completed"))

    ordered = [task.task_id for task in pool.get_sorted_tasks()]

    assert ordered[:2] == ["task_open_high", "task_open_low"]
    assert ordered[-2:] == ["task_done_urgent", "task_done_low"]


def test_default_tasks_loaded_from_db_are_reset_to_pending():
    """数据库里旧的默认任务如果是 completed，重新进入时也应恢复为未完成。"""
    backend = _make_backend()
    backend.task_pool = TaskPool()
    backend.task_db = TaskDB(":memory:")
    backend.task_db.flush_tasks([
        {
            "task_id": "task_default_1",
            "raw_text": "旧任务",
            "action": "new_task",
            "priority": "high",
            "target": "task_A",
            "constraints": [],
            "status": "completed",
            "task_type": "recon",
            "from_location": None,
            "to_location": "task_A",
            "return_to_base": False,
            "is_special": False,
            "requested_by": "admin",
            "user_level": "管理员",
            "required_drones": 1,
            "all_drones": False,
        }
    ])
    backend.reset_tasks_on_startup = True

    backend._prepare_startup_tasks()

    assert "task_default_1" in backend.task_pool.tasks
    assert backend.task_pool.tasks["task_default_1"].status == "pending"


def test_drone_holds_position_after_reaching_the_target():
    """抵达终点后进入 awaiting 且原地不动，而不是掉头返航。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    target = sim.resolve_task_target("task_default_1")
    sim.bind_task(drone, task_id="task_default_1", task_end=target, priority="low", return_to_base=False)

    for _ in range(4000):
        sim.tick()
        if drone.status == "awaiting":
            break

    assert drone.status == "awaiting", f"未进入待命状态：{drone.status}"
    assert drone.returning_to_base is False
    assert math.hypot(drone.x - target[0], drone.y - target[1]) <= ARRIVAL_TOLERANCE

    resting = (drone.x, drone.y)
    for _ in range(30):
        sim.tick()
    assert (drone.x, drone.y) == resting, "待命期间不应发生位移"


def test_return_to_base_flag_still_returns_the_drone():
    """显式要求返航时，原有返航语义必须保留。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    target = sim.resolve_task_target("task_default_1")
    sim.bind_task(drone, task_id="task_default_1", task_end=target, priority="low", return_to_base=True)

    for _ in range(4000):
        sim.tick()
        if drone.status == "returning":
            break
    assert drone.status == "returning"

    for _ in range(4000):
        sim.tick()
        if drone.status == "idle":
            break
    assert drone.status == "idle"
    assert drone.task_id is None, "归巢后应解绑任务"


def test_release_task_clears_the_flight_state():
    """release_task 必须清空任务绑定、路线与避障标记，待命机才能被重新分配。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    sim.bind_task(drone, task_id="task_default_1", task_end=(120, -120), priority="urgent", return_to_base=False)
    drone.planned_route = [(10.0, 10.0), (20.0, 20.0)]
    drone.route_index = 1
    drone.needs_replan = True
    drone.returning_to_base = True
    drone.borrowed_signal = True

    sim.release_task(drone)

    assert drone.task_id is None
    assert drone.task_end is None
    assert drone.planned_route == []
    assert drone.route_index == 0
    assert drone.needs_replan is False
    assert drone.returning_to_base is False
    assert drone.task_priority == "normal"


def test_completed_task_frees_the_drone_for_the_next_one():
    """端到端：任务完成 → 待命 → 新任务复用同一架无人机，且它并未先飞回基站。"""
    backend = _make_backend()
    backend.configure_runtime(speed=1.0, drone_count=1, task_count=0)
    backend._load_default_tasks()
    for task in list(backend.task_pool.tasks.values())[1:]:
        backend.task_pool.tasks.pop(task.task_id)
    _prime_signatures(backend)

    for _ in range(800):
        backend.tick_simulation(force_ai=False)
        if all(task.status == "completed" for task in backend.task_pool.tasks.values()):
            break
    assert all(task.status == "completed" for task in backend.task_pool.tasks.values())

    rested = backend.simulation.get_drones_state()
    assert rested[0]["status"] == "awaiting", "完成任务后应在原地待命"
    assert rested[0]["returning_to_base"] is False
    resting_spot = (rested[0]["x"], rested[0]["y"])
    assert math.hypot(*resting_spot) > 60.0, "待命点应是任务点而非基站"

    follow_up = backend._make_task(
        task_id="task_follow_up",
        raw_text="继续巡检 E 点",
        priority="normal",
        target="task_E",
        to_location="task_E",
    )
    backend.task_pool.add_task(follow_up)
    backend.mark_requests_dirty()

    for _ in range(200):
        backend.tick_simulation(force_ai=False)
        if follow_up.status == "in_progress":
            break

    assert follow_up.status == "in_progress", "新任务应被立即派出"
    holder = next(d for d in backend.simulation.drones.values() if d.task_id == "task_follow_up")
    assert math.hypot(holder.x, holder.y) > 60.0, "应复用待命机，而不是让它先返航回基站"
    assert math.hypot(holder.x - resting_spot[0], holder.y - resting_spot[1]) < 40.0, "待命机应从原地出发"


# ---------------------------------------------------------------------------
# 监控页无人机运行状态快照
# ---------------------------------------------------------------------------


def test_drones_state_exposes_monitoring_fields():
    """监控页列表需要位置/当前任务/等级/信号档/正在做什么，缺一不可。"""
    backend = _make_backend()
    backend.configure_runtime(speed=1.0, drone_count=2, task_count=1)
    state = backend.simulation.get_drones_state()

    assert len(state) == len(backend.simulation.drones)
    required = {
        "id", "x", "y", "position", "status", "status_label", "task_id", "busy_with",
        "signal_strength", "zone", "zone_label", "observed_zone_label", "borrowed_signal",
        "task_priority", "task_priority_label", "task_target", "return_to_base",
        "returning_to_base", "needs_replan", "route_length", "route_index",
    }
    for snapshot in state:
        assert required <= set(snapshot), f"缺少字段：{required - set(snapshot)}"
        assert snapshot["position"] == "({:.0f}, {:.0f})".format(snapshot["x"], snapshot["y"])
        assert snapshot["busy_with"], "正在做什么不能为空"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in snapshot["busy_with"]), "描述应为中文"


def test_drone_activity_description_tracks_the_task():
    """任务在飞时，"正在干嘛"必须带上任务编号与避障/借用细节。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    sim.bind_task(drone, task_id="task_default_1", task_end=(120, -120), priority="urgent")

    drone.status = "moving"
    snapshot = sim._drone_snapshot(drone)
    assert "task_default_1" in snapshot["busy_with"]

    drone.borrowed_signal = True
    assert "3×3" in sim._drone_snapshot(drone)["busy_with"]

    drone.borrowed_signal = False
    drone.status = "awaiting"
    assert "待命" in sim._drone_snapshot(drone)["busy_with"]


def test_status_labels_cover_every_drone_state():
    """新增状态若没有中文标签，监控页会直接把内部英文状态暴露给用户。"""
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    for status in ("idle", "awaiting", "assigned", "planning", "moving", "holding",
                   "avoiding", "exiting", "blocked", "returning"):
        drone.status = status
        snapshot = sim._drone_snapshot(drone)
        assert snapshot["status_label"] != status, f"{status} 缺少中文标签"


# ---------------------------------------------------------------------------
# 一条任务多架无人机（"所有无人机前往 A 点"）
# ---------------------------------------------------------------------------


def _add_multi_task(backend, task_id="task_multi", required=3, all_drones=False, priority="normal"):
    task = Task(
        task_id=task_id,
        raw_text="所有无人机前往 A 点",
        priority=priority,
        to_location="task_A",
        required_drones=required,
        all_drones=all_drones,
    )
    backend.task_pool.add_task(task)
    return task


def _holders(backend, task_id):
    return [d for d in backend.simulation.drones.values() if d.task_id == task_id]


def test_drone_quota_and_coercion_bounds():
    """机数必须收敛到 1..MAX_DRONES_PER_TASK，且 all_drones 取机队总数。"""
    assert _coerce_drone_count("3") == 3
    assert _coerce_drone_count(None) == 1
    assert _coerce_drone_count(True) == 1
    assert _coerce_drone_count(0) == 1
    assert _coerce_drone_count(999) == MAX_DRONES_PER_TASK

    single = Task(task_id="t_s", raw_text="x", required_drones=3)
    assert single.drone_quota(5) == 3
    assert single.drone_quota(2) == 2
    assert single.is_multi_drone is True

    every = Task(task_id="t_a", raw_text="x", all_drones=True)
    assert every.drone_quota(4) == 4
    persisted = every.to_dict()
    assert persisted["all_drones"] is True
    assert persisted["required_drones"] == 1


def test_non_urgent_multi_task_only_uses_idle_drones():
    """常规多机任务只派当前空闲机，不打断在飞任务；余额留待后续 tick 补派。"""
    backend = _make_backend()
    busy = Task(task_id="task_busy", raw_text="占位", priority="normal", to_location="task_D")
    backend.task_pool.add_task(busy)
    busy.status = "in_progress"
    backend.simulation.bind_task(
        backend.simulation.drones["drone_1"],
        task_id="task_busy",
        task_end=backend.simulation.resolve_task_target("task_D"),
        priority="normal",
    )
    backend.mark_requests_dirty()
    backend.tick_simulation()

    multi = _add_multi_task(backend, all_drones=True)
    backend.mark_requests_dirty()
    backend.tick_simulation()

    assert len(_holders(backend, "task_multi")) == 2, "只应派到 2 台空闲机"
    assert backend.simulation.drones["drone_1"].task_id == "task_busy", "不应打断在飞任务"
    assert multi.status == "in_progress"

    # 在飞任务结束后立刻原地待命 -> 下一代补派仍需满足 all_drones 的余额。
    freed = backend.simulation.drones["drone_1"]
    freed.status = "awaiting"
    backend._settle_tasks()
    assert freed.task_id is None and freed.status == "awaiting"
    assert busy.status == "completed"

    backend.mark_requests_dirty()
    backend.tick_simulation()
    assert len(_holders(backend, "task_multi")) == 3
    assert freed.task_id == "task_multi", "刚空出的无人机应被补派到未派满的任务上"


def test_urgent_multi_task_preempts_every_drone():
    """只有紧急任务可以抢占并把全部无人机调到同一条任务上。"""
    backend = _make_backend()
    low = Task(task_id="task_low", raw_text="低等级", priority="low", to_location="task_D")
    backend.task_pool.add_task(low)
    low.status = "in_progress"
    for drone in backend.simulation.drones.values():
        backend.simulation.bind_task(
            drone,
            task_id="task_low",
            task_end=backend.simulation.resolve_task_target("task_D"),
            priority="low",
        )

    urgent = _add_multi_task(backend, task_id="task_urgent", all_drones=True, priority="urgent")
    backend.mark_requests_dirty()
    backend.tick_simulation()

    assert len(_holders(backend, "task_urgent")) == 3
    assert low.status == "pending", "被抢占的任务应退回 pending 重新排队"
    assert urgent.status == "in_progress"


def test_multi_task_completes_only_when_all_holders_arrive():
    """多机任务必须全部持有者到位才算完成；先到者立即解绑可接下一任务。"""
    backend = _make_backend()
    multi = _add_multi_task(backend, task_id="task_settle")
    backend.mark_requests_dirty()
    backend.tick_simulation()

    attached = _holders(backend, "task_settle")
    assert len(attached) == 3
    target = backend.simulation.resolve_task_target("task_A")

    for drone in attached[:2]:
        drone.status = "awaiting"
        drone.x, drone.y = target
    backend._settle_tasks()

    assert multi.status == "in_progress", "部分到位不应结算"
    assert len(_holders(backend, "task_settle")) == 1, "先到者应已解绑"

    last = _holders(backend, "task_settle")[0]
    last.status = "awaiting"
    last.x, last.y = target
    backend._settle_tasks()

    assert multi.status == "completed"
    assert _holders(backend, "task_settle") == []


def test_multi_task_reports_assigned_once_per_task_id():
    """newly_assigned 是"任务编号"列表，多机协同仍只应出现一次。"""
    backend = _make_backend()
    backend.task_pool.tasks.clear()
    backend.configure_runtime(drone_count=4)
    _add_multi_task(backend, task_id="task_once", required=4)
    strategy = {"assignments": {}}

    assigned = backend._assign_tasks(strategy)

    assert assigned == ["task_once"]
    assert len(strategy["assignments"]) == 4
    assert set(strategy["assignments"].values()) == {"task_once"}


def test_queue_drain_detects_partially_dispatched_multi_task():
    """已 in_progress 但没派满的多机任务仍必须触发补派，否则余额永久滞留。"""
    backend = _make_backend()
    backend.configure_runtime(drone_count=4)
    multi = _add_multi_task(backend, task_id="task_partial", required=4)
    backend.mark_requests_dirty()
    backend.tick_simulation()

    assert multi.status == "in_progress"
    assert len(_holders(backend, "task_partial")) == 4
    assert backend._queue_drain_required() is False, "已派满的任务不应再触发补派"

    # 人为制造"派到 2 台、还差 2 台"的部分派遣状态。
    holders = _holders(backend, "task_partial")
    for drone in holders[2:]:
        backend.simulation.release_task(drone, status="idle")
    assert len(_holders(backend, "task_partial")) == 2
    assert multi.status == "in_progress", "状态不能因为解绑而退回 pending"

    assert backend._queue_drain_required() is True, "未派满的多机任务必须能触发补派"

    # 补派后应恢复成 4 台，且不再需要补派。
    backend.mark_requests_dirty()
    backend.tick_simulation()
    assert len(_holders(backend, "task_partial")) == 4
    assert backend._queue_drain_required() is False


def test_local_drone_count_fallback_parsing():
    """本地兜底解析：即使模型漏填，文本里的"所有无人机 / 派 3 架"也要生效。"""
    assert _local_drone_count("所有无人机前往 A 点") == (1, True)
    assert _local_drone_count("每一架无人机前往 (100, 100)") == (1, True)
    assert _local_drone_count("集群出动，飞往 task_C") == (1, True)
    assert _local_drone_count("派 3 架无人机去 B 点") == (3, False)
    assert _local_drone_count("用 2 台飞机前往基地") == (2, False)
    assert _local_drone_count("三架无人机去 A 点") == (3, False)
    assert _local_drone_count("去 A 点看看") == (1, False)


class _StubOllama:
    """返回固定 JSON 的假 Ollama：用来复现"模型把多机字段填错"的场景。"""

    def __init__(self, payload):
        self.payload = payload

    def call(self, prompt, *, json_mode=True, options=None, schema=None):
        return dict(self.payload)


def test_model_cannot_downgrade_explicit_multi_drone_request():
    """文本里写了"所有无人机"，模型就算返回 all_drones=false 也不能把它降级成单机。"""
    stub = _StubOllama(
        {
            "status": "pending",
            "task_id": "task_auto_1",
            "action": "new_task",
            "priority": "normal",
            "target": "a点",
            "to_location": "a点",
            "from_location": None,
            "task_type": "recon",
            "constraints": [],
            # 实测模型会给出这两个"看起来合理"的默认值。
            "required_drones": 1,
            "all_drones": False,
        }
    )
    service = AgentService(ollama_client=stub)
    result = service.parse_user_task("所有无人机前往a点", requested_by="admin", user_level="管理员")

    assert result["status"] == "pending"
    assert result["all_drones"] is True, "显式的'所有无人机'必须保留为全机队任务"
    assert result["required_drones"] == 1


def test_model_still_wins_when_text_is_silent_about_drone_count():
    """文本没提机数时，模型给出的机数仍然要被采纳。"""
    stub = _StubOllama(
        {
            "status": "pending",
            "task_id": "task_auto_2",
            "action": "new_task",
            "priority": "high",
            "target": "task_B",
            "to_location": "task_B",
            "from_location": None,
            "task_type": "recon",
            "constraints": [],
            "required_drones": 4,
            "all_drones": False,
        }
    )
    service = AgentService(ollama_client=stub)
    result = service.parse_user_task("去 B 点侦察一下", requested_by="admin", user_level="管理员")

    assert result["required_drones"] == 4
    assert result["all_drones"] is False


def test_task_id_is_always_locally_generated_and_unique():
    """模型只会照抄骨架里的示例编号，任务号必须由本地生成且每次不同。"""
    stub = _StubOllama(
        {
            "status": "pending",
            "task_id": "task_user_1",  # 模型实际稳定返回的值
            "action": "new_task",
            "priority": "normal",
            "target": "task_A",
            "to_location": "task_A",
            "from_location": None,
            "task_type": "recon",
            "constraints": [],
            "required_drones": 1,
            "all_drones": False,
        }
    )
    service = AgentService(ollama_client=stub)
    first = service.parse_user_task("去 A 点", requested_by="admin", user_level="管理员")
    second = service.parse_user_task("去 B 点", requested_by="admin", user_level="管理员")

    assert first["task_id"] != "task_user_1", "不能沿用模型抄来的示例编号"
    assert first["task_id"] != second["task_id"], "连续两次请求必须拿到不同任务号"
    assert first["task_id"].startswith("task_auto_")
    assert second["task_id"].startswith("task_auto_")

