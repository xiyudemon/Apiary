from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from AI.ollama_client import OllamaClient
from AI.prompts import ROUTE_REPLAN_PROMPT, SIMULATION_STRATEGY_PROMPT, USER_TASK_PROMPT
from AI.schema import (
    ROUTE_REPLAN_SCHEMA,
    SIMULATION_STRATEGY_SCHEMA,
    USER_TASK_SCHEMA,
    validate_route_replan,
    validate_simulation_strategy,
    validate_user_task,
)
from core.simulation import WORLD_BOUND, _BASE_ALIASES, _match_point_letter
from core.task_pool import _coerce_drone_count, normalize_priority

logger = logging.getLogger(__name__)

# 模型经常把"没有地点"写成字符串 "null"，或把 schema 里的占位示例当成真实值返回
# （task_id="string"）。这些值必须归一化成真正的缺失，否则会被当成有效地点/任务号，
# 最终静默落到某个默认任务点上。
_NULLISH = {"null", "none", "nil", "nan", "undefined", "unknown"}
# core.simulation 的 _COORD_RE 是锚定的（只匹配整串），这里需要从自由文本里 search。
_COORD_RE = re.compile(r"[（(]\s*(-?\d+(?:\.\d+)?)\s*[，,]\s*(-?\d+(?:\.\d+)?)\s*[)）]")
# 显式起终点表达式，例如“从 C 到 D”“由基地前往 A 点”“(0, 0) 至 (120, -120)”。
_POINT_TOKEN = r"(?:任务点\s*)?[A-E](?:点)?|点\s*[A-E]|基地|总部|起飞点|基站|base|home|origin"
_ROUTE_POINT_RE = re.compile(
    rf"(?:从|由|自)?\s*({_POINT_TOKEN})\s*(?:到|至|前往|去|往|飞往|运输到|送到)\s*({_POINT_TOKEN})",
    re.IGNORECASE,
)
_ROUTE_COORD_RE = re.compile(
    r"([（(]\s*-?\d+(?:\.\d+)?\s*[，,]\s*-?\d+(?:\.\d+)?\s*[)）])"
    r"\s*(?:到|至|前往|去|往|飞往|运输到|送到)\s*"
    r"([（(]\s*-?\d+(?:\.\d+)?\s*[，,]\s*-?\d+(?:\.\d+)?\s*[)）])",
    re.IGNORECASE,
)
# resolve_task_target 明确识别的任务点键（与 core.simulation.task_targets 保持一致）。
_POINT_TARGET_KEYS = {f"task_{letter}" for letter in "ABCDE"}

# 任务号占位值：编号一律由服务端在 parse_user_task() 末尾生成，模型拿到的骨架里
# 只放这个占位串，避免它在提示词里"看见"一个像真编号的示例然后照抄。
_TASK_ID_PLACEHOLDER = "pending-server-assignment"

# 完全无法定位地点时的统一拒绝理由，界面据此提示用户补充地点。
REASON_NO_LOCATION = "no_location_in_instruction"
NO_LOCATION_MESSAGE = "请求里没有可识别的任务地点，请补充坐标或任务点（如 (120, -120)、A点、基地）。"

# "所有无人机 / 全部无人机 / 每一架 / 全体出动" 等整体出动的说法。
_ALL_DRONES_RE = re.compile(
    r"所有|全部|全体|每一架|每架|各个无人机|所有机|全部机|全队|整个机队|集群出动|倾巢"
)
# "派 3 架 / 两架无人机 / 用 2 台" 等显式机数。中文数字先归一再取阿拉伯数字。
_DRONE_COUNT_RE = re.compile(
    r"(?:派|出动|使用|用|调用|起飞)?\s*([0-9]{1,2})\s*(?:架|台|个|部)\s*(?:无人机|飞机|机)?"
)
_CN_NUMERALS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _local_drone_count(raw_text: str) -> tuple:
    """本地兜底解析"需要几架无人机"，返回 ``(required_drones, all_drones)``。

    与地点解析同理：即使模型漏填或填错，只要用户文本里有"所有无人机""派 3 架"
    这类表述，调度就会按多机处理。默认单机（1, False）。
    """
    text = raw_text or ""
    if _ALL_DRONES_RE.search(text):
        return (1, True)
    match = _DRONE_COUNT_RE.search(text)
    if match:
        return (max(1, int(match.group(1))), False)
    for word, value in _CN_NUMERALS.items():
        if f"{word}架" in text or f"{word}台" in text:
            return (value, False)
    return (1, False)


def _clean_text(value: Any) -> Optional[str]:
    """把模型返回的 "null"/"none"/空串等归一化为 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _NULLISH:
        return None
    return text


def _local_location(raw_text: str) -> Optional[str]:
    """不依赖大模型的本地地点解析：坐标 → 字母任务点 → 基地别名。

    与 core.simulation.resolve_task_target 的识别范围保持一致，保证本地兜底
    解析出来的地点一定能被目标解析器认出来。
    """
    text = raw_text or ""
    match = _COORD_RE.search(text)
    if match:
        x = max(-WORLD_BOUND, min(WORLD_BOUND, float(match.group(1))))
        y = max(-WORLD_BOUND, min(WORLD_BOUND, float(match.group(2))))
        return "({:g}, {:g})".format(x, y)
    letter = _match_point_letter(text)
    if letter:
        return f"{letter}点"
    lowered = text.lower()
    if any(alias.lower() in lowered for alias in _BASE_ALIASES):
        return "基地"
    return None


def _normalize_route_location(value: str) -> Optional[str]:
    """把起终点表达式中的地点统一成仿真层可解析的写法。"""
    text = re.sub(r"\s+", "", value or "")
    if not text:
        return None
    if _COORD_RE.fullmatch(text):
        match = _COORD_RE.fullmatch(text)
        assert match is not None
        x = max(-WORLD_BOUND, min(WORLD_BOUND, float(match.group(1))))
        y = max(-WORLD_BOUND, min(WORLD_BOUND, float(match.group(2))))
        return "({:g}, {:g})".format(x, y)
    lowered = text.lower()
    if lowered in _BASE_ALIASES:
        return "基地"
    letter = _match_point_letter(text)
    if letter is None and len(text) == 1 and text.upper() in "ABCDE":
        letter = text.upper()
    return f"{letter}点" if letter in "ABCDE" else None


def _local_route_locations(raw_text: str) -> tuple[Optional[str], Optional[str]]:
    """解析包含两个地点的指令，返回 ``(from_location, to_location)``。"""
    text = raw_text or ""
    coordinate_match = _ROUTE_COORD_RE.search(text)
    if coordinate_match:
        return (
            _normalize_route_location(coordinate_match.group(1)),
            _normalize_route_location(coordinate_match.group(2)),
        )
    point_match = _ROUTE_POINT_RE.search(text)
    if point_match:
        return (
            _normalize_route_location(point_match.group(1)),
            _normalize_route_location(point_match.group(2)),
        )
    return None, None


class AgentService:
    def __init__(self, ollama_client: OllamaClient | None = None):
        self.ollama = ollama_client or OllamaClient()
        self.user_task_schema = validate_user_task
        self.simulation_schema = validate_simulation_strategy
        # 本地生成任务号时的自增序号，防止同一秒内的多个请求撞号。
        self._sequence = 0
        # 本进程已下发的任务号。模型无法生成编号，所以编号一律本地生成；集合用于
        # 自查（也是 _claim_task_id 的语义来源），跨进程的唯一性由数据库去重兜底。
        self._issued_ids: set = set()

    @staticmethod
    def _sanitize_llm_task_payload(payload: Any) -> Dict[str, Any]:
        """只接受 schema 中声明的字段，并把原始值规范为程序可用的数据类型。"""
        if not isinstance(payload, dict):
            return {}

        allowed = set(USER_TASK_SCHEMA["properties"])
        sanitized: Dict[str, Any] = {}
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key == "constraints":
                if isinstance(value, list):
                    sanitized[key] = [str(item) for item in value if item is not None]
                else:
                    sanitized[key] = []
                continue
            if key in {"required_drones", "all_drones"}:
                if key == "required_drones":
                    sanitized[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 1
                else:
                    sanitized[key] = bool(value)
                continue
            if key in {"priority", "task_type", "action", "status"}:
                sanitized[key] = str(value)
                continue
            if key in {"target", "to_location", "from_location"}:
                sanitized[key] = value if value is None else str(value)
                continue
            sanitized[key] = value
        return sanitized

    def _next_task_id(self) -> str:
        self._sequence += 1
        return f"task_auto_{int(time.time()) % 100000}_{self._sequence:02d}"

    def _claim_task_id(self, value: Any) -> str:
        """认领一个任务号：编号始终由本地生成，模型给出的值一律丢弃。

        编号必须在本进程内唯一，而模型**无法**生成唯一值：实测无论提示词怎么改，
        它都会照抄骨架里的示例（历史上稳定返回 "task_user_1"）。曾经的做法是"重复才
        改名"，结果是第一个请求顶着模型抄来的示例号，后续请求变成 "task_user_1_2"
        这类带后缀的脏编号；自然语言里也不会出现用户自定义编号。因此这里直接忽略
        模型值，只用本地自增序号保证唯一（跨进程重启用数据库去重兜底）。
        """
        text = self._next_task_id()
        self._issued_ids.add(text)
        return text

    def parse_user_task(
        self,
        raw_text: str,
        *,
        requested_by: Optional[str] = None,
        user_level: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把自然语言请求解析为结构化任务。

        用户身份与等级会一并交给模型，"由 AI 识别语言 + 用户等级"共同决定任务等级。
        地点部分另有一层本地解析兜底：只要用户文本里出现了坐标 / 任务点 / 基地别名，
        即使模型拒绝或输出不合法，请求也不会被静默丢弃。
        """
        # 骨架里的任务号只是字段占位：真正的编号在末尾由 _claim_task_id() 统一生成。
        # 模型也被告知不要设计编号，任何照抄回来的值都会被丢弃。
        local_from_location, local_to_location = _local_route_locations(raw_text)
        local_location = local_to_location or _local_location(raw_text)
        local_required, local_all = _local_drone_count(raw_text)

        payload: Dict[str, Any] = {
            "task_id": _TASK_ID_PLACEHOLDER,
            "action": "new_task",
            "priority": "normal",
            "target": local_location,
            "to_location": local_location,
            "from_location": local_from_location,
            "task_type": "recon",
            "constraints": ["avoid_weak_signal_zone"],
            "required_drones": local_required,
            "all_drones": local_all,
            "raw_text": raw_text,
            "status": "pending",
        }

        identity = {
            "requested_by": requested_by or "unknown",
            "user_level": user_level or "未标注",
        }
        prompt = (
            f"{USER_TASK_PROMPT}\n\n用户身份：{json.dumps(identity, ensure_ascii=False)}"
            f"\n用户指令：{raw_text}"
        )

        try:
            # 把真实 JSON Schema 交给 Ollama 做结构化输出约束，模型只能按 schema 填空，
            # 不会再自行发明权限规则或漏掉 required 键。
            llm_payload = self.ollama.call(prompt, schema=USER_TASK_SCHEMA)
            llm_payload = self._sanitize_llm_task_payload(llm_payload)
        except Exception as exc:  # noqa: BLE001 - Ollama 不可用时必须走本地兜底
            logger.warning("AI 解析任务失败，改用本地规则：%s", exc)
            llm_payload = None

        if isinstance(llm_payload, dict) and llm_payload.get("status") == "rejected":
            # 模型认为无法受理。若用户文本里其实写明了地点，则本地兜底继续建单，
            # 避免"有效请求被静默丢弃"。
            if local_location is None:
                return {
                    "status": "rejected",
                    "reason": str(llm_payload.get("reason") or REASON_NO_LOCATION),
                    "message": NO_LOCATION_MESSAGE,
                    "raw_text": raw_text,
                }
            logger.warning("模型拒绝但本地已解析出地点 %s，继续建单", local_location)

        if isinstance(llm_payload, dict) and llm_payload.get("status") not in ("error", "raw"):
            model_location = _clean_text(llm_payload.get("to_location")) or _clean_text(llm_payload.get("target"))
            if model_location:
                payload["to_location"] = model_location
                payload["target"] = model_location
            elif local_to_location is not None:
                payload["to_location"] = local_to_location
                payload["target"] = local_to_location
            payload["action"] = _clean_text(llm_payload.get("action")) or payload["action"]
            payload["task_type"] = _clean_text(llm_payload.get("task_type")) or payload["task_type"]
            payload["priority"] = normalize_priority(llm_payload.get("priority") or payload["priority"])
            from_location = _clean_text(llm_payload.get("from_location"))
            if from_location:
                payload["from_location"] = from_location
            elif local_from_location is not None:
                payload["from_location"] = local_from_location
            # 模型偶尔会把非约束内容（甚至字符串）塞进 constraints，统一归一为字符串列表。
            constraints = llm_payload.get("constraints")
            if isinstance(constraints, list):
                payload["constraints"] = [item for item in constraints if isinstance(item, str)] or payload["constraints"]
            # 多机字段：文本里出现"所有无人机 / 派 3 架"这类**显式**表述时以本地解析为准。
            # 否则模型把 all_drones 填成 false、required_drones 填成 1，会把多机任务
            # 静默降级为单机（实测用户提交"所有无人机前往a点"就落库成 all_drones=0）。
            if not local_all and local_required <= 1:
                payload["all_drones"] = bool(llm_payload["all_drones"]) if "all_drones" in llm_payload else payload["all_drones"]
                model_count = llm_payload.get("required_drones")
                if isinstance(model_count, int) and not isinstance(model_count, bool) and model_count > 0:
                    payload["required_drones"] = model_count

        if not payload["to_location"]:
            # 没有结束地点就无法建单：明确拒绝，而不是静默送到某个默认任务点。
            return {
                "status": "rejected",
                "reason": REASON_NO_LOCATION,
                "message": NO_LOCATION_MESSAGE,
                "raw_text": raw_text,
            }

        # 保证结束地点一定可被 resolve_task_target 解析；否则模型可能返回"检查一下"这类
        # 自由散文，最终静默落到默认目标点。
        if not self._is_resolvable(payload["to_location"]):
            if local_location is None:
                return {
                    "status": "rejected",
                    "reason": REASON_NO_LOCATION,
                    "message": NO_LOCATION_MESSAGE,
                    "raw_text": raw_text,
                }
            payload["to_location"] = local_location
        payload["target"] = payload["to_location"]

        payload["task_id"] = self._claim_task_id(payload["task_id"])
        self._finalize(payload)
        if self.user_task_schema(payload):
            return payload

        if payload["to_location"] != local_location and local_location is not None:
            payload["to_location"] = local_location
            payload["target"] = local_location
            self._finalize(payload)
            if self.user_task_schema(payload):
                return payload

        logger.warning("AI 返回结果仍不合法，改用本地规则建单")
        payload.update(
            {
                "action": "new_task",
                "task_type": "recon",
                "priority": "normal",
            }
        )
        self._finalize(payload)
        if self.user_task_schema(payload):
            return payload

        return {
            "status": "rejected",
            "reason": REASON_NO_LOCATION,
            "message": NO_LOCATION_MESSAGE,
            "raw_text": raw_text,
        }

    @staticmethod
    def _is_resolvable(location: Any) -> bool:
        """判断地点字符串能否被 core.simulation.resolve_task_target 解析出**真实地点**。

        只接受该解析器明确识别的写法；像"检查一下"这类散文会落到静默兜底分支（默认目标点），
        必须在这里拦下来，否则用户坐标会被悄悄替换成别的任务点。
        """
        text = _clean_text(location)
        if text is None:
            return False
        # 与 core.simulation._COORD_RE 一致：坐标必须是完整字符串，不能是散文的一部分。
        match = _COORD_RE.fullmatch(text)
        if match:
            return True
        letter = _match_point_letter(text)
        if letter and f"task_{letter}" in _POINT_TARGET_KEYS:
            return True
        if text.lower() in _BASE_ALIASES:
            return True
        if text.lower() in _POINT_TARGET_KEYS:
            return True
        indexed = re.fullmatch(r"task_(?:default|user|generated|auto)_(\d+)", text, re.IGNORECASE)
        return indexed is not None

    @staticmethod
    def _finalize(payload: Dict[str, Any]) -> None:
        """统一收敛返回值，确保 schema 与调度器都能接受。"""
        payload["target"] = payload.get("to_location") or payload.get("target")
        payload["to_location"] = payload["target"]
        if payload.get("from_location") is not None:
            payload["from_location"] = _clean_text(payload["from_location"])
        if not isinstance(payload.get("constraints"), list):
            payload["constraints"] = ["avoid_weak_signal_zone"]
        # 多机字段统一收敛到合法范围，"全部无人机"以 all_drones 为准。
        payload["required_drones"] = _coerce_drone_count(payload.get("required_drones", 1))
        payload["all_drones"] = bool(payload.get("all_drones", False))
        payload["raw_text"] = str(payload.get("raw_text") or "")
        payload["status"] = "pending"

    def parse_simulation_strategy(
        self,
        observations: List[Dict[str, Any]],
        task_pool: List[Dict[str, Any]],
        user_request: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        fallback = {
            "status": "ok",
            "strategy": "prioritize",
            "assignments": {},
            "avoidance": ["local_avoidance_only"],
            "reason": "local_signal_observations_available",
            "task_order": [task.get("task_id", "") for task in task_pool if task.get("task_id")],
        }

        payload_for_llm = {
            "observations": observations,
            "task_pool": task_pool,
        }
        # 服务端本地无法直接拿到原始热力图，只能以"3×3 区块观测"的形式随请求一起传输。
        if user_request:
            payload_for_llm["user_request"] = user_request

        try:
            llm_payload = self.ollama.call(
                f"{SIMULATION_STRATEGY_PROMPT}\n\n输入：{json.dumps(payload_for_llm, ensure_ascii=False)}",
                schema=SIMULATION_STRATEGY_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 - AI 不可用时由本地观测兜底
            logger.warning("AI 调度失败，改用本地策略：%s", exc)
            llm_payload = fallback

        if not isinstance(llm_payload, dict) or llm_payload.get("status") in ("error", "raw"):
            llm_payload = fallback

        # 模型偶尔省略某个键：缺的键用本地默认值补齐，避免整条策略被判失败而退化成 hold。
        candidate = {
            "status": llm_payload.get("status") or fallback["status"],
            "strategy": llm_payload.get("strategy") or fallback["strategy"],
            "assignments": llm_payload.get("assignments") if isinstance(llm_payload.get("assignments"), dict) else fallback["assignments"],
            "avoidance": llm_payload.get("avoidance") if isinstance(llm_payload.get("avoidance"), list) else fallback["avoidance"],
            "reason": llm_payload.get("reason") or fallback["reason"],
            "task_order": llm_payload.get("task_order") if isinstance(llm_payload.get("task_order"), list) else fallback["task_order"],
        }

        if self.simulation_schema(candidate):
            return candidate
        return {
            "status": "hold",
            "strategy": "hold",
            "assignments": {},
            "avoidance": ["hold_position"],
            "reason": "insufficient_observation",
            "task_order": [],
        }

    def parse_route_replan(self, route_context: Dict[str, Any]) -> Dict[str, Any]:
        fallback = {
            "status": "hold",
            "waypoints": [],
            "reason": "route_replan_unavailable",
        }
        try:
            llm_payload = self.ollama.call(
                f"{ROUTE_REPLAN_PROMPT}\n\n输入：{json.dumps(route_context, ensure_ascii=False)}",
                schema=ROUTE_REPLAN_SCHEMA,
                timeout=30.0,
                max_attempts=1,
            )
        except Exception as exc:  # noqa: BLE001 - 本地 A* 是路线重规划兜底
            logger.warning("AI 路线重规划失败，改用本地规划：%s", exc)
            return {
                "status": "hold",
                "waypoints": [],
                "reason": "ai_error_{}".format(type(exc).__name__),
            }

        if not validate_route_replan(llm_payload):
            return {
                "status": "hold",
                "waypoints": [],
                "reason": "ai_invalid_route_response",
            }
        return {
            "status": llm_payload.get("status", "hold"),
            "waypoints": llm_payload.get("waypoints", []),
            "reason": llm_payload.get("reason", "ai_route_replan"),
        }
