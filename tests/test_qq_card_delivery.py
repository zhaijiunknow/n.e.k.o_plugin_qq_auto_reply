# -*- coding: utf-8 -*-
"""卡片（`<ark>`）在官方通道上要真的发成卡片，别的地方降级成文字。

官方 v2 的发送侧已经没有 ark：#3709 的适配器如实把 ``supports_ark_cards`` 报成 False，
另报 ``supports_card_messages``（图文卡片，``msg_type=8``），``send_group_ark_card`` 仍然可用。
只认 ``supports_ark_cards`` 的调用方会把每张卡片都降级成文字 —— 在官方通道上等于整个卡片
功能没了。

这里钉三段，缺一段卡片都发不出去：

1. **解析**：提示词示例里的 `<ark …>正文</ark>` 不包 `<msg>`。以前它不在旧式标签的识别
   名单里，整段被当成纯文本，标签壳在投递时被剥掉 —— 发出去的是一句普通的话。
2. **投递**：投递层对卡片块以前只记一句「没有投递实现」，什么都不发。
3. **降级**：发不了卡片的地方（OneBot、私聊、只发语音）内容照发，按文字；卡片发送失败
   也改发文字，而且只发一次。
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply.pipeline_models import (
    QQDeliveryPlan,
    QQMessageBlock,
    delivered_blocks_text,
)
from plugin.plugins.qq_auto_reply.reply_delivery_node import QQReplyDeliveryNode
from plugin.plugins.qq_auto_reply.reply_postprocess_node import QQReplyPostprocessNode

PLUGIN = pathlib.Path(__file__).resolve().parents[1]

ARK = {"title": "运行状态", "desc": "已在线 3 小时", "url": "https://example.com/s", "pic": "https://example.com/p.png",
       "_body": "当前一切正常"}


class _Client:
    """记录发出去的东西。``cards`` 为真 = #3709 之后的官方通道。"""

    def __init__(self, *, cards: bool, card_result=True) -> None:
        self.needs_attention = not cards
        self.supports_ark_cards = False          # 官方 v2 如实报 False；OneBot 本来就是 False
        if cards:
            self.supports_card_messages = True
        self.card_result = card_result
        self.card_calls: list[tuple] = []
        self.texts: list[tuple[str, str]] = []

    async def send_group_ark_card(self, group_id, ark_obj, **kwargs):
        self.card_calls.append((str(group_id), dict(ark_obj), kwargs))
        if isinstance(self.card_result, Exception):
            raise self.card_result
        return self.card_result

    async def send_group_message(self, group_id, text, **kw):
        self.texts.append((str(group_id), str(text)))
        return "m-text"

    async def send_message(self, user_id, text, **kw):
        self.texts.append((str(user_id), str(text)))
        return "m-text"

    async def send_group_message_segments(self, group_id, segments, **kw):
        self.texts.append((str(group_id), "".join(
            f"[reply:{seg['data']['id']}]" if seg.get("type") == "reply" else str(seg.get("data", {}).get("text", ""))
            for seg in segments)))
        return "m-text"


class _Guard:
    """出站守门的替身：同一段文字第二次来查就拦（真的那个也是查过即记账）。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def check(self, *, group_id, text, kind="reply"):
        blocked = text in self.seen
        self.seen.append(text)
        return SimpleNamespace(blocked=blocked, reason="duplicate", detail="0s 前发过")


def _node(client, *, mode="text"):
    plugin = SimpleNamespace(
        qq_client=client,
        outbound_guard_service=_Guard(),
        logger=SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
        _emit_log=lambda *a, **k: None,
        _mask_token=lambda t: t,
        _get_reply_mode=lambda: mode,
        i18n=SimpleNamespace(t=lambda key, default=None: default or key),
    )
    return QQReplyDeliveryNode(plugin)


def _plan(*, group=True, reply_to="m-in"):
    return QQDeliveryPlan(
        target_type="group" if group else "private", target_id="G1" if group else "U1",
        blocks=[QQMessageBlock(ark=dict(ARK), reply_to=reply_to)],
    )


# ── 投递 ────────────────────────────────────────────────────────────

def test_the_official_channel_sends_a_real_card():
    client = _Client(cards=True)
    result = asyncio.run(_node(client).deliver(_plan()))

    assert result.delivered is True
    assert client.texts == [], "卡片发成功了就不该再补一条文字"
    (group_id, card, kwargs), = client.card_calls
    assert group_id == "G1"
    assert card == {"title": "运行状态", "description": "已在线 3 小时\n当前一切正常",
                    "url": "https://example.com/s", "pic_url": "https://example.com/p.png"}
    assert kwargs == {"reply_message_id": "m-in"}, "要带上被动回复的锚点，否则算主动消息"
    assert "运行状态" in (result.reply_text or "")


def test_the_gate_is_supports_card_messages_not_supports_ark_cards():
    """``supports_ark_cards`` 为 True 而没有 ``supports_card_messages`` 的连接：不当成能发卡片。"""
    client = _Client(cards=False)
    client.supports_ark_cards = True
    asyncio.run(_node(client).deliver(_plan()))
    assert client.card_calls == [] and len(client.texts) == 1


@pytest.mark.parametrize("case", ["onebot", "private", "voice-only"])
def test_where_cards_cannot_go_the_content_still_arrives_as_text(case, monkeypatch):
    client = _Client(cards=case != "onebot")
    node = _node(client, mode="voice" if case == "voice-only" else "text")
    spoken: list[str] = []

    async def _speak(target, text, **kw):
        spoken.append(text)
        return True

    node.plugin._deliver_group_reply = _speak
    result = asyncio.run(node.deliver(_plan(group=case != "private")))

    assert result.delivered is True and client.card_calls == []
    sent = spoken[0] if case == "voice-only" else client.texts[0][1]
    for part in ("运行状态", "已在线 3 小时", "当前一切正常"):
        assert part in sent
    assert "<ark" not in sent


@pytest.mark.parametrize("failure", [False, None, RuntimeError("card refused")])
def test_a_card_that_fails_falls_back_to_text_exactly_once(failure):
    """失败后改发文字；这段文字已经过了守门，不能因为"刚查过一次"被当成重复拦掉。"""
    client = _Client(cards=True, card_result=failure)
    result = asyncio.run(_node(client).deliver(_plan()))

    assert len(client.card_calls) == 1
    assert len(client.texts) == 1 and "当前一切正常" in client.texts[0][1]
    assert client.texts[0][1].startswith("[reply:m-in]"), "降级后的文字仍然是对那条消息的被动回复"
    assert result.delivered is True


def test_an_empty_card_is_not_delivered():
    client = _Client(cards=True)
    plan = QQDeliveryPlan(target_type="group", target_id="G1", blocks=[QQMessageBlock(ark={"btn": "看看"})])
    result = asyncio.run(_node(client).deliver(plan))
    assert result.delivered is False and client.card_calls == [] and client.texts == []


# ── 解析 ────────────────────────────────────────────────────────────

def test_a_bare_ark_tag_is_parsed_as_a_card():
    """提示词里的示例就是这个写法（不包 `<msg>`）。"""
    blocks = QQReplyPostprocessNode._parse_blocks('<ark title="运行状态" desc="已在线 3 小时">当前一切正常</ark>')
    assert len(blocks) == 1
    assert blocks[0].ark == {"title": "运行状态", "desc": "已在线 3 小时", "_body": "当前一切正常"}
    assert blocks[0].text == ""


def test_text_around_a_bare_ark_tag_becomes_its_own_block():
    blocks = QQReplyPostprocessNode._parse_blocks('给你看看：<ark title="状态" url="https://a.example/x"/>')
    assert [(b.text, b.ark) for b in blocks] == [("给你看看：", {}), ("", {"title": "状态", "url": "https://a.example/x"})]


def test_an_ark_inside_msg_still_parses():
    blocks = QQReplyPostprocessNode._parse_blocks('<msg><ark title="状态" desc="在线">正文</ark></msg>')
    assert blocks[0].ark == {"title": "状态", "desc": "在线", "_body": "正文"}


def test_a_card_only_reply_is_anchored_to_the_incoming_message():
    """只有卡片的回复也要挂上来信的 id：开放平台上没有它就是主动消息，占额度、还可能被拒。"""
    from plugin.plugins.qq_auto_reply.pipeline_models import KIND_INCOMING_GROUP

    request = SimpleNamespace(current_message_id="m-in", source_kind=KIND_INCOMING_GROUP, group_context=None)
    blocks = QQReplyPostprocessNode._address_group_blocks(request, [QQMessageBlock(ark=dict(ARK))])
    assert blocks[0].reply_to == "m-in"


# ── 记账 ────────────────────────────────────────────────────────────

def test_what_the_card_said_is_part_of_the_delivered_text():
    text = delivered_blocks_text([QQMessageBlock(ark=dict(ARK))])
    assert text.splitlines() == ["运行状态", "已在线 3 小时", "当前一切正常"]


# ── 防回归：别再有人按 supports_ark_cards 判断 ───────────────────────

def test_nothing_in_the_plugin_gates_on_supports_ark_cards():
    offenders = []
    for path in PLUGIN.glob("*.py"):
        source = re.sub(r'""".*?"""', "", path.read_text(encoding="utf-8"), flags=re.S)
        for number, line in enumerate(source.splitlines(), 1):
            if "supports_ark_cards" in line and not line.strip().startswith("#"):
                offenders.append(f"{path.name}: {line.strip()[:80]}")
    assert not offenders, "这些地方还在读 supports_ark_cards（官方 v2 上它恒为 False）:\n" + "\n".join(offenders)
    assert "_send_ark" not in (PLUGIN / "reply_pipeline.py").read_text(encoding="utf-8"), (
        "reply_pipeline 里那份没人调用的卡片发送又回来了 —— 卡片只在 reply_delivery_node 里发"
    )
