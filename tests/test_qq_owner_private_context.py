"""主人私聊 → 群：跨域披露的授权、读取与撤销，以及可配置的图库目录。

钉住四件事：
1. 令牌只在"接收时就是 admin + 开关打开"时才铸造，写死在名字里的发言人
   不会被后续提升（同一条消息里升职不得打开主人的私聊）；
2. 开关关掉或主人被降级后，令牌立刻失效（撤销是实时复检，不是一次性判决）；
3. 两段读取不产生写入，也不把草稿/多模态载荷带进群 prompt；
4. 图库目录只接受绝对路径，空值回到历史布局。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply import owner_private_context as opc
from plugin.plugins.qq_auto_reply.permission import PermissionManager
from plugin.plugins.qq_auto_reply.session_instruction_service import (
    QQSessionInstructionService,
)
from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService
from plugin.plugins.qq_auto_reply.sticker_store import StickerStore

OWNER = "10001"
OTHER = "20002"


class _Row:
    def __init__(self, type_: str, content):
        self.type = type_
        self.content = content


def _plugin(*, settings=None, sessions=None, memory=None) -> SimpleNamespace:
    plugin = SimpleNamespace(
        _qq_settings=dict(settings or {}),
        _user_sessions=dict(sessions or {}),
        permission_mgr=PermissionManager([{"qq": OWNER, "level": "admin", "nickname": "主人"}]),
        i18n=SimpleNamespace(t=lambda key, default=None: default or key),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                               debug=lambda *a, **k: None),
        memory_bridge=SimpleNamespace(fetch_bootstrap_memory=AsyncMock(return_value=memory or "")),
    )
    plugin._build_session_key = lambda *, sender_id, is_group: (
        f"{'group' if is_group else 'private'}:{sender_id}"
    )
    return plugin


def _private_session(history, *, drafts=()) -> dict:
    entry = {"is_group": False, "sender_id": OWNER, "session": SimpleNamespace(_conversation_history=history)}
    if drafts:
        entry["undelivered_draft_rows"] = list(drafts)
    return {f"private:{OWNER}": entry}


# ── 令牌 ────────────────────────────────────────────────────────────

def test_token_requires_receipt_time_admin_and_open_switch():
    plugin = _plugin(settings={"cross_domain_owner_memory": True})
    assert opc.capture_owner_private_token(
        plugin, sender_id=OWNER, permission_at_receipt="admin", synthetic=False,
    ) == f"{opc.TOKEN_PREFIX}{OWNER}"
    # 接收时不是主人：即便此刻名册已把他升成 admin，这一轮也不得打开
    assert opc.capture_owner_private_token(
        plugin, sender_id=OWNER, permission_at_receipt="trusted", synthetic=False,
    ) == ""
    # 合成轮没有真实发言人
    assert opc.capture_owner_private_token(
        plugin, sender_id=OWNER, permission_at_receipt="admin", synthetic=True,
    ) == ""
    # 群友一律不铸造
    assert opc.capture_owner_private_token(
        plugin, sender_id=OTHER, permission_at_receipt="admin", synthetic=False,
    ) == ""


def test_token_requires_at_least_one_of_the_two_switches():
    plugin = _plugin(settings={})
    assert opc.capture_owner_private_token(
        plugin, sender_id=OWNER, permission_at_receipt="admin", synthetic=False,
    ) == ""
    plugin._qq_settings["cross_domain_owner_longterm_memory"] = True
    assert opc.owner_private_token_live(plugin, f"{opc.TOKEN_PREFIX}{OWNER}")


def test_token_dies_when_both_switches_are_off():
    plugin = _plugin(settings={"cross_domain_owner_memory": True})
    token = f"{opc.TOKEN_PREFIX}{OWNER}"
    assert opc.owner_private_token_live(plugin, token)
    plugin._qq_settings["cross_domain_owner_memory"] = False
    assert not opc.owner_private_token_live(plugin, token)


def test_token_dies_when_owner_is_demoted_mid_turn():
    plugin = _plugin(settings={"cross_domain_owner_memory": True})
    token = f"{opc.TOKEN_PREFIX}{OWNER}"
    assert opc.owner_private_token_live(plugin, token)
    plugin.permission_mgr.remove_user(OWNER)
    assert not opc.owner_private_token_live(plugin, token)


# ── 最近对话 ────────────────────────────────────────────────────────

def test_recent_lines_flatten_multimodal_and_skip_drafts():
    draft = _Row("ai", "这句没发出去")
    history = [
        _Row("human", "在吗"),
        _Row("ai", [{"type": "text", "text": "在的"}, {"type": "image_url", "image_url": "x"}]),
        _Row("human", [{"type": "image_url", "image_url": "data:image/png;base64,AAAA"}]),
        draft,
    ]
    plugin = _plugin(sessions=_private_session(history, drafts=[draft]))
    lines = opc.recent_owner_private_lines(plugin, sender_id=OWNER, rounds=8)
    assert lines == ["主人：在吗", "你：在的"]
    assert all("base64" not in line for line in lines)


def test_recent_lines_clamp_rounds_and_trim_long_messages():
    history = [_Row("human", "长" * 500)] + [_Row("ai", f"第{i}句") for i in range(10)]
    plugin = _plugin(sessions=_private_session(history))
    lines = opc.recent_owner_private_lines(plugin, sender_id=OWNER, rounds=2)
    assert len(lines) == 2
    assert lines[-1] == "你：第9句"
    long_lines = opc.recent_owner_private_lines(plugin, sender_id=OWNER, rounds=40)
    assert long_lines[0] == "主人：" + "长" * opc.LINE_CHARS


def test_rounds_zero_is_clamped_not_treated_as_off():
    assert opc.clamp_rounds(0) == opc.MIN_ROUNDS
    assert opc.clamp_rounds(None) == opc.DEFAULT_ROUNDS
    assert opc.clamp_rounds(999) == opc.MAX_ROUNDS
    assert opc.clamp_rounds("garbage") == opc.DEFAULT_ROUNDS


def test_missing_private_session_is_not_an_error():
    plugin = _plugin()
    assert opc.recent_owner_private_lines(plugin, sender_id=OWNER, rounds=8) == []


# ── 指令段 ──────────────────────────────────────────────────────────

def _instruction_service(plugin) -> SimpleNamespace:
    return SimpleNamespace(plugin=plugin)


class _Service:
    """把真实实现绑到一个轻量对象上，避免构造整条指令服务。"""

    _append_owner_private_section = QQSessionInstructionService._append_owner_private_section
    _append_owner_longterm_section = QQSessionInstructionService._append_owner_longterm_section
    _fetch_owner_legacy_memory = QQSessionInstructionService._fetch_owner_legacy_memory
    _truncate_memory_text = staticmethod(QQSessionInstructionService._truncate_memory_text)
    _OWNER_LONGTERM_CACHE_SECONDS = QQSessionInstructionService._OWNER_LONGTERM_CACHE_SECONDS
    _OWNER_LONGTERM_FAIL_CACHE_SECONDS = QQSessionInstructionService._OWNER_LONGTERM_FAIL_CACHE_SECONDS

    def __init__(self, plugin):
        self.plugin = plugin


@pytest.mark.asyncio
async def test_private_section_needs_switch_and_admin():
    rows = [_Row("human", "今天好累")]
    plugin = _plugin(settings={"cross_domain_owner_memory": True}, sessions=_private_session(rows))
    service = _Service(plugin)
    token = f"{opc.TOKEN_PREFIX}{OWNER}"
    sections: list[str] = []
    section = service._append_owner_private_section(sections, token=token, is_group=True, sender_id=OWNER)
    assert "今天好累" in section and "Owner Private Chat" in section
    # 群友的轮次：令牌不会存在，调用方只能传空
    assert service._append_owner_private_section([], token="", is_group=True, sender_id=OTHER) == ""
    # 开关关掉：同一份令牌也不再放行
    plugin._qq_settings["cross_domain_owner_memory"] = False
    assert service._append_owner_private_section([], token=token, is_group=True, sender_id=OWNER) == ""


@pytest.mark.asyncio
async def test_longterm_section_truncates_by_line_and_caches():
    long_memory = "\n".join(f"第{i}行记忆内容" for i in range(200))
    plugin = _plugin(
        settings={"cross_domain_owner_longterm_memory": True, "cross_domain_owner_longterm_max_chars": 200},
        memory=long_memory,
    )
    service = _Service(plugin)
    token = f"{opc.TOKEN_PREFIX}{OWNER}"
    section = await service._append_owner_longterm_section(
        [], her_name="猫娘", token=token, is_group=True, already_in_core=False,
    )
    assert section
    body = section.split("\n", 4)[-1]
    assert len(body) <= 200
    # 第二次走缓存，不再打记忆服务器
    await service._append_owner_longterm_section(
        [], her_name="猫娘", token=token, is_group=True, already_in_core=False,
    )
    assert plugin.memory_bridge.fetch_bootstrap_memory.await_count == 1


@pytest.mark.asyncio
async def test_longterm_section_skipped_when_core_memory_already_has_it():
    plugin = _plugin(
        settings={"cross_domain_owner_longterm_memory": True}, memory="旧记忆",
    )
    service = _Service(plugin)
    token = f"{opc.TOKEN_PREFIX}{OWNER}"
    assert await service._append_owner_longterm_section(
        [], her_name="猫娘", token=token, is_group=True, already_in_core=True,
    ) == ""
    assert plugin.memory_bridge.fetch_bootstrap_memory.await_count == 0


@pytest.mark.asyncio
async def test_longterm_fetch_failure_keeps_previous_text():
    plugin = _plugin(settings={"cross_domain_owner_longterm_memory": True}, memory="上周的事")
    service = _Service(plugin)
    token = f"{opc.TOKEN_PREFIX}{OWNER}"
    first = await service._append_owner_longterm_section(
        [], her_name="猫娘", token=token, is_group=True, already_in_core=False,
    )
    assert "上周的事" in first
    # 过期后重新读，失败按"沿用旧值"处理，不是整段消失
    plugin.memory_bridge.fetch_bootstrap_memory.side_effect = RuntimeError("boom")
    cache = service._owner_legacy_cache["猫娘"]
    service._owner_legacy_cache["猫娘"] = (cache[0] - 10_000, cache[1])
    second = await service._append_owner_longterm_section(
        [], her_name="猫娘", token=token, is_group=True, already_in_core=False,
    )
    assert "上周的事" in second


# ── 撤销 ────────────────────────────────────────────────────────────

def test_strip_removes_section_with_its_separator():
    from plugin.plugins.qq_auto_reply.reply_context_node import QQReplyContextNode

    node = QQReplyContextNode(_plugin())
    prompt = "A段\n\n## 私聊\n只有主人能看到\n\nB段"
    stripped = node._strip_owner_private_sections(prompt, "## 私聊\n只有主人能看到")
    assert stripped == "A段\n\nB段"


# ── 图库目录 ────────────────────────────────────────────────────────

def test_sticker_store_uses_default_layout_when_unset():
    data = Path("C:/neko-test-data")
    # 宿主/替身的 data_path 都至少收一个位置参数：无参调用会在 CI 的替身上炸掉
    plugin = SimpleNamespace(_qq_settings={}, data_path=lambda name: data / name)
    store = StickerStore(plugin)
    assert store.root == data
    assert store.index == data / "sticker.json"
    assert store.directory == data / "sticker"


def test_sticker_store_follows_gallery_dir():
    data = Path("C:/neko-test-data")
    target = Path("C:/neko-test-library")
    plugin = SimpleNamespace(_qq_settings={"gallery_dir": str(target)}, data_path=lambda name: data / name)
    store = StickerStore(plugin)
    assert store.root == target
    assert store.index == target / "sticker.json"


def test_gallery_dir_rejects_relative_paths():
    service = QQSettingsService(_plugin())
    with pytest.raises(ValueError):
        service._apply_gallery_dir("relative/gallery")
    with pytest.raises(ValueError):
        service._apply_gallery_dir("../up")


def test_gallery_dir_normalizes_and_clears():
    plugin = _plugin()
    service = QQSettingsService(plugin)
    # 用平台自带的绝对路径：Windows 的 "C:/..." 在 Linux CI 上不是绝对路径
    target = Path(os.path.abspath("neko-test-library"))
    service._apply_gallery_dir(f'  "{target}"  ')
    assert plugin._qq_settings["gallery_dir"] == str(target)
    service._apply_gallery_dir("")
    assert plugin._qq_settings["gallery_dir"] == ""
    # 保存只归一字符串，不碰文件系统：目录不存在也照存（用户随时改回去）
    assert not target.exists()
