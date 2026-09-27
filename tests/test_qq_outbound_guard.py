# -*- coding: utf-8 -*-
"""出站守门人：内容安全（黑名单）+ 重复过滤。

为什么需要（2026-09-27 定的四项增益之 ①②）：

* **内容安全**：我们有三条把**别人原文**发出去的路 —— 跟着复读（逐字发出）、
  合并转发（附群友原文）、别的插件原文直发。原来的 `is_blacklisted` **只查入站**，
  所以群里有人发攻击性内容时，她可能原样再喊一遍。词表复用关键词页那份
  （`backlog_labels` 里 priority<0），用户不用维护第二份名单。
* **重复过滤**：同一个群窗口内不重发同一句，治"卡带"。两条豁免是刻意的：
  复读（本来就故意重复）与短应答（`嗯嗯`/`？` 重复是正常的，滤掉她会变哑巴）。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.outbound_guard_service import QQOutboundGuardService
from plugin.plugins.qq_auto_reply.repeat_echo_service import QQRepeatEchoService
from plugin.plugins.qq_auto_reply.reply_delivery_node import QQReplyDeliveryNode

GROUP = "1048307485"
OTHER_GROUP = "985066274"

#: 与关键词页同构的黑名单：priority<0 的标签即"不许出现"。
BLACKLIST_LABELS = [
    {"id": "chat", "label": "闲聊", "keywords": [], "priority": 0},
    {"id": "ban", "label": "黑名单", "keywords": ["傻逼", "滚"], "priority": -100},
]


class _FakeQQClient:
    needs_attention = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def is_connected(self) -> bool:
        return True

    async def send_group_message(self, group_id: str, message: str):
        self.sent.append((group_id, message))
        return "mid-1"


def _guard(**settings) -> QQOutboundGuardService:
    base = {
        "backlog_labels": BLACKLIST_LABELS,
        "outbound_guard_enabled": True,
        "outbound_blacklist_enabled": True,
        "outbound_dedup_enabled": True,
        "outbound_dedup_window_seconds": 1800,
        "outbound_dedup_min_chars": 4,
    }
    base.update(settings)
    plugin = SimpleNamespace(
        _qq_settings=base,
        logger=logging.getLogger("qq.guard"),
        _emit_log=lambda *a, **k: None,
    )
    return QQOutboundGuardService(plugin)


# ── ① 内容安全 ──────────────────────────────────────────────────────

def test_blacklisted_text_is_blocked():
    verdict = _guard().check(group_id=GROUP, text="你这个傻逼")
    assert verdict.blocked and verdict.reason == "blacklist"
    assert verdict.detail == "傻逼", "日志要能说出命中哪个词，否则用户不知道删哪条"


def test_clean_text_passes():
    assert _guard().check(group_id=GROUP, text="今天天气不错").allowed


def test_blacklist_can_be_switched_off_alone():
    guard = _guard(outbound_blacklist_enabled=False)
    assert guard.check(group_id=GROUP, text="你这个傻逼").allowed
    # 关掉黑名单不该顺手关掉去重
    assert guard.check(group_id=GROUP, text="同一句话同一句话").allowed
    assert guard.check(group_id=GROUP, text="同一句话同一句话").reason == "duplicate"


def test_total_switch_off_restores_old_behaviour():
    guard = _guard(outbound_guard_enabled=False)
    assert guard.check(group_id=GROUP, text="你这个傻逼").allowed
    assert guard.check(group_id=GROUP, text="重复句子重复句子").allowed
    assert guard.check(group_id=GROUP, text="重复句子重复句子").allowed


def test_blacklist_applies_to_echo_too():
    """复读是把别人的原话逐字喊一遍 —— 内容安全这一道必须过。"""
    assert _guard().check(group_id=GROUP, text="滚", kind="echo").reason == "blacklist"


# ── ② 重复过滤 ──────────────────────────────────────────────────────

def test_same_sentence_in_the_same_group_is_blocked_once():
    guard = _guard()
    assert guard.check(group_id=GROUP, text="我来啦我来啦").allowed
    second = guard.check(group_id=GROUP, text="我来啦我来啦")
    assert second.blocked and second.reason == "duplicate"


def test_other_group_is_not_affected():
    guard = _guard()
    guard.check(group_id=GROUP, text="我来啦我来啦")
    assert guard.check(group_id=OTHER_GROUP, text="我来啦我来啦").allowed


def test_window_expiry_lets_it_through_again():
    guard = _guard(outbound_dedup_window_seconds=60)
    # 用注入时钟：`check` 的窗口判断以「当前时刻 - 上次发送时刻」算
    assert guard.check(group_id=GROUP, text="我来啦我来啦", now=1000.0).allowed
    assert guard.check(group_id=GROUP, text="我来啦我来啦", now=1030.0).reason == "duplicate"
    assert guard.check(group_id=GROUP, text="我来啦我来啦", now=1061.0).allowed, (
        "窗口（60s）之外应该允许再说一次"
    )


def test_echo_is_exempt_from_dedup():
    """跟着复读本来就是故意重复：被去重拦掉等于关掉这个功能。"""
    guard = _guard()
    guard.check(group_id=GROUP, text="一江大气喵")
    assert guard.check(group_id=GROUP, text="一江大气喵", kind="echo").allowed


def test_forward_is_exempt_from_dedup():
    guard = _guard()
    guard.check(group_id=GROUP, text="我赢了，原文如下")
    assert guard.check(group_id=GROUP, text="我赢了，原文如下", kind="forward").allowed


def test_short_replies_are_exempt():
    """`嗯嗯`/`？` 这类应答重复是正常的，滤掉会把她变成哑巴。"""
    guard = _guard()
    assert guard.check(group_id=GROUP, text="嗯嗯").allowed
    assert guard.check(group_id=GROUP, text="嗯嗯").allowed
    assert guard.check(group_id=GROUP, text="？").allowed
    assert guard.check(group_id=GROUP, text="？").allowed


def test_fingerprint_ignores_cq_codes_spaces_and_punctuation():
    """带引用前缀/空格/标点的同一句要算同一句（否则去重形同虚设）。"""
    guard = _guard()
    assert guard.check(group_id=GROUP, text="[CQ:reply,id=1252066434]我来啦我来啦").allowed
    assert guard.check(group_id=GROUP, text="我来啦，我来啦！").reason == "duplicate"


def test_window_setting_controls_pruning():
    guard = _guard(outbound_dedup_window_seconds=10)
    guard.note_sent(group_id=GROUP, text="第一句内容", now=100.0)
    guard.note_sent(group_id=GROUP, text="第二句内容", now=130.0)
    kept = [fp for fp, _ts in guard._recent[GROUP]]
    assert len(kept) == 1, "窗口外的那条应该在 note_sent 时就被清掉"


# ── 投递层 / 复读 / 桥接 三处接线 ───────────────────────────────────

class _DeliveryPlugin:
    def __init__(self, guard) -> None:
        self.qq_client = _FakeQQClient()
        self.outbound_guard_service = guard
        self.logger = logging.getLogger("qq.delivery")
        self._emit_log = lambda *a, **k: None
        self._qq_settings = {}

    def _get_reply_mode(self) -> str:
        return "text"


def test_delivery_blocks_before_sending():
    guard = _guard()
    plugin = _DeliveryPlugin(guard)
    node = QQReplyDeliveryNode(plugin)
    plan = SimpleNamespace(target_type="group", target_id=GROUP, fallback_to_text_on_voice_failure=True)

    assert asyncio.run(node._send_text(plan, None, "你这个傻逼")) is False
    assert plugin.qq_client.sent == [], "被拦的文本不许发出去"

    assert asyncio.run(node._send_text(plan, None, "正常一句话")) is True
    assert plugin.qq_client.sent == [(GROUP, "正常一句话")]


def test_delivery_private_is_not_guarded():
    """私聊不查：那是主人自己的对话，误伤代价更高（也免得黑名单词在私聊里哑掉）。"""
    guard = _guard()
    plugin = _DeliveryPlugin(guard)
    node = QQReplyDeliveryNode(plugin)

    async def _send_message(target, text):
        plugin.qq_client.sent.append((target, text))
        return "mid-p"

    plugin.qq_client.send_message = _send_message  # type: ignore[attr-defined]
    plan = SimpleNamespace(target_type="private", target_id="820040531", fallback_to_text_on_voice_failure=True)
    assert asyncio.run(node._send_text(plan, None, "你这个傻逼")) is True


class _EchoAttention:
    def _enabled(self) -> bool:
        return True

    def get_focus_group(self):
        return GROUP


def _echo_service(guard):
    client = _FakeQQClient()
    plugin = SimpleNamespace(
        logger=logging.getLogger("qq.echo"),
        _emit_log=lambda *a, **k: None,
        qq_client=client,
        attention_service=_EchoAttention(),
        attention_gate_service=SimpleNamespace(on_reply_sent=lambda gid: None),
        outbound_guard_service=guard,
        _qq_settings={},
        _user_sessions={},
    )
    plugin._ensure_qq_client_connected = lambda: None
    plugin._validate_group_id = lambda gid: str(gid)
    plugin._validate_outbound_message = lambda text: str(text).strip()
    service = QQRepeatEchoService(plugin)
    return service, client


def _echo_six(service, text: str) -> str | None:
    """模拟 6 个不同的人复读同一句（复读服务的触发前置），返回最后一次 observe 的结论。"""
    result: str | None = None
    for index in range(6):
        result = service.observe(group_id=GROUP, sender_id=f"u{index}", text=text, now=1000 + index)
    return result


def test_echo_is_blocked_by_blacklist_and_released():
    """黑名单内容不跟着复读；而且**不落冷却**（这样内容换了还能跟）。"""
    guard = _guard()
    service, client = _echo_service(guard)
    assert _echo_six(service, "滚") == "滚", "六个人复读同一句，服务本身应该决定要跟"

    assert asyncio.run(service._send(group_id=GROUP, text="滚")) is False
    assert client.sent == [], "被守门拦下的复读不许发出去"


def test_echo_passes_when_content_is_clean():
    guard = _guard()
    service, client = _echo_service(guard)
    assert _echo_six(service, "一江大气喵") == "一江大气喵"

    assert asyncio.run(service._send(group_id=GROUP, text="一江大气喵")) is True
    assert client.sent == [(GROUP, "一江大气喵")]
