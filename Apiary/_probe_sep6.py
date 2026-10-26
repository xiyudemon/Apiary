# -*- coding: utf-8 -*-
"""共享轴向飞行道：全机队同任务最小间距扫描（纯引擎，无后端重分配干扰）。"""
import math
import sys

from core.simulation import SimulationEngine

TARGET = (120.0, -120.0)
TICKS = 500
BASE_CLEAR = 30.0


def run(count):
    sim = SimulationEngine(drone_count=count)
    task_id = "probe_same_goal"
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

    worst = (float("inf"), -1, "", "")
    timeline = []
    for tick in range(TICKS):
        sim.tick()
        airborne = [
            sim.drones[n]
            for n in names
            if sim.drone_distance_to(sim.drones[n], sim.base_station) > BASE_CLEAR
        ]
        if len(airborne) < 2:
            timeline.append((tick, None))
            continue
        best = float("inf")
        pair = ("", "")
        for i in range(len(airborne)):
            for j in range(i + 1, len(airborne)):
                a, b = airborne[i], airborne[j]
                d = math.hypot(a.x - b.x, a.y - b.y)
                if d < best:
                    best = d
                    pair = (a.drone_id, b.drone_id)
        timeline.append((tick, best))
        if best < worst[0]:
            worst = (best, tick, pair[0], pair[1])

    final = []
    for n in names:
        d = sim.drones[n]
        final.append("{}:({:.2f},{:.2f})/{}".format(d.drone_id, d.x, d.y, d.status))

    status = "OK" if worst[0] > 1.0 else "FAIL"
    print("[{}] {:>2} 台 -> 最小间距 {:.3f} 于 tick={} ({} vs {})".format(
        status, count, worst[0], worst[1], worst[2], worst[3]))
    airborne_ticks = [(t, v) for t, v in timeline if v is not None]
    if airborne_ticks:
        print("      时间线: " + ", ".join("t{}={:.2f}".format(t, v) for t, v in airborne_ticks[:16]))
    print("      终态: " + " | ".join(final))
    return worst[0]


def main():
    failures = []
    for count in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12):
        try:
            value = run(count)
        except Exception as exc:  # noqa: BLE001
            failures.append("{} 台异常: {}".format(count, exc))
            print("[ERR ] {} 台 -> {}".format(count, exc))
            continue
        if value <= 1.0:
            failures.append("{} 台最小间距 {:.3f}".format(count, value))
    print("")
    if failures:
        print("失败项：" + "；".join(failures))
    else:
        print("失败项：无")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
