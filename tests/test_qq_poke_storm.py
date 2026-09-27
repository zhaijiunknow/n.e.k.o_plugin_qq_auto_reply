# -*- coding: utf-8 -*-
"""戳一戳：**不管几个人戳，都只跟戳、不回话**。

使用者口径（2026-09-27）：

> 「戳戳风暴就不需要回复了，只需要跟戳」

改之前派发层里是**反着**的：

    人少 → 逐个回戳、不进入 LLM
    人多（风暴）→ 不回戳、把「N 个人戳了戳你」注入管线让她在群里说点什么

真机 15:20 那次风暴就因此多花了一轮生成，而且那条合成消息带着 `is_at_bot=True`
一路走到门控 —— 相当于"有人点名她"，会抢焦点、会上锁。她要的语义是：戳一戳本来就是
轻量互动，**只跟戳**，不必说话，也不必为它开一轮对话。

保留的两道闸（在下面各有用例）：

* **每人 5 分钟最多回戳 2 次**：跟戳不等于陪到底，否则就是无限互戳；
* **黑名单用户的戳在更早处已被拦掉**（`test_qq_user_blacklist.py` 守着那条）。

「戳别人」（她不是被戳对象）**不受影响**：仍然注入管线让模型决定要不要也戳一下 ——
使用者说的是"戳戳风暴"（一群人戳她），不是"群里任何人戳任何人"。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.message_dispatcher import QQMessageDispatcher

GROUP = "1048307485"
BOT = "3281414178"
ALICE = "1782348687"
BOB = "3658428358"
CAROL = "647695392"


class _Recorder:
    """最小插件桩：记录回戳、backlog、以及"有没有走到群聊管线"。"""

    def __init__(self, *, self_id: str = BOT, level: str = "trusted") -> None:
        self.pokes: list[tuple[str, str]] = []
        self.backlog: list = []
        self.pipeline: list = []
        self.emitted: list[tuple[str, str]] = []
        self._poke_timestamps: dict[str, list[float]] = {}
        self._poke_storm: dict[str, list[tuple[float, str]]] = {}
        self._user_sessions: dict = {}
        self.enricher = None
        self.backlog_service = SimpleNamespace(record_message=self._record_backlog)
        self.permission_mgr = SimpleNamespace(
            get_permission_level=lambda qq: level,
            get_nickname=lambda qq: "",
        )
        self.qq_client = SimpleNamespace(
            needs_attention=True,
            self_id=self_id,
            send_group_poke=self._poke,
            is_group_muted=lambda gid: False,
        )
        self._qq_settings = {"backlog_labels": []}
        self.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
        self._emit_log = lambda level_, msg: self.emitted.append((level_, msg))

    _sanitize_message_text = staticmethod(lambda text, **kw: text)
    _build_session_key = staticmethod(lambda **kw: "session")

    async def _record_backlog(self, message):
        self.backlog.append(message)

    async def _poke(self, group_id, user_id):
        self.pokes.append((str(group_id), str(user_id)))

    async def _maybe_notify_backlog_summary(self, *args, **kwargs):
        return None


def _dispatcher(*, self_id: str = BOT, level: str = "trusted"):
    plugin = _Recorder(self_id=self_id, level=level)
    dispatcher = QQMessageDispatcher(plugin)

    async def _pipeline(*args, **kwargs):
        # 「再往下就是整条群聊管线」那一步：走到了就说明她真的要开口了。
        plugin.pipeline.append((args, kwargs))

    dispatcher.handle_group_message = _pipeline
    return dispatcher, plugin


def _poke(dispatcher, *, poker: str, target: str = BOT, group: str = GROUP, nickname: str = ""):
    message = {
        "message_type": "notice", "notice_type": "poke",
        "group_id": group, "user_id": poker, "target_id": target,
    }
    if nickname:
        message["sender"] = {"nickname": nickname}
    asyncio.run(dispatcher.handle_message(message))
    return message


def _logs(plugin) -> str:
    return " | ".join(msg for _lvl, msg in plugin.emitted)


# ── 一、戳她：只跟戳，不回话 ───────────────────────────────────────

def test_a_single_poke_gets_poked_back_without_talking():
    dispatcher, plugin = _dispatcher()
    _poke(dispatcher, poker=ALICE)

    assert plugin.pokes == [(GROUP, ALICE)], "戳她没有回戳"
    assert plugin.pipeline == [], "戳一戳不该进管线（她要的是只跟戳）"
    assert plugin.backlog == [], "戳一戳不该进 backlog（会被回溯补回喂给模型）"


def test_a_storm_pokes_everyone_back_instead_of_talking():
    """**本次改动的核心**：两个人戳 → 两个都跟戳，且**一个字的生成都不开**。

    旧行为在这里会走"会话模式"：不回戳、把合成消息注入管线让她说话（真机 15:20）。
    """
    dispatcher, plugin = _dispatcher()
    _poke(dispatcher, poker=ALICE)
    _poke(dispatcher, poker=BOB)

    assert plugin.pokes == [(GROUP, ALICE), (GROUP, BOB)], f"风暴里没有逐个跟戳: {plugin.pokes}"
    assert plugin.pipeline == [], "风暴仍然进了管线 —— 使用者要的是「不需要回复，只需要跟戳」"
    assert "风暴" in _logs(plugin), f"风暴没有留痕: {_logs(plugin)}"
    assert "只跟戳" in _logs(plugin), _logs(plugin)


def test_everyone_in_a_bigger_storm_is_poked_back():
    dispatcher, plugin = _dispatcher()
    for poker in (ALICE, BOB, CAROL):
        _poke(dispatcher, poker=poker)

    assert plugin.pokes == [(GROUP, ALICE), (GROUP, BOB), (GROUP, CAROL)]
    assert plugin.pipeline == []


def test_a_repeat_poker_is_not_poked_back_forever():
    """跟戳不等于陪到底：同一个人 5 分钟内最多回戳 2 次。"""
    dispatcher, plugin = _dispatcher()
    for _ in range(5):
        _poke(dispatcher, poker=ALICE)

    assert plugin.pokes == [(GROUP, ALICE), (GROUP, ALICE)], (
        f"同一人被回戳了 {len(plugin.pokes)} 次 —— 无限互戳"
    )
    assert plugin.pipeline == []


def test_a_storm_never_sets_at_bot():
    """旧实现给风暴消息打了 `is_at_bot=True`（等于"有人点名她"→ 抢焦点、上锁）。

    现在整条分支都不进管线，所以那条合成消息根本不存在了；这里钉住"返回前没改消息"。
    """
    dispatcher, plugin = _dispatcher()
    message = _poke(dispatcher, poker=ALICE)
    _poke(dispatcher, poker=BOB)

    assert "is_at_bot" not in message, "戳一戳的原始事件被改写了（不该动它）"
    assert plugin.pipeline == []


# ── 二、戳别人：维持原样（交给模型决定） ─────────────────────────────

def test_poking_someone_else_still_reaches_the_pipeline():
    """对照：她不是被戳对象时照旧进管线 —— 别把这条也一起关掉。"""
    dispatcher, plugin = _dispatcher()
    _poke(dispatcher, poker=ALICE, target=CAROL, nickname="爱丽丝")

    assert plugin.pokes == [], "戳的不是她，不该回戳"
    assert plugin.backlog, "戳别人的消息应该照常进 backlog"
    assert plugin.pipeline, "戳别人的消息应该照常走到群聊管线"
    assert "爱丽丝" in plugin.backlog[0].get("content", ""), plugin.backlog[0]


def test_unknown_self_id_treats_the_poke_as_poking_someone_else():
    """拿不到 self_id 时无法判断"是不是戳她" → 按戳别人处理（不冒然回戳）。"""
    dispatcher, plugin = _dispatcher(self_id="")
    _poke(dispatcher, poker=ALICE, target=BOT)

    assert plugin.pokes == []
    assert plugin.pipeline


# ── 三、边界 ───────────────────────────────────────────────────────

def test_missing_group_or_poker_is_dropped_quietly():
    dispatcher, plugin = _dispatcher()
    asyncio.run(dispatcher.handle_message({
        "message_type": "notice", "notice_type": "poke",
        "group_id": "", "user_id": ALICE, "target_id": BOT,
    }))
    asyncio.run(dispatcher.handle_message({
        "message_type": "notice", "notice_type": "poke",
        "group_id": GROUP, "user_id": "", "target_id": BOT,
    }))

    assert plugin.pokes == []
    assert plugin.pipeline == []


def test_a_failing_poke_back_is_only_logged():
    """跟戳失败不能把整条派发炸掉（回戳是装饰，失败就失败）。"""
    dispatcher, plugin = _dispatcher()

    async def _boom(*args, **kwargs):
        raise RuntimeError("napcat 掉线了")

    plugin.qq_client.send_group_poke = _boom
    _poke(dispatcher, poker=ALICE)

    assert "回戳失败" in _logs(plugin), _logs(plugin)


def test_blacklisted_poker_is_still_blocked_before_the_poke_back():
    """黑名单闸在戳一戳分支**之前** —— 这次改动没动那个顺序。"""
    dispatcher, plugin = _dispatcher(level="blacklist")
    _poke(dispatcher, poker=ALICE)

    assert plugin.pokes == [], "黑名单用户戳她，她回戳了"
    assert "用户黑名单过滤" in _logs(plugin), _logs(plugin)
