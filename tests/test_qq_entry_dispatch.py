"""合并入口的 action 分流 + 入口面的看门狗。

每个大入口都是"薄分发器 + 每 action 一个私有方法"。这里钉住三件事：
未知 action 必须明确报错（不是静默什么都不做）、必填参数必须在下游服务被调用**之前**拦下、
以及**入口面恰好是这 7 个**。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin


def _plugin(**service_methods):
    """只装被测方法用到的那几样：logger + dashboard_service。"""
    p = QQAutoReplyPlugin.__new__(QQAutoReplyPlugin)
    p.logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                               error=lambda *a: None)
    p.dashboard_service = SimpleNamespace(
        **{name: AsyncMock(return_value={"ok": name}) for name in service_methods})
    return p


# ── 分流 ────────────────────────────────────────────────────

async def test_routes_each_action_to_its_service_method():
    p = _plugin(add_trusted_user=None, remove_trusted_user=None, add_trusted_group=None,
                remove_trusted_group=None, set_user_nickname=None,
                list_identity_claims=None, bind_identity_account=None,
                unbind_identity_account=None, refresh_actual_contacts=None)

    await p.trust(action="user_add", qq_number="111")
    await p.trust(action="user_remove", qq_number="111")
    await p.trust(action="user_nickname", qq_number="111", nickname="阿喵")
    await p.trust(action="group_add", group_id="222")
    await p.trust(action="group_remove", group_id="222")
    await p.trust(action="claims")
    await p.trust(action="identity_bind", user_id="A", target_user_id="B")
    await p.trust(action="identity_unbind", user_id="A")
    await p.trust(action="refresh_contacts")

    for name, called in (
        ("add_trusted_user", dict(qq_number="111", level="trusted",
                                 nickname="", normal_relay_probability=None)),
        ("remove_trusted_user", dict(qq_number="111")),
        ("set_user_nickname", dict(qq_number="111", nickname="阿喵")),
        ("add_trusted_group", dict(group_id="222", level="normal",
                                   normal_relay_probability=None,
                                   open_reply_probability=None)),
        ("remove_trusted_group", dict(group_id="222")),
        ("list_identity_claims", dict()),
        ("bind_identity_account", dict(user_id="A", target_user_id="B")),
        ("unbind_identity_account", dict(user_id="A")),
        ("refresh_actual_contacts", dict()),
    ):
        getattr(p.dashboard_service, name).assert_awaited_once_with(**called)


async def test_rejects_unknown_action():
    p = _plugin()
    r = await p.trust(action="promote")

    assert r.is_err()
    assert "BAD_ACTION" in str(r.error) and "promote" in str(r.error)


async def test_rejects_empty_action():
    p = _plugin()
    assert (await p.trust(action="")).is_err()


# ── 必填参数在调用服务之前就拦下 ────────────────────────────

async def test_missing_required_params_never_reach_the_service():
    """少了 group_id 就该在分发层返回 Err，而不是把空值丢给服务层。"""
    p = _plugin(add_trusted_group=None, remove_trusted_group=None)
    for action in ("group_add", "group_remove"):
        r = await p.trust(action=action, group_id="")
        assert r.is_err(), action
        assert "INVALID_INPUT" in str(r.error), action
    p.dashboard_service.add_trusted_group.assert_not_awaited()
    p.dashboard_service.remove_trusted_group.assert_not_awaited()


async def test_identity_bind_requires_both_ids():
    p = _plugin(bind_identity_account=None)
    r = await p.trust(action="identity_bind", user_id="A")

    assert r.is_err() and "INVALID_INPUT" in str(r.error)
    p.dashboard_service.bind_identity_account.assert_not_awaited()


# ── 入口对 agent 的可见性 ───────────────────────────────────

def test_trust_entry_is_visible_to_the_agent():
    """trust 没打隐藏标记 —— agent 能调（与 asset/deploy 相反）。"""
    from plugin.sdk.shared.core.decorators import EVENT_META_ATTR
    meta = getattr(QQAutoReplyPlugin.trust, EVENT_META_ATTR)
    flags = meta.metadata or {}
    assert flags.get("agent_auto") is not False
    assert flags.get("agent_hidden") is not True


# ── 入口面看门狗 ────────────────────────────────────────────

EXPECTED_ENTRIES = {"runtime", "config", "query", "send", "trust", "deploy", "asset"}

#: 对 agent 隐藏的入口（braid/task_executor.py 的 _is_plugin_entry_agent_hidden 读这个标记）
AGENT_HIDDEN = {"asset", "deploy"}


def _plugin_entry_ids() -> set[str]:
    from plugin.sdk.shared.core.decorators import EVENT_META_ATTR

    ids: set[str] = set()
    for name in dir(QQAutoReplyPlugin):
        meta = getattr(getattr(QQAutoReplyPlugin, name, None), EVENT_META_ATTR, None)
        if meta is not None and getattr(meta, "event_type", "") == "plugin_entry":
            ids.add(str(meta.id))
    return ids


def test_entry_surface_is_exactly_seven():
    """**收束成果的看门狗。**

    48 → 7 是一次大重构换来的。没有这条，后续提交会一点点把入口长回去 ——
    而涨回去的入口会重新进入 agent 的候选面、重新出现在宿主面板上。
    确实要加新入口就改这个集合，改的时候你会看到这条提示。
    """
    assert _plugin_entry_ids() == EXPECTED_ENTRIES


def test_agent_hidden_entries_carry_the_flag():
    """asset / deploy 必须对 agent 隐藏 —— 前者是资源写入面，
    后者装着会杀掉正在运行的 QQ 的 one_click 和会轮换 AppSecret 的 bind_*。"""
    from plugin.sdk.shared.core.decorators import EVENT_META_ATTR

    for name in AGENT_HIDDEN:
        meta = getattr(getattr(QQAutoReplyPlugin, name), EVENT_META_ATTR)
        flags = meta.metadata or {}
        assert flags.get("agent_auto") is False, f"{name} 少了 agent_auto: False"


def test_the_other_five_stay_agent_visible():
    from plugin.sdk.shared.core.decorators import EVENT_META_ATTR

    for name in EXPECTED_ENTRIES - AGENT_HIDDEN:
        meta = getattr(getattr(QQAutoReplyPlugin, name), EVENT_META_ATTR)
        flags = meta.metadata or {}
        assert flags.get("agent_auto") is not False, f"{name} 不该隐藏"
        assert flags.get("agent_hidden") is not True, f"{name} 不该隐藏"


def test_every_entry_dispatches_on_action():
    """7 个入口都是同一形状：`(self, action="", **kw)` + 一个 `_<entry>_dispatch`。"""
    import inspect

    for name in EXPECTED_ENTRIES:
        params = list(inspect.signature(getattr(QQAutoReplyPlugin, name)).parameters)
        assert params == ["self", "action", "kw"], f"{name}: {params}"
        assert callable(getattr(QQAutoReplyPlugin, f"_{name}_dispatch", None)), name
