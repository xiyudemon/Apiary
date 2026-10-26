from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 四级任务等级：紧急 > 急迫 > 一般 > 常规。
# internal key 保持英文，界面与提示统一使用中文标签，避免中文参与排序/持久化后难以迁移。
#
# may_borrow_signal：仅紧急任务可以借用 3×3 范围内其它无人机的信号。
# interrupts：仅紧急任务可以打断正在执行的任务；其它等级必须排队等待。
PRIORITY_TIERS = {
    "urgent": {
        "rank": 4,
        "label": "紧急",
        "may_borrow_signal": True,
        "interrupts": True,
        "description": "立即抢占，可借用集群信号",
    },
    "high": {
        "rank": 3,
        "label": "急迫",
        "may_borrow_signal": False,
        "interrupts": False,
        "description": "优先排队，不抢占",
    },
    "normal": {
        "rank": 2,
        "label": "一般",
        "may_borrow_signal": False,
        "interrupts": False,
        "description": "常规排队",
    },
    "low": {
        "rank": 1,
        "label": "常规",
        "may_borrow_signal": False,
        "interrupts": False,
        "description": "闲时执行",
    },
}
# 由高到低，供下拉框、列表展示使用。
PRIORITY_KEYS = ("urgent", "high", "normal", "low")
DEFAULT_PRIORITY = "normal"

# 单条任务可要求的最大执行机数，与 configure_runtime 的机队上限保持一致。
MAX_DRONES_PER_TASK = 12


def _coerce_drone_count(value: object) -> int:
    """把任意输入收敛为 1..MAX_DRONES_PER_TASK 的整数机数。"""
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    return max(1, min(count, MAX_DRONES_PER_TASK))

# 统一规范：整个系统只保留四档英文枚举，并把旧别名归一化到唯一规范值。
_LEGACY_ALIASES = {
    "critical": "urgent",
    "medium": "normal",
}

PRIORITY_ORDER = {key: tier["rank"] for key, tier in PRIORITY_TIERS.items()}
PRIORITY_ORDER.update({alias: PRIORITY_ORDER[target] for alias, target in _LEGACY_ALIASES.items()})

# 中文标签 -> 内部键，供 normalize_priority 与 label_to_key 共用。
_LABEL_TO_KEY = {tier["label"]: key for key, tier in PRIORITY_TIERS.items()}


def normalize_priority(value: Optional[str]) -> str:
    """把任意历史写法收敛到四级内部键，无法识别时回退到"一般"。

    同时接受界面使用的中文标签（"紧急"等），避免调用方忘记 ``label_to_key`` 时
    等级被静默降级。
    """
    if value is None:
        return DEFAULT_PRIORITY
    text = str(value).strip()
    if text in _LABEL_TO_KEY:
        return _LABEL_TO_KEY[text]
    key = _LEGACY_ALIASES.get(text.lower(), text.lower())
    if key in PRIORITY_TIERS:
        return key
    if text:
        logger.warning("未知任务等级 %r，回退到 %s", value, DEFAULT_PRIORITY)
    return DEFAULT_PRIORITY


def priority_label(value: Optional[str]) -> str:
    return PRIORITY_TIERS[normalize_priority(value)]["label"]


def priority_rank(value: Optional[str]) -> int:
    return PRIORITY_TIERS[normalize_priority(value)]["rank"]


def priority_tier(value: Optional[str]) -> Dict[str, object]:
    return PRIORITY_TIERS[normalize_priority(value)]


def may_borrow_signal(value: Optional[str]) -> bool:
    """只有紧急任务可以借用集群信号。"""
    return bool(priority_tier(value)["may_borrow_signal"])


def may_interrupt(value: Optional[str]) -> bool:
    """只有紧急任务可以打断正在执行的任务。"""
    return bool(priority_tier(value)["interrupts"])


def priority_choices() -> List[Dict[str, object]]:
    """界面下拉框使用的等级列表，按等级从高到低排列。"""
    return [
        {"key": key, "label": PRIORITY_TIERS[key]["label"], "rank": PRIORITY_TIERS[key]["rank"]}
        for key in PRIORITY_KEYS
    ]


def label_to_key(label: Optional[str]) -> Optional[str]:
    """把界面选择的中文等级还原为内部键；传入的已经是内部键时原样返回。"""
    if label is None:
        return None
    text = str(label).strip()
    if text in _LABEL_TO_KEY:
        return _LABEL_TO_KEY[text]
    lowered = text.lower()
    if lowered in PRIORITY_TIERS:
        return lowered
    return None


# 任务状态 -> 中文标签（任务池列表用）。
TASK_STATUS_LABELS = {
    "pending": "待办",
    "in_progress": "执行中",
    "completed": "已完成",
    "cancelled": "已取消",
}


def status_label(value: Optional[str]) -> str:
    """把任务状态翻译成界面用的中文标签，未知状态原样返回。"""
    if value is None:
        return "-"
    text = str(value)
    return TASK_STATUS_LABELS.get(text, text)


@dataclass
class Task:
    task_id: str
    raw_text: str
    action: str = "new_task"
    priority: str = DEFAULT_PRIORITY
    target: Optional[str] = None
    constraints: List[str] = field(default_factory=list)
    status: str = "pending"
    task_type: str = "recon"
    # from_location：出发地点，可选；为空时默认使用执行该任务的无人机自身位置。
    from_location: Optional[str] = None
    # to_location：结束地点，任务必须有结束地点；为空时由 target 补齐。
    to_location: Optional[str] = None
    # return_to_base：完成后是否返航。默认 False —— 无人机在任务点原地待命，
    # 等下一个任务；无脑返航会让"待命机"永远停在基站，白白浪费调度距离。
    return_to_base: bool = False
    is_special: bool = False
    # 提出该任务的用户等级/身份，仅用于留痕与 AI 参考，不参与排序。
    requested_by: Optional[str] = None
    user_level: Optional[str] = None
    # required_drones：本条任务需要多少架无人机协同执行；1 表示单机任务。
    # all_drones：是否要求"全部无人机"投入（管理员说"所有无人机前往 A 点"）。
    #   为 True 时以当前机队总数为准，忽略 required_drones。
    # 只有紧急任务才能把全部无人机抢占过来；其它等级只派当前空闲机，
    # 未派满的余额等后续 tick 有空闲机时自动补派。
    required_drones: int = 1
    all_drones: bool = False
    # Equal-weight child executions. A parent completes only after every child does.
    frozen_drone_count: Optional[int] = None
    completed_children: int = 0
    child_statuses: Dict[str, str] = field(default_factory=dict)
    child_origins: Dict[str, tuple] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.priority = normalize_priority(self.priority)
        self.constraints = list(self.constraints or [])
        self.required_drones = _coerce_drone_count(self.required_drones)
        self.all_drones = bool(self.all_drones)

    def ensure_children(self, total_drones: int) -> None:
        if self.frozen_drone_count is None:
            self.frozen_drone_count = max(1, total_drones) if self.all_drones else self.required_drones
        if not self.child_statuses:
            self.child_statuses = {str(i): "pending" for i in range(self.frozen_drone_count)}

    @property
    def total_children(self) -> int:
        return self.frozen_drone_count or self.required_drones

    def drone_quota(self, total_drones: int) -> int:
        """本条任务本轮最多可占用的执行机数，受当前机队规模上限约束。"""
        total = _coerce_drone_count(total_drones)
        if self.all_drones:
            return total
        return min(self.required_drones, total)

    @property
    def is_multi_drone(self) -> bool:
        """是否需要多架无人机协同（含"全部无人机"）。"""
        return self.all_drones or self.required_drones > 1

    @property
    def start_location(self) -> Optional[str]:
        """出发地点；None 表示由无人机自身位置兜底。"""
        return self.from_location

    @property
    def end_location(self) -> Optional[str]:
        """结束地点；任务必须能解析出一个结束地点。"""
        return self.to_location or self.target

    def ensure_end_location(self, fallback: Optional[str] = None) -> str:
        """补齐结束地点。返回最终生效的结束地点键。"""
        resolved = self.end_location or fallback
        if not resolved:
            # 结束地点是硬性要求，兜底到任务自身编号，由仿真层解析为真实坐标。
            resolved = self.task_id
            logger.warning("任务 %s 缺少结束地点，回退到 %r", self.task_id, resolved)
        self.to_location = str(resolved)
        return self.to_location

    def to_dict(self) -> Dict[str, object]:
        return {
            "task_id": self.task_id,
            "raw_text": self.raw_text,
            "action": self.action,
            "priority": self.priority,
            "priority_label": priority_label(self.priority),
            "target": self.target,
            "constraints": self.constraints,
            "status": self.status,
            "task_type": self.task_type,
            "from_location": self.from_location,
            "to_location": self.to_location,
            "return_to_base": self.return_to_base,
            "is_special": self.is_special,
            "requested_by": self.requested_by,
            "user_level": self.user_level,
            "required_drones": self.required_drones,
            "all_drones": self.all_drones,
            "frozen_drone_count": self.frozen_drone_count,
            "completed_children": self.completed_children,
            "total_children": self.total_children,
            "progress": self.completed_children / self.total_children,
            "child_statuses": dict(self.child_statuses),
            "child_origins": dict(self.child_origins),
            "children": [{"child_id": key, "status": value} for key, value in self.child_statuses.items()],
        }


class TaskPool:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}

    def add_task(self, task: Task) -> Task:
        # 任务必须有结束地点：缺失时用 target 补齐，仍缺失则用任务编号兜底。
        task.ensure_end_location(fallback=task.target)
        self.tasks[task.task_id] = task
        return task

    def update_priority(self, task_id: str, priority: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            return False
        task.priority = normalize_priority(priority)
        return True

    def cancel_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            return False
        task.status = "cancelled"
        return True

    def get_sorted_tasks(self) -> List[Task]:
        """未完成任务优先，再按等级从高到低排序，同等级按任务编号稳定排序。"""
        return sorted(
            self.tasks.values(),
            key=lambda item: (
                item.status not in {"completed", "cancelled"},
                item.status == "cancelled",
                item.is_special,
                priority_rank(item.priority),
                item.task_id,
            ),
            reverse=True,
        )

    def get_scheduling_tasks(self) -> List[Task]:
        # Python's stable sort preserves insertion FIFO within each priority.
        return sorted(
            (task for task in self.tasks.values() if task.status not in {"completed", "cancelled"}),
            key=lambda task: -priority_rank(task.priority),
        )

    def as_dict(self) -> Dict[str, Dict[str, object]]:
        return {task_id: task.to_dict() for task_id, task in self.tasks.items()}
