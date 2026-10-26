# -*- coding: utf-8 -*-
"""细查二：隔离出"单任务 4 机"场景，看编队目标点与飞行间距。"""
import sys
from pathlib import Path

ROOT = Path(r"d:\desktop\daima\python\VS\Apiary")
sys.path.insert(0, str(ROOT))

from core.backend import ApiaryBackend
from core import pathplan
from core.taskdb import TaskDB

backend = ApiaryBackend()
backend.task_db = TaskDB(":memory:")
backend.configure_runtime(drone_count=4)
backend.ai_enabled = False

# 隔离：清掉 4 个默认任务，只留探针任务。
backend.task_pool.tasks.clear()
backend.simulation._seed_drones(4)

task = backend._make_task(
    task_id="probe_overlap_1",
    raw_text=u"所有无人机前往a点",
    priority="normal",
    target="a点",
    to_location="a点",
    from_location=None,
    required_drones=1,
    all_drones=True,
    return_to_base=False,
)
backend.task_pool.add_task(task)
backend.task_db.add_or_update_task(task.to_dict())
backend.mark_requests_dirty()

names = sorted(backend.simulation.drones)
print(u"--- 手工直接 bind_task 4 台到同一任务 ---")
for name in names:
    drone = backend.simulation.drones[name]
    backend.simulation.bind_task(
        drone,
        task_id=task.task_id,
        task_type="recon",
        task_end=(120.0, -120.0),
        task_start=(0.0, 0.0),
        priority="normal",
        return_to_base=False,
    )
    print(u"  %s task_end=%s needs_replan=%s planned_route=%s" % (
        name, drone.task_end, drone.needs_replan, drone.planned_route))
print(u"编队注册表：%s" % backend.simulation._formation)

# 逐个 tick 记录每机目标点与两机最小间距
min_pair = None
min_info = None
for tick in range(400):
    backend.tick_simulation()
    holders = [backend.simulation.drones[n] for n in names if backend.simulation.drones[n].task_id == task.task_id]
    for i in range(len(holders)):
        for j in range(i + 1, len(holders)):
            a, b = holders[i], holders[j]
            dist = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
            if min_pair is None or dist < min_pair:
                min_pair = dist
                min_info = (tick, a.drone_id, a.x, a.y, a.task_end, b.drone_id, b.x, b.y, b.task_end)
    if tick % 40 == 0:
        print(u"tick=%3d holders=%d %s" % (tick, len(holders), [
            u"%s(%.0f,%.0f|%s)" % (h.drone_id, h.x, h.y, h.status) for h in holders]))

print(u"--- 结果 ---")
for name in names:
    drone = backend.simulation.drones[name]
    print(u"  %s task_end=%-20s final=(%7.2f,%7.2f) status=%s zone=%s" % (
        name,
        "None" if drone.task_end is None else "(%6.1f,%6.1f)" % drone.task_end,
        drone.x, drone.y, drone.status, drone.observed_zone))
if min_info is not None:
    print(u"全程最小间距 %.3f 于 tick=%d: %s(%.2f,%.2f)->%s / %s(%.2f,%.2f)->%s" % (
        min_pair, min_info[0], min_info[1], min_info[2], min_info[3], min_info[4],
        min_info[5], min_info[6], min_info[7], min_info[8]))
