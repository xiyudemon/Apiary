from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from AI.agent_service import AgentService
from core import pathplan,zones
from core.simulation import SimulationEngine
from core.task_pool import Task, TaskPool, normalize_priority, priority_label, priority_rank, may_interrupt
from core.taskdb import TaskDB

logger = logging.getLogger(__name__)


class ApiaryBackend:
    def __init__(self):
        self.task_pool = TaskPool()
        self.simulation = SimulationEngine()
        self.ai = AgentService()
        self.task_db = TaskDB()
        stored_reset_setting = self.task_db.get_setting("reset_tasks_on_startup")
        self.reset_tasks_on_startup = (
            True
            if stored_reset_setting is None
            else self.task_db.get_bool_setting("reset_tasks_on_startup")
        )
        if stored_reset_setting is None:
            self.task_db.set_setting("reset_tasks_on_startup", True)
        self.last_ai_call_ts = 0.0
        self.ai_enabled = True
        self._last_summary_refresh = 0.0
        self.ai_avoidance_enabled = True
        self.runtime = {
            "speed": 1.0,
            "drone_count": 3,
            "task_count": 4,
            "last_action": "idle",
            "ai_known_cells": 0,
            "ai_unknown_cells": 0,
            "ai_enabled": True,
            "ai_avoidance_enabled": True,
            "reset_tasks_on_startup": self.reset_tasks_on_startup,
        }
        self.event_trace: List[Dict[str, Any]] = []
        # 事件驱动调度：只有"任务状态变化"或"任务请求变化"才会重新分配无人机。
        # 其余 tick 只推进仿真，非紧急任务因此不会被打断。
        self._last_status_signature: Optional[tuple] = None
        self._last_request_signature: Optional[tuple] = None
        self.reschedule_reason = "initial"
        self.reschedule_count = 0
        # 每次 AI 调度随请求一起传输服务端本地热力图，AI 无法直接读取原始全局热力图。
        self.last_heatmap_payload: Dict[str, Any] = {}
        self.ai_dispatch_count = 0
        self._tick_lock = threading.RLock()
        self._route_ai_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apiary-route-ai")
        self._route_ai_jobs: Dict[str, tuple[str, Future]] = {}
        self._strategy_ai_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apiary-strategy-ai")
        self._strategy_ai_job: Optional[Future] = None
        self._route_ai_cooldowns: Dict[str, float] = {}
        self.pipeline_state = {
            "current_stage": "观测",
            "steps": [
                {"name": "观测", "done": False, "active": True},
                {"name": "决策", "done": False, "active": False},
                {"name": "分配无人机", "done": False, "active": False},
                {"name": "避障", "done": False, "active": False},
                {"name": "执行", "done": False, "active": False},
            ],
            "message": "等待调度起始",
        }
        self._prepare_startup_tasks()
        self._sync_task_pool_to_db()
        self.configure_runtime(speed=1.0, drone_count=3, task_count=len(self.task_pool.tasks))
        self.set_ai_mode(avoidance_enabled=True)

    @staticmethod
    def _is_preserved_startup_task(task: Dict[str, Any]) -> bool:
        target = str(task.get("target") or task.get("to_location") or "").lower()
        raw_text = str(task.get("raw_text") or "").lower()
        task_id = str(task.get("task_id") or "").lower()
        is_detection = task.get("task_type") == "recon" and bool(
            re.fullmatch(r"task_[a-e]", target)
        )
        is_test = (
            task.get("task_type") == "test"
            or task_id.startswith("task_test_")
            or "测试" in raw_text
            or "test" in raw_text
        )
        return is_detection or is_test

    @staticmethod
    def _task_from_record(record: Dict[str, Any]) -> Task:
        fields = Task.__dataclass_fields__
        return Task(**{key: record[key] for key in fields if key in record})

    def _prepare_startup_tasks(self) -> None:
        # Every process starts a new demo; execution progress is never resumed.
        self.task_db.reset()
        self.task_pool.tasks.clear()
        self._load_default_tasks()

    def set_reset_tasks_on_startup(self, enabled: bool) -> bool:
        self.reset_tasks_on_startup = bool(enabled)
        self.task_db.set_setting("reset_tasks_on_startup", self.reset_tasks_on_startup)
        self.runtime["reset_tasks_on_startup"] = self.reset_tasks_on_startup
        return self.reset_tasks_on_startup

    def _load_default_tasks(self, include_nonpreserved: bool = True):
        default_tasks = [
            {
                "task_id": "task_default_1",
                "raw_text": "前往任务点A巡检一遍，观察信号覆盖和任务状态",
                "action": "new_task",
                "priority": "high",
                "target": "task_A",
                "constraints": ["avoid_weak_signal_zone"],
                "status": "pending",
                "task_type": "recon",
                "return_to_base": False,
                "is_special": False,
            },
            {
                "task_id": "task_default_2",
                "raw_text": "前往任务点B巡检一遍，观察信号覆盖和任务状态",
                "action": "new_task",
                "priority": "high",
                "target": "task_B",
                "constraints": ["avoid_weak_signal_zone"],
                "status": "pending",
                "task_type": "recon",
                "return_to_base": False,
                "is_special": False,
            },
            {
                "task_id": "task_default_3",
                "raw_text": "从C点到D点运输关键物资，尽快送达",
                "action": "transport",
                "priority": "urgent",
                "target": "task_D",
                "constraints": ["avoid_high_noise_zone", "prefer_safe_corridor"],
                "status": "pending",
                "task_type": "transport",
                "from_location": "task_C",
                "to_location": "task_D",
                "return_to_base": False,
                "is_special": True,
            },
            {
                "task_id": "task_default_4",
                "raw_text": "前往任务点D巡检一遍，观察信号覆盖和任务状态",
                "action": "new_task",
                "priority": "normal",
                "target": "task_D",
                "constraints": ["avoid_weak_signal_zone"],
                "status": "pending",
                "task_type": "recon",
                "return_to_base": False,
                "is_special": False,
            },
        ]

        for item in default_tasks:
            if not include_nonpreserved and not self._is_preserved_startup_task(item):
                continue
            if item["task_id"] in self.task_pool.tasks:
                continue
            self.task_pool.add_task(
                Task(
                    task_id=item["task_id"],
                    raw_text=item["raw_text"],
                    action=item["action"],
                    priority=item["priority"],
                    target=item["target"],
                    constraints=item["constraints"],
                    status=item["status"],
                    task_type=item.get("task_type", "recon"),
                    from_location=item.get("from_location"),
                    to_location=item.get("to_location"),
                    return_to_base=item.get("return_to_base", False),
                    is_special=item.get("is_special", False),
                    requested_by="admin",
                    user_level="管理员",
                )
            )

    def _sync_task_pool_to_db(self):
        self._flush_dirty_tasks()

    def _make_task(self, *, task_id: str, raw_text: str, priority: str, target: str, **kwargs: Any) -> Task:
        """构造任务：结束地点必有值，出发地点留空时由无人机自身位置兜底。

        ``return_to_base`` 默认 False：任务做完在任务点原地待命等下一个任务，
        只有显式要求返航（如运输任务要回库）才会飞回基站。
        """
        task = Task(
            task_id=task_id,
            raw_text=raw_text,
            priority=priority,
            target=target,
            constraints=kwargs.pop("constraints", ["avoid_weak_signal_zone"]),
            action=kwargs.pop("action", "new_task"),
            status=kwargs.pop("status", "pending"),
            task_type=kwargs.pop("task_type", "recon"),
            return_to_base=kwargs.pop("return_to_base", False),
            **kwargs,
        )
        # 结束地点是硬性要求：缺失时用 target 补齐。
        task.ensure_end_location(fallback=target)
        return task

    def configure_runtime(self, speed: float | None = None, drone_count: int | None = None, task_count: int | None = None) -> Dict[str, Any]:
        if speed is not None:
            self.runtime["speed"] = max(0.25, min(3.0, float(speed)))
        if drone_count is not None:
            count = max(1, min(int(drone_count), 12))
            self.runtime["drone_count"] = count
            if count != len(self.simulation.drones):
                self.simulation._seed_drones(count)
                self.mark_requests_dirty()
        if task_count is not None:
            # 允许 0：清空动态任务（此前 max(1, ...) 导致 task_generated_* 永远无法移除）。
            desired = max(0, min(int(task_count), 12))
            self.runtime["task_count"] = desired
            for idx in range(desired):
                task_id = f"task_generated_{idx + 1}"
                if task_id not in self.task_pool.tasks:
                    task = self._make_task(
                        task_id=task_id,
                        raw_text=f"动态任务 {idx + 1}",
                        priority="normal" if idx % 2 == 0 else "high",
                        target=f"task_{chr(65 + (idx % 4))}",
                        requested_by="admin",
                        user_level="管理员",
                    )
                    self.task_pool.add_task(task)
                    self.task_db.add_or_update_task(task.to_dict())
            for idx in range(desired, len(self.task_pool.tasks)):
                task_id = f"task_generated_{idx + 1}"
                if task_id in self.task_pool.tasks:
                    self.task_pool.tasks.pop(task_id)
                    self.task_db.update_status(task_id, "cancelled")
        self.runtime["last_action"] = "runtime_configured"
        return dict(self.runtime)

    def set_ai_mode(self, enabled: bool | None = None, avoidance_enabled: bool | None = None) -> Dict[str, Any]:
        if enabled is not None:
            self.ai_enabled = bool(enabled)
        if avoidance_enabled is not None:
            self.ai_avoidance_enabled = bool(avoidance_enabled)
        self.runtime["ai_enabled"] = self.ai_enabled
        self.runtime["ai_avoidance_enabled"] = self.ai_avoidance_enabled
        return dict(self.runtime)

    def record_event(self, drone_id: str, action: str, step: str, detail: str) -> None:
        event = {
            "drone_id": drone_id,
            "action": action,
            "step": step,
            "detail": detail,
            "timestamp": time.monotonic(),
        }
        last_same_event = any(
            prev.get("drone_id") == drone_id
            and prev.get("action") == action
            and prev.get("step") == step
            and prev.get("detail") == detail
            for prev in self.event_trace[-10:]
        )
        if last_same_event:
            return
        self.event_trace.append(event)
        if len(self.event_trace) > 30:
            self.event_trace = self.event_trace[-30:]
        self.runtime["last_action"] = action

    def get_event_trace(self) -> List[Dict[str, Any]]:
        return list(self.event_trace)

    def set_pipeline_state(self, current_stage: str, message: str, steps: List[Dict[str, Any]]) -> None:
        self.pipeline_state = {
            "current_stage": current_stage,
            "message": message,
            "steps": steps,
        }

    def get_pipeline_state(self) -> Dict[str, Any]:
        return dict(self.pipeline_state)

    def list_tasks(self) -> List[Dict[str, Any]]:
        return [task.to_dict() for task in self.task_pool.get_sorted_tasks()]

    def update_task_priority(self, task_id: str, priority: str) -> bool:
        normalized = normalize_priority(priority)
        task = self.task_pool.tasks.get(task_id)
        if task is not None:
            task.priority = normalized
            task.is_special = may_interrupt(normalized)
        ok = self.task_db.update_priority(task_id, normalized)
        if task is not None:
            self.task_db.add_or_update_task(task.to_dict())
            # 等级变化属于"请求更新"，需要重新排队。
            self.mark_requests_dirty()
        return ok

    def submit_user_request(
        self,
        raw_text: str,
        *,
        requested_by: Optional[str] = None,
        user_level: Optional[str] = None,
        priority: Optional[str] = None,
        required_drones: Optional[int] = None,
        all_drones: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """自然语言请求入口。

        用户等级与身份一并交给 AI，由 AI 识别语言后产出任务；管理员可在界面上
        直接指定等级（``priority``），指定的等级优先于 AI 推断结果。
        ``required_drones`` / ``all_drones`` 同理：管理员显式指定时覆盖 AI 的解析结果。
        """
        parsed = self.ai.parse_user_task(
            raw_text,
            requested_by=requested_by,
            user_level=user_level,
        )
        if parsed.get("status") == "rejected":
            return parsed
        if parsed.get("action", "new_task") not in {"new_task", "transport", "recon"}:
            return {"status": "rejected", "reason": "unsupported_action", "action": parsed.get("action")}

        self._ensure_runtime_state()
        with self._tick_lock:
            if self._closed:
                return {"status": "rejected", "reason": "backend_closed"}
            return self._commit_user_request(
                parsed, raw_text, requested_by=requested_by, user_level=user_level,
                priority=priority, required_drones=required_drones, all_drones=all_drones,
            )

    def _commit_user_request(
        self, parsed, raw_text, *, requested_by=None, user_level=None,
        priority=None, required_drones=None, all_drones=None,
    ):
        resolved_priority = normalize_priority(priority or parsed.get("priority", "normal"))
        task_id = str(parsed.get("task_id") or f"task_user_{len(self.task_pool.tasks) + 1}")
        # 任务号由 AgentService 本地生成，但每次启动进程它的自增序号都会从头开始，
        # 于是重启后第一批编号会和历史编号重合；TaskPool 对同 ID 是覆盖写入、任务又会
        # 持久化到 SQLite，历史任务会被同号新请求静默顶掉。这里以任务池 + 数据库为准
        # 做最后一道去重，保证跨进程也不会撞号。
        existing_ids = set(self.task_pool.tasks)
        existing_ids.update(item["task_id"] for item in self.task_db.list_tasks())
        if task_id in existing_ids:
            task_id = self._unique_task_id(task_id, existing_ids)
        target = parsed.get("target") or parsed.get("to_location") or task_id
        to_location = parsed.get("to_location") or parsed.get("target") or target

        task = self._make_task(
            task_id=task_id,
            raw_text=parsed.get("raw_text", raw_text),
            priority=resolved_priority,
            target=target,
            constraints=parsed.get("constraints", []),
            action=parsed.get("action", "new_task"),
            status=parsed.get("status", "pending"),
            task_type=parsed.get("task_type") or parsed.get("action", "new_task"),
            # 出发地点可选；未提供时由执行任务的无人机自身位置兜底。
            from_location=parsed.get("from_location"),
            to_location=to_location,
            is_special=may_interrupt(resolved_priority),
            # 一条请求可以要求多架无人机协同（"所有无人机前往 A 点"）。
            # 管理员在界面上的显式选择优先于 AI 的解析结果。
            required_drones=parsed.get("required_drones", 1) if required_drones is None else required_drones,
            all_drones=parsed.get("all_drones", False) if all_drones is None else all_drones,
            requested_by=requested_by,
            user_level=user_level,
        )
        self.task_pool.add_task(task)
        self.task_db.add_or_update_task(task.to_dict())
        self.mark_requests_dirty()
        self.record_event("system", "request", "task_added", f"新任务 {task.task_id}（{priority_label(task.priority)}）")
        return task.to_dict()

    @staticmethod
    def _unique_task_id(base: str, taken: set) -> str:
        """给一个已占用的任务号找一个未被占用的后缀编号。"""
        suffix = 2
        candidate = f"{base}_{suffix}"
        while candidate in taken:
            suffix += 1
            candidate = f"{base}_{suffix}"
        return candidate

    # ------------------------------------------------------------------ 事件驱动调度

    def _status_signature(self) -> tuple:
        """任务"状态"指纹：等级、状态、结束地点等发生变化才算状态更新。"""
        return tuple(
            sorted(
                (
                    task.task_id,
                    normalize_priority(task.priority),
                    task.status,
                    str(task.end_location),
                    str(task.start_location),
                    bool(task.return_to_base),
                    int(task.required_drones),
                    bool(task.all_drones),
                )
                for task in self.task_pool.tasks.values()
            )
        )

    def _request_signature(self) -> tuple:
        """任务"请求"指纹：任务集合本身发生变化（新增/取消）。"""
        return tuple(sorted(self.task_pool.tasks.keys()))

    def mark_requests_dirty(self) -> None:
        """外部（GUI/AI）提交请求后调用，触发下一次 tick 重新调度。"""
        self._last_request_signature = None
        self._request_version = getattr(self, "_request_version", 0) + 1

    def _queue_drain_required(self) -> bool:
        """是否必须为了"把排队任务发出去"而重新调度。

        只要同时存在可控的无人机和"还没派够机"的任务，就应该分配：
        非紧急任务能立刻交给空闲机；紧急任务还能抢占低等级持有者。
        这里不比较指纹是否"变化"，因为排队状态处于稳态时指纹也不会变化，
        仅靠变化检测会导致任务永久滞留（无人机归巢后任务仍然无人认领）。

        多机任务只派到一部分时状态已是 ``in_progress``，但余额仍需补派，
        因此这里按"持有者数量 < 需求台数"判定，而不是只看 ``status == "pending"``。
        """
        holders = self._task_holders()
        for task in self.task_pool.tasks.values():
            if task.status in {"completed", "cancelled"}:
                continue
            if len(holders.get(task.task_id, ())) >= self._task_quota(task):
                continue
            if self._candidate_drones(may_interrupt(task.priority)):
                return True
        return False

    def drain_reschedule_reason(self, allow_queue_drain: bool = True) -> Optional[str]:
        """判断本 tick 是否需要重新调度；返回触发原因，None 表示保持现状。

        非紧急任务不会打断正在执行的任务：只有任务状态/请求发生变化（或待办队列
        出现了可以立刻派出的空闲执行者）时才会重新分配，其余 tick 完全不影响在飞的无人机。
        """
        status_signature = self._status_signature()
        request_signature = self._request_signature()
        reasons = []
        if self._last_request_signature != request_signature:
            reasons.append("request_update")
        if self._last_status_signature != status_signature:
            reasons.append("status_update")
        self._last_request_signature = request_signature
        self._last_status_signature = status_signature

        # 只有"排队中的任务确实能被派出"时才补一次分配，避免每 tick 空转。
        if not reasons and allow_queue_drain and self._queue_drain_required():
            reasons.append("queue_drain")

        if not reasons:
            return None
        self.reschedule_reason = "+".join(reasons)
        self.reschedule_count += 1
        return self.reschedule_reason

    # ------------------------------------------------------------------ 任务结算与分配

    def _task_holders(self) -> Dict[str, List[Any]]:
        """``task_id -> 正持有该任务的无人机列表``。

        ``release_task()`` 会清空 ``drone.task_id``，若在遍历中直接查询会丢失关联，
        因此统一用这份快照作答。
        """
        holders: Dict[str, List[Any]] = {}
        for drone in self.simulation.drones.values():
            if drone.task_id is not None:
                holders.setdefault(str(drone.task_id), []).append(drone)
        return holders

    def _task_quota(self, task: Task) -> int:
        """本条任务本轮最多可占用的执行机数（受当前机队规模上限约束）。"""
        task.ensure_children(len(self.simulation.drones))
        return task.total_children - task.completed_children

    def _settle_tasks(self) -> None:
        holders = self._task_holders()
        for task in self.task_pool.tasks.values():
            if task.status in {"completed", "cancelled"}:
                continue
            attached = holders.get(task.task_id, [])
            if not attached and not task.child_statuses:
                continue
            task.ensure_children(len(self.simulation.drones))
            occupied = set()
            for drone in attached:
                if drone.returning_to_base and drone.child_id is None:
                    continue
                if drone.child_id not in task.child_statuses:
                    drone.child_id = next((key for key, value in task.child_statuses.items()
                                           if value == "pending" and key not in occupied), None)
                key = drone.child_id
                if key is None:
                    continue
                occupied.add(key)
                if drone.status in {"returning", "awaiting"}:
                    task.child_statuses[key] = "completed"
                    if drone.status == "awaiting":
                        self.simulation.release_task(drone)
                    else:
                        # Returning remains unavailable, but no longer owns a child.
                        drone.child_id = None
                else:
                    task.child_statuses[key] = "in_progress"
            for key, value in task.child_statuses.items():
                if value == "in_progress" and key not in occupied:
                    task.child_statuses[key] = "pending"
            task.completed_children = sum(value == "completed" for value in task.child_statuses.values())
            if task.completed_children == task.total_children:
                task.status = "completed"
            else:
                task.status = "in_progress" if any(value == "in_progress" for value in task.child_statuses.values()) else "pending"

    def _assignment_load(self) -> Dict[str, int]:
        """每架无人机当前背负的任务数（含正在执行的一个），用于负载均衡。"""
        return {
            drone_id: (1 if drone.task_id is not None else 0)
            for drone_id, drone in self.simulation.drones.items()
        }

    def _assignment_cost(self, drone, start: tuple, load: int) -> tuple:
        """分配代价：负载均衡优先 → 路程最短 → 限制绕路。

        ``load`` 是"台数"量级，作为元组首项主导排序；路程只在负载相同时才起作用，
        绕路比例作为同分兜底，避免把无人机派往需要绕远的任务。
        """
        cache = getattr(self, "_cycle_cost_cache", {})
        key = (drone.drone_id, drone.x, drone.y, start, load, self.simulation.heatmap_version)
        if key in cache:
            return cache[key]
        distance = self.simulation.drone_distance_to(drone, start)
        route = pathplan.plan_route((drone.x, drone.y), start, self.simulation._local_zone)
        length = pathplan.path_length((drone.x, drone.y), route) if route else distance
        # 绕路比例：只在负载与路程都无法区分时作为兜底，避免把无人机派往需要绕远的任务。
        detour = length / max(distance, 1.0)
        cost = (load, round(distance, 3), round(detour, 3), drone.drone_id)
        cache[key] = cost
        return cost

    def _candidate_drones(self, urgent: bool) -> List[Any]:
        """可执行任务的无人机。

        非紧急任务只能交给空闲无人机（不打断当前进程）；紧急任务允许抢占
        等级更低的正在执行任务的无人机。
        """
        idle = [
            drone
            for drone in self.simulation.drones.values()
            if drone.task_id is None and not drone.returning_to_base
        ]
        if not urgent:
            return idle

        # 紧急任务优先使用空闲机；否则抢占等级较低的任务持有者。
        preemptible = [
            drone
            for drone in self.simulation.drones.values()
            if drone.task_id is not None
            and not drone.returning_to_base
            and drone.status != "returning"
            and priority_rank(self._task_priority_of(drone.task_id)) < priority_rank("urgent")
        ]
        return idle + preemptible

    def _task_priority_of(self, task_id: Optional[str]) -> str:
        task = self.task_pool.tasks.get(str(task_id)) if task_id else None
        return task.priority if task is not None else "normal"

    def _preempt(self, drone) -> None:
        """中断无人机当前任务：任务退回 pending，等待下一轮重新排队。

        用 ``release_task`` 一次性清空线路状态，避免残留的 ``task_end``/``route_index``
        被下一轮分配当成"已规划路线"继续飞（抢占了却仍在飞旧航点）。
        """
        previous_id = drone.task_id
        task = self.task_pool.tasks.get(str(previous_id)) if previous_id else None
        if task is not None:
            if drone.child_id in task.child_statuses:
                task.child_statuses[drone.child_id] = "pending"
            task.status = "pending"
            self.record_event(
                drone.drone_id,
                "preemption",
                "interrupt",
                f"紧急任务抢占 {task.task_id}",
            )
        self.simulation.release_task(drone)

    def _assign_tasks(self, strategy: Dict[str, Any]) -> List[str]:
        """按等级队列分配任务，返回本轮新分配的任务编号。

        一条任务可以要求多架无人机协同执行（``required_drones`` / ``all_drones``）。
        非紧急任务只能使用当前空闲机，派不满的余额留待后续 tick 有空闲机时补派，
        因此不会打断正在执行的任务；只有紧急任务才允许抢占低等级任务的持有者。
        同等级任务之间互不打断（抢占只在等级更低时发生）。
        """
        self._settle_tasks()
        self._cycle_cost_cache = {}
        holders = self._task_holders()
        load = self._assignment_load()
        newly_assigned: List[str] = []

        for task in self.task_pool.get_scheduling_tasks():
            if task.status in {"completed", "cancelled"}:
                continue
            attached = holders.get(task.task_id, [])
            remaining = self._task_quota(task) - len(attached)
            if remaining <= 0:
                continue

            urgent = may_interrupt(task.priority)
            # 同等级按"空闲且离起点近"排队：起点未指定时用无人机自身位置，
            # 因此这里以任务结束地点作为无起点时的参考点。
            start_key = task.start_location or task.end_location or task.task_id
            start = self.simulation.resolve_task_target(start_key)
            end = self.simulation.resolve_task_target(task.end_location)

            dispatched: List[Any] = []
            for _ in range(remaining):
                candidates = [
                    drone
                    for drone in self._candidate_drones(urgent)
                    if drone not in dispatched
                ]
                if not candidates:
                    break
                chosen = min(
                    candidates,
                    key=lambda d: (load.get(d.drone_id, 0),
                                   getattr(self, "_ai_preferences", {}).get(d.drone_id) != task.task_id,
                                   self._assignment_cost(d, start, load.get(d.drone_id, 0))),
                )
                if chosen.task_id is not None:
                    self._preempt(chosen)

                child_id = next((key for key, value in task.child_statuses.items() if value == "pending"), None)
                if child_id is None:
                    break
                task.child_statuses[child_id] = "in_progress"
                origin = task.child_origins.setdefault(child_id,
                    self.simulation.resolve_task_target(task.start_location) if task.start_location else (chosen.x, chosen.y))
                self.simulation.bind_task(
                    chosen,
                    child_id=child_id,
                    task_id=task.task_id,
                    task_type=task.task_type,
                    # 未指定出发地点时交给 bind_task 默认取无人机自身位置。
                    task_start=origin,
                    task_end=end,
                    priority=task.priority,
                    return_to_base=task.return_to_base,
                )
                dispatched.append(chosen)
                load[chosen.drone_id] = load.get(chosen.drone_id, 0) + 1
                strategy.setdefault("assignments", {})[chosen.drone_id] = task.task_id
                strategy.setdefault("task_types", {})[chosen.drone_id] = task.task_type
                self.record_event(
                    chosen.drone_id,
                    "assignment",
                    "task_select",
                    f"接取 {task.task_id}（{priority_label(task.priority)}）",
                )

            if dispatched:
                # 只需记录一次任务号：多机协同仍属于"同一个任务"。
                task.status = "in_progress"
                newly_assigned.append(task.task_id)
        return newly_assigned

    def tick_simulation(self, force_ai: bool = False) -> Dict[str, Any]:
        self._ensure_runtime_state()
        with self._tick_lock:
            if self._closed:
                raise RuntimeError("Backend is closed")
            return self._tick_simulation_locked(force_ai=force_ai)

    def _apply_ai_assignments(self, strategy: Dict[str, Any]) -> List[str]:
        if strategy.get("status") == "hold":
            return []
        # AI preferences are tie-breakers only; quota, FIFO and preemption are local invariants.
        self._ai_preferences = dict(strategy.get("assignments") or {})
        strategy["assignments"] = {}
        try:
            return self._assign_tasks(strategy)
        finally:
            self._ai_preferences = {}

    def _tick_simulation_locked(self, force_ai: bool = False) -> Dict[str, Any]:
        observations = self.simulation.tick()
        self._settle_tasks()

        strategy_result: Optional[Dict[str, Any]] = None
        if self._strategy_ai_job is not None and self._strategy_ai_job.done():
            job = self._strategy_ai_job
            self._strategy_ai_job = None
            try:
                result = job.result()
                if self.ai_enabled and getattr(self, "_strategy_ai_token", None) == self._execution_token():
                    strategy_result = result if isinstance(result, dict) else {"status": "hold"}
            except Exception as exc:  # noqa: BLE001 - 本地规则必须接管失败的 AI 请求
                logger.exception("AI 调度请求失败，回退本地规则")
                strategy_result = {"status": "hold", "reason": f"ai_error_{type(exc).__name__}"}

        ai_route_replans = []
        if self.ai_enabled and self.ai_avoidance_enabled:
            for drone_id, (task_id, execution_version, heatmap_version, future) in list(self._route_ai_jobs.items()):
                if not future.done():
                    continue
                del self._route_ai_jobs[drone_id]
                drone = self.simulation.drones.get(drone_id)
                try:
                    route_result = future.result()
                except Exception as exc:  # noqa: BLE001 - 本地路线继续兜底
                    route_result = {"status": "hold", "waypoints": [], "reason": f"ai_error_{type(exc).__name__}"}
                if (drone is None or drone.task_id != task_id or drone.execution_version != execution_version
                        or self.simulation.heatmap_version != heatmap_version or drone.status in {"awaiting", "returning"}):
                    continue
                self.record_event(
                    drone_id,
                    "ai_route",
                    "response",
                    "AI 返回状态={}，航点={}，原因={}".format(
                        route_result.get("status", "unknown"),
                        len(route_result.get("waypoints", [])),
                        route_result.get("reason", "unknown"),
                    ),
                )
                applied = bool(
                    drone is not None
                    and drone.task_id == task_id
                    and route_result.get("status") == "ok"
                    and self.simulation.apply_ai_route(drone, route_result.get("waypoints", []))
                )
                if applied:
                    ai_route_replans.append(drone_id)
                    # 成功后也冷却 5 秒：AI 路线至少走几秒再允许重调，
                    # 避免路线短、很快走完又卡住导致每秒反复调用 AI。
                    self._route_ai_cooldowns[drone_id] = time.monotonic() + 5.0
                    self.record_event(drone_id, "ai_route", "applied", "AI 路线已写入并等待下一 tick 执行")
                else:
                    if drone is not None and drone.task_id == task_id:
                        self.simulation.ensure_route(drone, force=True)
                        drone.status = "assigned"
                        drone.needs_replan = False
                    reason = str(route_result.get("reason", ""))
                    if route_result.get("status") == "hold" and not reason.startswith("ai_error"):
                        # 只有 AI 正常返回 hold（真的找不到方向）才降级直行
                        drone.red_bypass = True
                        self.record_event(drone_id, "ai_route", "fallback", "AI 返回 hold，启用降级直行")
                    else:
                        # AI 超时/错误/格式问题：冷却 20 秒，避免反复调用打爆 Ollama
                        self._route_ai_cooldowns[drone_id] = time.monotonic() + 20.0
                        self.record_event(drone_id, "ai_route", "fallback", f"AI 失败（{reason}），冷却 20 秒后回退本地")


            for drone in self.simulation.drones.values():
                if drone.task_id is None or drone.status not in {"planning", "blocked", "holding"}:
                    continue

                if drone.drone_id in self._route_ai_jobs:
                    continue
                if time.monotonic() < self._route_ai_cooldowns.get(drone.drone_id, 0.0):
                    continue
                goal = self.simulation.task_goal(drone) or (drone.x, drone.y)
                drone_block = pathplan.block_of(drone.x, drone.y)
                goal_block = pathplan.block_of(*goal)
                all_blocks = self.simulation.get_heatmap_blocks()
                all_blocks.sort(
                    key=lambda b: min(
                        max(abs(b["block"][0] - drone_block[0]), abs(b["block"][1] - drone_block[1])),
                        max(abs(b["block"][0] - goal_block[0]), abs(b["block"][1] - goal_block[1])),
                    )
                )
                nearby_blocks = all_blocks[:150]
                other_drones = [
                    {
                        "drone_id": other.drone_id,
                        "position": [other.x, other.y],
                        "status": other.status,
                        "task_id": other.task_id,
                        "planned_route": [list(point) for point in other.planned_route[:4]],
                    }
                    for other in self.simulation.drones.values()
                    if other.drone_id != drone.drone_id
                ]
                route_context = {
                    "drone_id": drone.drone_id,
                    "position": [drone.x, drone.y],
                    "goal": list(goal),
                    "block_size": pathplan.BLOCK_SIZE,
                    "step_size": self.simulation.step_size_for(drone),
                    "safe_distance": 15.0,
                    "recent_moves": getattr(drone, "recent_moves", [])[-3:],
                    "failed_moves": getattr(drone, "failed_moves", []),
                    "other_drones": other_drones,
                    "observed_zone": drone.observed_zone,
                    "current_route": [list(point) for point in drone.planned_route],
                    "heatmap": {
                        "heatmap_version": self.simulation.heatmap_version,
                        "observed_blocks": len(nearby_blocks),
                        "blocks": nearby_blocks,
                    },
                }
                self._route_ai_jobs[drone.drone_id] = (
                    str(drone.task_id),
                    drone.execution_version,
                    self.simulation.heatmap_version,
                    self._route_ai_executor.submit(self.ai.parse_route_replan, route_context),
                )
                # AI 路线受阻时保持 planning，下一 tick 等待 AI 结果；本地不覆盖 AI 路线。
                self.record_event(drone.drone_id, "ai_route", "request", f"已提交 AI 重规划 {drone.task_id}")

        pipeline_steps = [
            {"name": "观测", "done": True, "active": False},
            {"name": "决策", "done": False, "active": False},
            {"name": "分配无人机", "done": False, "active": False},
            {"name": "避障", "done": False, "active": False},
            {"name": "执行", "done": False, "active": False},
        ]
        self.set_pipeline_state("观测", "读取局部观测与信号场", pipeline_steps)

        # 事件驱动：只有任务状态或任务请求发生变化才重新调度，其余 tick 不影响在飞的无人机。
        reason = self.drain_reschedule_reason()
        strategy: Dict[str, Any] = {
            "status": "ok",
            "strategy": "maintain",
            "assignments": {},
            "avoidance": ["local_avoidance_only"],
            "reason": "local_rule_engine",
        }

        newly_assigned: List[str] = []
        if reason is not None and not force_ai:
            newly_assigned = self._assign_tasks(strategy)

        has_active_tasks = any(
            task.status not in {"completed", "cancelled"}
            for task in self.task_pool.tasks.values()
        )
        local_assignment_failed = (reason is not None and has_active_tasks and not newly_assigned
                                   and ("request_update" in reason or self._queue_drain_required()))
        should_query_ai = (
            self.ai_enabled
            and strategy_result is None
            and self._strategy_ai_job is None
            and (force_ai or local_assignment_failed)
        )
        ai_takeover = False
        ai_result_ready = strategy_result is not None
        if ai_result_ready:
            strategy = strategy_result or {"status": "hold"}
            ai_assignments = self._apply_ai_assignments(strategy)
            ai_takeover = bool(ai_assignments)
            if ai_takeover:
                newly_assigned = ai_assignments
            pipeline_steps[1]["done"] = True
            self.set_pipeline_state(
                "决策",
                "AI 已接管本轮任务分配" if ai_takeover else "AI 未返回可执行分配，继续本地规则",
                pipeline_steps,
            )
        elif should_query_ai:
            task_snapshot = [task.to_dict() for task in self.task_pool.get_sorted_tasks()]
            # 服务端本地无法拿到原始热力图，只能提供各无人机 3×3 区块的观测，
            # 因此每次 AI 调度都把本地热力图摘要一并传输。
            self.last_heatmap_payload = self.simulation.get_heatmap_payload()
            self.ai_dispatch_count += 1
            self._strategy_ai_token = self._execution_token()
            self._strategy_ai_job = self._strategy_ai_executor.submit(
                self.ai.parse_simulation_strategy,
                observations,
                task_snapshot,
                user_request={"raw_text": "", "reason": reason or "manual", "heatmap": self.last_heatmap_payload},
            )
            self.last_ai_call_ts = time.monotonic()
            pipeline_steps[1]["done"] = True
            self.set_pipeline_state(
                "决策",
                "AI 决策已提交，继续本地规则执行",
                pipeline_steps,
            )
        else:
            strategy["task_order"] = [task.task_id for task in self.task_pool.get_sorted_tasks()]
            pipeline_steps[1]["done"] = True
            message = "本地规则控制任务分配和避障" if reason is None else "检测到任务变更，重新排队"
            self.set_pipeline_state("决策", message, pipeline_steps)

        if reason is not None and not ai_takeover and not newly_assigned:
            newly_assigned = self._assign_tasks(strategy)

        pipeline_steps[2]["done"] = True
        if newly_assigned:
            self.set_pipeline_state(
                "分配无人机",
                f"本轮新分配 {len(newly_assigned)} 个任务（{reason}）",
                pipeline_steps,
            )
        else:
            self.set_pipeline_state("分配无人机", "无任务变更，保持当前分配", pipeline_steps)

        avoidance_count = 0
        for drone in self.simulation.drones.values():
            if drone.status == "avoiding":
                avoidance_count += 1
                self.record_event(drone.drone_id, "avoidance", "reroute", "在红/紫区执行本地避障")
            elif drone.status in {"moving", "returning"}:
                self.record_event(drone.drone_id, "move", "progress", f"前往 {drone.task_id or '基地'}")

        pipeline_steps[3]["done"] = True
        pipeline_steps[4]["done"] = True
        if avoidance_count:
            self.set_pipeline_state("避障", f"{avoidance_count} 台无人机正在本地避障", pipeline_steps)
        else:
            self.set_pipeline_state("执行", "所有无人机按本地规则持续执行", pipeline_steps)

        # 一次性提交，避免每 tick 多次磁盘同步拖慢 GUI。
        self._flush_dirty_tasks()

        if time.monotonic() - self._last_summary_refresh > 2.0:
            ai_summary = self.simulation.get_known_unknown_summary()
            self.runtime["ai_known_cells"] = ai_summary["known"]
            self.runtime["ai_unknown_cells"] = ai_summary["unknown"]
            self._last_summary_refresh = time.monotonic()

        return {
            "source": "ai" if ai_result_ready else "local",
            "reschedule_reason": reason,
            "assigned": newly_assigned,
            "observations": observations,
            "strategy": strategy,
            "tasks": [task.to_dict() for task in self.task_pool.get_sorted_tasks()],
            "pipeline": self.pipeline_state,
            "heatmap_version": self.simulation.heatmap_version,
            "ai_route_replans": ai_route_replans,
        }

    def _execution_token(self) -> tuple:
        return (getattr(self, "_request_version", 0), self._status_signature(),
                tuple((d.drone_id, d.task_id, d.child_id, d.execution_version, d.task_phase)
                      for d in self.simulation.drones.values()))

    def _ensure_runtime_state(self) -> None:
        if not hasattr(self, "_tick_lock"):
            self._tick_lock = threading.RLock()
        if not hasattr(self, "_strategy_ai_executor"):
            self._strategy_ai_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apiary-strategy-ai")
            self._strategy_ai_job = None
            self._route_ai_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apiary-route-ai")
            self._route_ai_jobs = {}
            self._route_ai_cooldowns = {}
        if not hasattr(self, "_closed"):
            self._closed = False

    def _flush_dirty_tasks(self) -> None:
        previous = getattr(self, "_persisted_tasks", {})
        records = {task.task_id: task.to_dict() for task in self.task_pool.tasks.values()}
        changed = [record for key, record in records.items() if record != previous.get(key)]
        if changed:
            self.task_db.flush_tasks(changed)
        self._persisted_tasks = records

    def cancel_task(self, task_id: str) -> bool:
        self._ensure_runtime_state()
        with self._tick_lock:
            task = self.task_pool.tasks.get(task_id)
            if task is None or task.status in {"completed", "cancelled"}:
                return False
            for drone in self._task_holders().get(task_id, []):
                self.simulation.release_task(drone)
            task.status = "cancelled"
            task.child_statuses = {key: (value if value == "completed" else "cancelled")
                                   for key, value in task.child_statuses.items()}
            self.mark_requests_dirty()
            self._flush_dirty_tasks()
            return True

    def close(self) -> None:
        self._ensure_runtime_state()
        with self._tick_lock:
            if self._closed:
                return
            self._closed = True
            for job in self._route_ai_jobs.values():
                job[-1].cancel()
            if self._strategy_ai_job is not None:
                self._strategy_ai_job.cancel()
            self._route_ai_executor.shutdown(wait=False, cancel_futures=True)
            self._strategy_ai_executor.shutdown(wait=False, cancel_futures=True)
            self._flush_dirty_tasks()
            close = getattr(self.task_db, "close", None)
            if close:
                close()
