USER_TASK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "task_id",
        "action",
        "priority",
        "target",
        "to_location",
        "task_type",
        "constraints",
        "from_location",
        "required_drones",
        "all_drones",
        "raw_text",
        "status",
    ],
    "properties": {
        "task_id": {"type": "string"},
        "action": {"type": "string", "enum": ["new_task", "modify_priority", "cancel_task", "change_constraint"]},
        # 四级任务等级：urgent(紧急) / high(急迫) / normal(一般) / low(常规)
        "priority": {"type": "string", "enum": ["urgent", "high", "normal", "low"]},
        "target": {"type": ["string", "null"]},
        # 结束地点为硬性要求；出发地点可缺省（由无人机自身位置兜底）。
        "to_location": {"type": ["string", "null"]},
        "from_location": {"type": ["string", "null"]},
        "task_type": {"type": "string", "enum": ["recon", "transport", "rescue"]},
        "constraints": {"type": "array", "items": {"type": "string"}},
        # 多机协同：required_drones 是需要几架无人机执行；all_drones 表示"全部无人机"。
        # 二者都不放进 required，避免模型漏填时整条请求被判为非法（本地解析会兜底）。
        "required_drones": {"type": "integer", "minimum": 1, "maximum": 12},
        "all_drones": {"type": "boolean"},
        "raw_text": {"type": "string"},
        "status": {"type": "string", "enum": ["pending"]},
    },
}

SIMULATION_STRATEGY_SCHEMA = {
    "type": "object",
    "required": ["status", "strategy", "assignments", "avoidance", "reason", "task_order"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "degraded", "hold"]},
        "strategy": {"type": "string", "enum": ["reassign", "prioritize", "avoid_weak_signal", "hold"]},
        "assignments": {"type": "object"},
        "avoidance": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "task_order": {"type": "array", "items": {"type": "string"}},
    },
}

ROUTE_REPLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "waypoints", "reason"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "hold"]},
        "waypoints": {
            "type": "array",
            "items": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "number"},
            },
        },
        "reason": {"type": "string"},
    },
}


def _is_valid_instance(payload: dict, schema: dict) -> bool:
    """Very small validation helper for the prototype. It checks the required keys and basic types."""
    if not isinstance(payload, dict):
        return False
    for key in schema["required"]:
        if key not in payload:
            return False
    for key, definition in schema["properties"].items():
        if key not in payload:
            continue
        value = payload[key]
        if "type" in definition:
            expected = definition["type"]
            if isinstance(expected, list):
                ok = any(isinstance(value, _type_map(item)) for item in expected)
                if not ok:
                    return False
            else:
                if not isinstance(value, _type_map(expected)):
                    return False
                # bool 是 int 的子类，整数型字段要显式排除布尔值。
                if expected == "integer" and isinstance(value, bool):
                    return False
        # 枚举校验：浅类型检查无法阻止模型编造 action/priority/status。
        # 值为 None 时跳过（target 允许 null），但 non-null 必须在白名单内。
        if "enum" in definition and value is not None:
            if str(value) not in definition["enum"]:
                return False
        if key == "constraints":
            if not isinstance(value, list):
                return False
            for item in value:
                if not isinstance(item, str):
                    return False
        if key == "avoidance":
            if not isinstance(value, list):
                return False
            for item in value:
                if not isinstance(item, str):
                    return False
        if key == "task_order":
            if not isinstance(value, list):
                return False
            for item in value:
                if not isinstance(item, str):
                    return False
    return True


def _type_map(expected: str):
    mapping = {
        "string": str,
        "object": dict,
        "array": list,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    return mapping.get(expected, object)


def validate_user_task(payload: dict) -> bool:
    return _is_valid_instance(payload, USER_TASK_SCHEMA)


def validate_simulation_strategy(payload: dict) -> bool:
    return _is_valid_instance(payload, SIMULATION_STRATEGY_SCHEMA)


def validate_route_replan(payload: dict) -> bool:
    return _is_valid_instance(payload, ROUTE_REPLAN_SCHEMA)

