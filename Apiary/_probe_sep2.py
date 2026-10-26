# -*- coding: utf-8 -*-
"""细查：多机共用一个目标点时，重叠发生在哪一段、各机目标点是否已分散。"""
import sys
from pathlib import Path

ROOT = Path(r"d:\desktop\daima\python\VS\Apiary")
sys.path.insert(0, str(ROOT))

from core.backend import ApiaryBackend
from core.taskdb import TaskDB

backend = ApiaryBackend()
backend.task_db = TaskDB(":memory:")
backend.configure_runtime(drone_count=4)
backend.ai_enabled = False
backend._sync_task_pool_to_db()

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

worst = None
for tick in range(400):
    backend.tick_simulation()
    holders = [d for d in backend.simulation.drones.values() if d.task_id == task.task_id]
    if len(holders) < 2:
        continue
    for i in range(len(holders)):
        for j in range(i + 1, len(holders)):
            a, b = holders[i], holders[j]
            dist = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
            if worst is None or dist < worst[0]:
                worst = (dist, tick, a, b)

print(u"各机目标点（task_end）与最终坐标：")
for drone in [backend.simulation.drones[key] for key in sorted(backend.simulation.drones)]:
    print(u"  %s  task_end=%-18s  final=(%7.2f, %7.2f)  status=%s" % (
        drone.drone_id,
        "None" if drone.task_end is None else "(%6.1f,%6.1f)" % drone.task_end,
        drone.x, drone.y, drone.status))
print(u"编队注册表：%s" % backend.simulation._formation)
if worst is not None:
    dist, tick, a, b = worst
    print(u"全程最小间距 %.3f 出现在 tick=%d：%s=(%.2f,%.2f) 距基站 %.2f / %s=(%.2f,%.2f) 距基站 %.2f" % (
        dist, tick, a.drone_id, a.x, a.y,
        backend.simulation.drone_distance_to(a, backend.simulation.base_station),
        b.drone_id, b.x, b.y,
        backend.simulation.drone_distance_to(b, backend.simulation.base_station)))
