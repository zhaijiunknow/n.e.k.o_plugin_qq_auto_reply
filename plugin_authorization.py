"""Shared normalization and live authorization for QQ plugin capabilities."""
from typing import Any


def normalize_plugin_tiers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        plugin_id = str(key or "").strip()
        tier = str(value or "").strip().lower()
        if plugin_id and tier in ("all", "admin"):
            clean[plugin_id] = tier
    return clean


def current_conversation_permission(plugin: Any, conversation: dict | None) -> str:
    """Revalidate identity, bounded by this turn's original authority.

    Group tool authority belongs to the group, but a blacklisted sender must
    never retain it. Missing/unavailable rosters fail closed.

    One exception, by the owner's decision (2026-11-16): on the QQ Open Platform
    the group roster is not consulted. Groups there are opaque openids with no
    place in the UI to register them, so the roster only ever holds QQ group
    numbers and every tool call in an open-platform group was refused after
    being mounted. Which groups the bot serves is managed on the QQ side. The
    ceiling stays "trusted" -- a group never carries admin authority -- and the
    turn's original authority still bounds the result.
    """
    from .media_seam import is_open_platform

    convo = conversation or {}
    sender = str(convo.get("sender_id") or "").strip()
    if not sender:
        return "none"
    try:
        level = str(
            plugin.permission_mgr.get_permission_level(sender) or "none"
        ).strip().lower()
        if level == "blacklist":
            return "none"
        if convo.get("is_group"):
            group_id = str(convo.get("group_id") or "").strip()
            if not group_id:
                return "none"
            if is_open_platform(getattr(plugin, "qq_client", None)):
                level = "trusted"
            else:
                level = str(
                    plugin.group_permission_mgr.get_group_level(group_id) or "none"
                ).strip().lower()
    except Exception:
        return "none"
    ranks = {"none": 0, "normal": 1, "trusted": 2, "admin": 3}
    original = str(convo.get("permission_level") or "none").strip().lower()
    if level not in ranks or original not in ranks:
        return "none"
    return min((level, original), key=ranks.__getitem__)
