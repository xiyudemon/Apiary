# -*- coding: utf-8 -*-
"""起飞窗口重叠取证：打印 6 台同任务多机的目标点、线路与逐 tick 坐标。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.simulation import SimulationEngine  # noqa: E402


TARGET = (120.0, -120.0)
COUNT = 6
TICKS = 20


def main():
    sim = SimulationEngine(drone_count=COUNT)
    task_id = "probe_takeoff"
    names = sorted(sim.drones)
    for name in names:
        sim.bind_task(
            sim.drones[name],
            task_id=task_id,
            task_type="recon",
            task_end=TARGET,
            task_start=sim.base_station,
            priority="normal",
            return_to_base=False,
        )
    print("=" * 78)
    for name in names:
        drone = sim.drones[name]
        print("{} end={} start={} slot={}".format(
            name,
            tuple(round(v, 2) for v in drone.task_end),
            drone.task_start,
            sim._formation_slot_of(drone),
        ))
        print("    axis={}".format(sim._formation_axis.get(task_id)))
        print("    offset={}".format(
            tuple(round(v, 3) for v in sim._lane_offset(drone, (0.0, 0.0), drone.task_end))
        ))
    print("=" * 78)
    for tick in range(TICKS):
        sim.tick()
        parts = []
        for name in names:
            drone = sim.drones[name]
            parts.append("{}={}".format(name, (round(drone.x, 2), round(drone.y, 2))))
        # 最小间距
        worst = (1e9, "", "")
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = sim.drones[names[i]], sim.drones[names[j]]
                dist = math.hypot(a.x - b.x, a.y - b.y)
                if dist < worst[0]:
                    worst = (dist, names[i], names[j])
        print("tick={:>2} min={:>7.3f} {:<22} | {}".format(
            tick + 1, worst[0], "{} vs {}".format(worst[1], worst[2]), " ".join(parts)
        ))
    print("=" * 78)
    for name in names:
        drone = sim.drones[name]
        print("{} route={} idx={} status={}".format(
            name,
            [tuple(round(v, 1) for v in p) for p in drone.planned_route],
            drone.route_index,
            drone.status,
        ))


if __name__ == "__main__":
    main()
