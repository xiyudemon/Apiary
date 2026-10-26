# -*- coding: utf-8 -*-
"""核实：多机被派往同一目标点时，抵达后是否重叠在同一点。"""
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

peak = 0
min_pair_moving = None
overlap_moving = False
min_pair_at_target = None
for tick in range(400):
    backend.tick_simulation()
    holders = [d for d in backend.simulation.drones.values() if d.task_id == task.task_id]
    peak = max(peak, len(holders))
    if len(holders) < 2:
        continue
    # 排除"刚起飞还挤在基站"的阶段：只统计离开基地后的间距。
    if any(backend.simulation.drone_distance_to(d, backend.simulation.base_station) <= 30.0 for d in holders):
        continue
    for i in range(len(holders)):
        for j in range(i + 1, len(holders)):
            dx = holders[i].x - holders[j].x
            dy = holders[i].y - holders[j].y
            dist = (dx * dx + dy * dy) ** 0.5
            if min_pair_moving is None or dist < min_pair_moving:
                min_pair_moving = dist
            if dist < 1.0:
                overlap_moving = True
    # 全部抵达终点待命时再取一次间距
    if all(d.status == "awaiting" for d in holders):
        target_pairs = []
        for i in range(len(holders)):
            for j in range(i + 1, len(holders)):
                dx = holders[i].x - holders[j].x
                dy = holders[i].y - holders[j].y
                target_pairs.append((dx * dx + dy * dy) ** 0.5)
        if target_pairs:
            min_pair_at_target = min(target_pairs)
        break

print(u"== all_drones=%s，配额=%s，峰值同时持机=%d ==" % (
    task.all_drones, backend._task_quota(task), peak))
print(u"离开基地后的最小两机间距：%s" % (
    u"n/a" if min_pair_moving is None else u"%.2f" % min_pair_moving))
print(u"离开基地后出现重叠（间距 < 1.0）：%s" % overlap_moving)
print(u"抵达终点时的最小两机间距：%s" % (
    u"n/a" if min_pair_at_target is None else u"%.2f" % min_pair_at_target))
print(u"（_SEPARATION_RADIUS = 22.0 从未被使用）")
