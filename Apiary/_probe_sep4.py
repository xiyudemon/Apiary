# -*- coding: utf-8 -*-
"""细查三：记录"最小间距"发生当刻的真实坐标，判断重叠是停在基站还是飞行中。"""
import sys
from pathlib import Path

ROOT = Path(r"d:\desktop\daima\python\VS\Apiary")
sys.path.insert(0, str(ROOT))

from core.backend import ApiaryBackend
from core.taskdb import TaskDB

backend = ApiaryBackend()
backend.task_db = TaskDB(":memory:")
backend.task_pool.tasks.clear()
backend.configure_runtime(drone_count=4)
backend.ai_enabled = False

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
backend.mark_requests_dirty()

names = sorted(backend.simulation.drones)


def sample(tick, exclude_base_radius):
    holders = [backend.simulation.drones[n] for n in names
               if backend.simulation.drones[n].task_id == task.task_id]
    if len(holders) < 2:
        return None
    if exclude_base_radius is not None:
        if any(backend.simulation.drone_distance_to(h, backend.simulation.base_station) <= exclude_base_radius
               for h in holders):
            return None
    best = None
    for i in range(len(holders)):
        for j in range(i + 1, len(holders)):
            a, b = holders[i], holders[j]
            dist = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
            if best is None or dist < best[0]:
                best = (dist, tick, a.drone_id, a.x, a.y, b.drone_id, b.x, b.y, a.status, b.status)
    return best


for label, radius in (("含基站阶段", None), ("排除距基站<=30", 30.0), ("排除距基站<=5", 5.0)):
    backend.task_pool.tasks[task.task_id].status = "pending"
    backend.simulation._seed_drones(4)
    for n in names:
        backend.simulation.bind_task(
            backend.simulation.drones[n],
            task_id=task.task_id,
            task_type="recon",
            task_end=(120.0, -120.0),
            task_start=(0.0, 0.0),
            priority="normal",
            return_to_base=False,
        )
    worst = None
    for tick in range(400):
        backend.tick_simulation()
        got = sample(tick, radius)
        if got is not None and (worst is None or got[0] < worst[0]):
            worst = got
    if worst is None:
        print(u"%s：无样本" % label)
        continue
    print(u"%s：最小间距 %.3f 于 tick=%d" % (label, worst[0], worst[1]))
    print(u"    %s=(%.2f,%.2f) 距基站 %.2f %s | %s=(%.2f,%.2f) 距基站 %.2f %s" % (
        worst[2], worst[3], worst[4],
        backend.simulation.drone_distance_to(
            backend.simulation.drones[worst[2]], backend.simulation.base_station), worst[8],
        worst[5], worst[6], worst[7],
        backend.simulation.drone_distance_to(
            backend.simulation.drones[worst[5]], backend.simulation.base_station), worst[9]))
