"""One-way owner-private -> group context: recent turns and long-term memory.

The owner's private chat is a different memory domain (legacy private corpus);
group turns read it only here, and only when the speaker is the plugin roster's
``admin``. Nothing is written back: this is a read-only disclosure path.

A per-turn token is minted *before* any await and names only the actual speaker.
It is rechecked before every use, so revoking the switch mid-turn drops the
section instead of leaking a prompt that was composed a moment earlier.
"""
from __future__ import annotations

from typing import Any

from .group_identity import speaker_permission

TOKEN_PREFIX = "owner-private-context:"

#: One history line is trimmed to this many characters. A private message can
#: hold a pasted log; the group prompt only needs the gist.
LINE_CHARS = 120
MIN_ROUNDS = 2
MAX_ROUNDS = 40
DEFAULT_ROUNDS = 8


def capture_owner_private_token(
    plugin: Any, *, sender_id: str, permission_at_receipt: str | None,
    synthetic: bool,
) -> str:
    """Mint the token for one group turn, or ``""`` when this turn may not read.

    The receipt-time permission is the only gate: a later promotion inside the
    same turn must not open the owner's private history.
    """
    sender = str(sender_id or "").strip()
    if not sender or synthetic or permission_at_receipt != "admin":
        return ""
    token = f"{TOKEN_PREFIX}{sender}"
    return token if owner_private_token_live(plugin, token) else ""


def owner_private_token_live(plugin: Any, token: str) -> bool:
    """True while the disclosure is still authorized *and* still switched on."""
    if not token.startswith(TOKEN_PREFIX):
        return False
    sender = token[len(TOKEN_PREFIX):]
    if not sender:
        return False
    settings = getattr(plugin, "_qq_settings", {}) or {}
    if not (
        settings.get("cross_domain_owner_memory", False)
        or settings.get("cross_domain_owner_longterm_memory", False)
    ):
        return False
    # A demotion during the turn revokes the token: identity is revalidated,
    # never carried over from the receipt stamp alone.
    return speaker_permission(plugin, sender) == "admin"


def _content_text(raw: Any) -> str:
    """Flatten one history row's content to plain text.

    Multimodal rows carry ``list[dict]`` content; ``str()`` on those would put
    image payloads or reprs into a group prompt.
    """
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts).strip()
    if isinstance(raw, dict) and raw.get("type") == "text":
        return str(raw.get("text", "")).strip()
    return str(raw).strip() if raw else ""


def clamp_rounds(value: Any) -> int:
    """``None`` -> default; anything else clamped (``0`` must not mean "off")."""
    try:
        rounds = DEFAULT_ROUNDS if value is None else int(value)
    except (TypeError, ValueError):
        rounds = DEFAULT_ROUNDS
    return min(MAX_ROUNDS, max(MIN_ROUNDS, rounds))


def recent_owner_private_lines(
    plugin: Any, *, sender_id: str, rounds: Any,
) -> list[str]:
    """Last ``rounds`` rows of the owner's private session, oldest first.

    Reads the live session object (the same one the private turn used), not the
    memory server: "what we just said" is a session fact, and the two domains
    are deliberately not queried across each other.
    """
    sender = str(sender_id or "").strip()
    if not sender:
        return []
    build_key = getattr(plugin, "_build_session_key", None)
    if build_key is None:
        return []
    try:
        session_key = build_key(sender_id=sender, is_group=False)
    except Exception:  # noqa: BLE001 - a key we cannot build is simply no data
        return []
    entry = (getattr(plugin, "_user_sessions", {}) or {}).get(session_key)
    if not isinstance(entry, dict):
        return []
    session = entry.get("session")
    if session is None or not hasattr(session, "_conversation_history"):
        return []
    history = list(getattr(session, "_conversation_history", []) or [])
    # Buffer drafts were replaced before delivery: nobody said them.
    undelivered = {
        id(row) for row in (entry.get("undelivered_draft_rows") or [])
    }
    limit = clamp_rounds(rounds)
    lines: list[str] = []
    for row in history[-limit:]:
        if id(row) in undelivered:
            continue
        row_type = getattr(row, "type", "") or getattr(row, "role", "")
        text = _content_text(getattr(row, "content", ""))
        if not text:
            continue
        if row_type in ("human", "user"):
            lines.append(f"主人：{text[:LINE_CHARS]}")
        elif row_type in ("ai", "assistant"):
            lines.append(f"你：{text[:LINE_CHARS]}")
    return lines
