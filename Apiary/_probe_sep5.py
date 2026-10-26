# -*- coding: utf-8 -*-
"""细查五：纯引擎、无后端干扰的多机同目标点排距测量。

4 台机手工绑定到同一 task_end，逐 tick 采样（排除仍在基地 30 单位内的起飞阶段），
记录最小间距当刻的真实坐标与状态，用于判断重叠是"途中共线"还是"撞上静止同伴"。
"""
import sys
from pathlib import Path

ROOT = Path(r"d:\desktop\daima\python\VS\Apiary")
sys.path.insert(0, str(ROOT))

from core.simulation import SimulationEngine

TARGET = (120.0, -120.0)


def run(drone_count, ticks=500, task_end=TARGET):
    sim = SimulationEngine(drone_count=drone_count)
    task_id = "probe_same_goal"
    names = sorted(sim.drones)
    for name in names:
        sim.bind_task(sim.drones[name], task_id=task_id, task_type="recon",
                      task_end=task_end, task_start=sim.base_station,
                      priority="normal", return_to_base=False)
    print(u"绑定后各机 task_end：")
    for name in names:
        print(u"  %s -> %s" % (name, sim.drones[name].task_end))
    print(u"编队注册表：%s" % (sim._formation,))
    sim.tick()
    for name in names:
        d = sim.drones[name]
        print(u"  %s 航点数=%d route=%s" % (name, len(d.planned_route),
                                          [(round(p[0], 1), round(p[1], 1)) for p in d.planned_route[:4]]))

    worst = None
    timeline = []
    for tick in range(ticks):
        sim.tick()
        airborne = [sim.drones[n] for n in names
                    if sim.drone_distance_to(sim.drones[n], sim.base_station) > 30.0]
        tick_min = None
        for i in range(len(airborne)):
            for j in range(i + 1, len(airborne)):
                a, b = airborne[i], airborne[j]
                dist = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
                if tick_min is None or dist < tick_min:
                    tick_min = dist
                if worst is None or dist < worst[0]:
                    worst = (dist, tick, a.drone_id, a.x, a.y, a.status,
                             b.drone_id, b.x, b.y, b.status)
        timeline.append((tick, tick_min, len(airborne)))
    print(u"逐 tick 最小间距（前 20 个有样本的 tick）：")
    shown = 0
    for tick, tick_min, alive in timeline:
        if tick_min is None or shown >= 20:
            continue
        print(u"  tick=%3d 空中=%2d 最小间距=%.3f" % (tick, alive, tick_min))
        shown += 1
    if worst is None:
        print(u"无空中样本")
        return
    print(u"巡航阶段最小两机间距：%.3f 于 tick=%d" % (worst[0], worst[1]))
    print(u"  %s=(%.2f,%.2f) %s  |  %s=(%.2f,%.2f) %s" % (
        worst[2], worst[3], worst[4], worst[5], worst[6], worst[7], worst[8], worst[9]))
    print(u"最终位置：")
    for name in names:
        d = sim.drones[name]
        print(u"  %s task_end=%s 位置=(%.2f,%.2f) status=%s" % (
            name, d.task_end, d.x, d.y, d.status))


for count in (9,):
    print(u"=== %d 台同目标点 ===" % count)
    run(count, ticks=30)
    print()
