"""信号分区（四档）的唯一事实来源。

绿色 = 无干扰，黄色 = 有干扰，红色 = 难以通行，紫色 = 危险区域（完全禁入）。

core 与 GUI 都必须从本模块取阈值/颜色/文案，避免出现多套互不一致的色阶
（历史问题：GUI 用了 5 档色阶，而信号场只有 4 档，紫色从未被渲染）。
"""

from __future__ import annotations

from typing import Optional

GREEN = "green"
YELLOW = "yellow"
RED = "red"
PURPLE = "purple"

# 由好到坏排列，rank 越大越差。
ZONE_ORDER = (GREEN, YELLOW, RED, PURPLE)

# 阈值按由高到低匹配，命中即返回。
ZONE_THRESHOLDS = (
    (80.0, GREEN),
    (55.0, YELLOW),
    (30.0, RED),
)

ZONE_COLORS = {
    GREEN: "#2fb36b",
    YELLOW: "#d9c74a",
    RED: "#f28c4a",
    PURPLE: "#8b5cf6",
}

ZONE_LABELS = {
    GREEN: "无干扰",
    YELLOW: "有干扰",
    RED: "难以通行",
    PURPLE: "危险区域",
}

# 红色区为"难以通行"，允许本地停滞后退回黄/绿区；紫色危险区域完全禁入。
FORBIDDEN_ZONES = (PURPLE,)
HOLD_ZONES = (RED,)

_UNKNOWN_COLOR = "#dfe7ee"

_ZONE_RANK = {zone: index for index, zone in enumerate(ZONE_ORDER)}


def zone_of(strength: float) -> str:
    """把信号强度映射到四档分区。低于 30 即为危险区（紫色）。"""
    for threshold, zone in ZONE_THRESHOLDS:
        if strength >= threshold:
            return zone
    return PURPLE


def color_for_strength(strength: float) -> str:
    return ZONE_COLORS[zone_of(strength)]


def color_for_zone(zone: Optional[str]) -> str:
    """未知分区（尚未探测）返回中性底色。"""
    if zone is None:
        return _UNKNOWN_COLOR
    return ZONE_COLORS.get(str(zone), _UNKNOWN_COLOR)


def label_for_zone(zone: Optional[str]) -> str:
    if zone is None:
        return "未探测"
    return ZONE_LABELS.get(str(zone), "未探测")


def is_forbidden(zone: Optional[str]) -> bool:
    """紫色危险区对所有无人机（含紧急任务）完全禁入。"""
    return zone is not None and str(zone) in FORBIDDEN_ZONES


def must_hold(zone: Optional[str]) -> bool:
    """红色难以通行区需要本地停滞并搜索回退点。"""
    return zone is not None and str(zone) in HOLD_ZONES


def zone_rank(zone: Optional[str]) -> int:
    """分区质量排名，未观测区块视为最差。"""
    if zone is None:
        return len(ZONE_ORDER)
    return _ZONE_RANK.get(str(zone), len(ZONE_ORDER))


def legend() -> list:
    """给 GUI 用的图例：(zone, 颜色, 名称, 说明)。"""
    return [
        (zone, ZONE_COLORS[zone], ZONE_LABELS[zone], f"信号强度 {'≥' if zone != PURPLE else '<'} "
         f"{'80' if zone == GREEN else '55' if zone == YELLOW else '30' if zone == RED else '30'}")
        for zone in ZONE_ORDER
    ]
