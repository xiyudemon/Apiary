from concurrent.futures import Future

import pytest

from test_backend_scheduler import _make_backend, _add_multi_task, _holders
from core.simulation import SimulationEngine
from core.task_pool import Task
from core.taskdb import TaskDB
from core import pathplan, zones


def test_completed_children_are_not_redispatched():
    backend = _make_backend()
    task = _add_multi_task(backend, required=3)
    backend._assign_tasks({})
    first, *remaining = _holders(backend, task.task_id)
    first.status = "awaiting"
    backend._settle_tasks()
    assert task.completed_children == 1
    assert task.to_dict()["progress"] == pytest.approx(1 / 3)
    backend._assign_tasks({})
    assert len(_holders(backend, task.task_id)) == 2
    for drone in remaining:
        drone.status = "awaiting"
    backend._settle_tasks()
    assert task.status == "completed"


def test_required_children_exceed_fleet_and_run_sequentially():
    backend = _make_backend()
    backend.configure_runtime(drone_count=1)
    task = _add_multi_task(backend, required=3)
    for count in range(3):
        backend._assign_tasks({})
        holder, = _holders(backend, task.task_id)
        holder.status = "awaiting"
        backend._settle_tasks()
        assert task.completed_children == count + 1
    assert task.status == "completed"


def test_all_drones_freezes_at_first_scheduling_and_resize_requeues():
    backend = _make_backend()
    task = _add_multi_task(backend, all_drones=True)
    backend._assign_tasks({})
    assert task.total_children == 3
    backend.configure_runtime(drone_count=1)
    backend._settle_tasks()
    assert task.total_children == 3
    backend._assign_tasks({})
    assert len(_holders(backend, task.task_id)) == 1


def test_interruption_restarts_child_at_original_origin():
    backend = _make_backend()
    task = _add_multi_task(backend, required=1)
    backend._assign_tasks({})
    holder, = _holders(backend, task.task_id)
    child = holder.child_id
    origin = task.child_origins[child]
    holder.x, holder.y = 45, 33
    backend._preempt(holder)
    backend._assign_tasks({})
    restarted, = _holders(backend, task.task_id)
    assert restarted.child_id == child
    assert restarted.task_start == origin
    assert restarted.task_phase == "start"


def test_transport_visits_start_before_end_without_completing_at_pickup():
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    sim.bind_task(drone, task_id="transport", task_type="transport", task_start=(20, 0), task_end=(60, 0), return_to_base=False)
    assert sim.task_goal(drone) == (20, 0)
    drone.x, drone.y = 20, 0
    drone.planned_route = [(20, 0)]
    sim._finish_leg(drone)
    assert drone.task_phase == "end"
    assert sim.task_goal(drone) == (60, 0)
    assert drone.status == "assigned"


def test_ai_cannot_preempt_nonurgent_or_overfill_quota():
    backend = _make_backend()
    low = Task(task_id="low", raw_text="low", priority="low", target="task_D", required_drones=3)
    backend.task_pool.add_task(low)
    backend._assign_tasks({})
    normal = Task(task_id="normal", raw_text="normal", target="task_A")
    backend.task_pool.add_task(normal)
    before = [(d.task_id, d.execution_version) for d in backend.simulation.drones.values()]
    backend._apply_ai_assignments({"status": "ok", "assignments": {key: "normal" for key in backend.simulation.drones}})
    assert before == [(d.task_id, d.execution_version) for d in backend.simulation.drones.values()]
    assert not _holders(backend, "normal")


def test_fifo_is_independent_of_display_id_and_special_flag():
    backend = _make_backend()
    backend.configure_runtime(drone_count=1)
    first = Task(task_id="aaa", raw_text="first", target="task_A")
    later = Task(task_id="zzz", raw_text="later", target="task_B", is_special=True)
    backend.task_pool.add_task(first)
    backend.task_pool.add_task(later)
    backend._assign_tasks({})
    assert backend.simulation.drones["drone_1"].task_id == "aaa"


def test_unchanged_task_state_does_not_write_database():
    backend = _make_backend()
    backend._flush_dirty_tasks()
    task = _add_multi_task(backend)
    backend._flush_dirty_tasks()
    flushes = backend.task_db.flushes
    backend._flush_dirty_tasks()
    assert backend.task_db.flushes == flushes
    task.priority = "high"
    backend._flush_dirty_tasks()
    assert backend.task_db.flushes == flushes + 1


def test_child_state_round_trips_and_close_is_idempotent():
    db = TaskDB(":memory:")
    task = Task(task_id="x", raw_text="x", required_drones=3)
    task.ensure_children(2)
    task.child_statuses["0"] = "completed"
    task.completed_children = 1
    db.flush_tasks([task.to_dict()])
    stored, = db.list_tasks()
    assert stored["completed_children"] == 1
    assert stored["child_statuses"]["0"] == "completed"
    db.close()
    db.close()


def test_cancel_releases_children_and_close_rejects_ticks():
    backend = _make_backend()
    task = _add_multi_task(backend)
    backend._assign_tasks({})
    assert backend.cancel_task(task.task_id)
    assert not _holders(backend, task.task_id)
    assert task.status == "cancelled"
    backend.close()
    backend.close()
    with pytest.raises(RuntimeError):
        backend.tick_simulation()


def test_planner_does_not_cut_diagonal_between_forbidden_cells():
    lookup = lambda block: zones.PURPLE if block in {(1, 0), (0, 1)} else None
    route = pathplan.plan_route((0, 0), (18, 18), lookup)
    assert route
    assert route[0] != (18, 18)


def test_long_step_cannot_jump_over_a_forbidden_strip():
    sim = SimulationEngine(drone_count=1)
    drone = sim.drones["drone_1"]
    sim.signal_field.zone_at = lambda x, y: zones.PURPLE if 8 <= x <= 12 else zones.GREEN
    assert not sim._apply_step(drone, (1, 0), 30)
    assert (drone.x, drone.y) == (0, 0)
