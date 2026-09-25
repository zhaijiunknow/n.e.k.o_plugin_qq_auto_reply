"""回复消耗不得把群直接打入回落相位。

现场（`backlog_state.json`，两个群都是这个终局）：

    { "attention_score": 0.775, "phase": "fall", "last_focus_reason": "reply_consume" }
    { "attention_score": 0.0,   "phase": "fall", "last_focus_reason": "reply_consume" }

``update_on_reply`` 曾经无条件 ``phase = "fall"`` 并覆盖 ``phase_started_at``，
于是**每次回复都把蜜月掐断**、逼这个群重新熬满 ``attention_fall_seconds``。
用户配置下那是 240 秒，期间消息加成只剩 ``attention_fall_boost_attenuation``
（0.3）、分数还按 ``attention_fall_rate`` 往下掉 —— 回复一次等于判这个群四分钟
不说话，且分数注定跌破 ``group_attention_focus_send_threshold`` 的发送门控线，
后续消息全被 ``focus_low_attention`` 静默忽略（用户看到的就是"发一句就没有后文"）。

回落本身没被删掉：它仍由 ``_advance_phase`` 依
``focus_acquired_at + attention_honeymoon_seconds`` 判定，或被别的群抢走焦点。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply.attention_service import (
    QQAttentionService,
    QQGroupAttentionState,
)

#: 用户 business_config.json 里的实际注意力参数
LIVE_SETTINGS = {
    "group_attention_max_score": 10.0,
    "group_attention_focus_threshold": 4.0,
    "group_attention_focus_send_threshold": 2.0,
    "group_attention_min_threshold": 1.0,
    "attention_base_rise_rate": 0.02,
    "attention_message_boost": 0.15,
    "attention_honeymoon_seconds": 60,
    "attention_fall_seconds": 240,
    "attention_fall_rate": 0.015,
    "attention_consume_ratio": 0.3,
    "attention_fall_boost_attenuation": 0.3,
    "backlog_labels": [],
}


def _service() -> tuple[QQAttentionService, list[int]]:
    plugin = SimpleNamespace(
        _qq_settings=dict(LIVE_SETTINGS),
        backlog_store=None,
        group_permission_mgr=None,
        permission_mgr=None,
        fatigue_service=None,
        _emit_log=lambda *a, **k: None,
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    service = QQAttentionService(plugin)
    now = [1000]
    service._current_time = lambda: now[0]
    return service, now


def test_reply_does_not_force_the_fall_phase():
    """回复只消耗分数，相位保持不变（修之前这里会变成 "fall"）。"""
    service, now = _service()
    state = QQGroupAttentionState(group_id="g1", attention_score=6.0, phase="rise")
    state.last_decay_at = now[0]
    state.focus_acquired_at = now[0]          # 刚夺冠，蜜月才刚开始
    service._write_state(state)

    asyncio.run(service.update_on_reply("g1"))

    after = service._load_state("g1")
    # 消耗语义已从「乘性」改为「绝对量」：cost = max_score × consume_ratio。
    # 从真实常量推导（本文件桩里 ratio=0.3 ⇒ cost=3.0），不硬编码手算值。
    expected = 6.0 - service._max_attention() * service._consume_ratio()
    assert after.attention_score == pytest.approx(max(0.0, expected), rel=1e-3)
    assert after.phase == "rise", "回复不得把群打入回落"
    assert after.last_focus_reason == "reply_consume"


def test_reply_does_not_restart_the_fall_clock():
    """回复不得覆盖 ``phase_started_at``——否则回落计时被反复重置。"""
    service, now = _service()
    state = QQGroupAttentionState(group_id="g1", attention_score=6.0, phase="rise")
    state.last_decay_at = now[0]
    state.focus_acquired_at = now[0]
    state.phase_started_at = 500              # 一个明确的、回复前的值
    service._write_state(state)

    asyncio.run(service.update_on_reply("g1"))

    assert service._load_state("g1").phase_started_at == 500


def test_group_stays_above_the_send_gate_after_one_reply():
    """用户症状的直接反证：回复一次后，分数必须仍在发送门控线（2.0）之上。

    修之前：分数进入 240 秒回落，回落结束时已跌破 2.0，门控第 5 步
    （``focus_low_attention``）把后续消息全部忽略。
    """
    service, now = _service()
    # 一个刚夺冠、正在蜜月里的群，**且群里刚有人说过话**。
    #
    # last_message_at 必须贴近 now：自然增速现在按发言频率缩放
    # （_frequency_scale，目标间隔 30 秒）。若留 0，间隔是无穷大 → 被压到下限
    # 0.15×，60 秒只能涨 0.72 分，测出来的就不是「回复是否导致失焦」而是
    # 「冷群涨得慢」了。这里显式建一个活跃群，把被测行为隔离出来。
    # 用**使用者真实配置**建场景：consume_ratio=0.1、max_score=10
    # ⇒ 回复一次固定扣 1.0 分（减性，与当前分数无关）。
    # 本文件 `_service()` 的桩参数（consume_ratio=0.5 等）是给旧的乘性语义设计的，
    # 减性下 cost 会达到 5.0、超过起始分，所以这里显式覆盖成真实值。
    service, now = _service()
    service.plugin._qq_settings.update({
        "attention_consume_ratio": 0.1,
        "attention_honeymoon_seconds": 60,
        "attention_fall_seconds": 30,
    })
    state = QQGroupAttentionState(group_id="g1", attention_score=4.5, phase="rise")
    state.last_decay_at = now[0]
    state.focus_acquired_at = now[0]
    state.steady_since = now[0]              # 稳线起点 = 蜜月开始
    state.last_message_at = now[0]
    service._write_state(state)

    asyncio.run(service.update_on_reply("g1"))
    score_after_reply = service._load_state("g1").attention_score
    assert score_after_reply > 2.0, (
        f"回复一次后跌到 {score_after_reply:.2f}（门控线 2.0）—— "
        f"cost 应为 1.0 而不是随分数放大"
    )

    # 蜜月内推进 30 秒（仍在 60 秒蜜月窗口内）：rise 相位应回补，而不是下坠。
    now[0] += 30
    after = service._apply_decay(service._load_state("g1"), now[0])
    assert after.attention_score > score_after_reply, "蜜月期内应回补，而不是继续下坠"
    assert after.attention_score > 2.0, "且必须仍在发送门控线之上"
    assert after.phase == "rise", "蜜月未结束，不该进入回落"


def test_honeymoon_expiry_still_triggers_the_fall():
    """回落没有被删除：蜜月结束后仍必须进入 fall。

    ⚠️ 必须同时设 ``steady_since``：蜜月**从「稳线」起算，不从「刚到线」起算**。
    一个本来就高于焦点线的群若只设 focus_acquired_at，steady_since 为 0，
    fall 判定永远不会成立——这正是旧存档需要迁移的原因（见 load_cached_state）。
    """
    service, now = _service()
    state = QQGroupAttentionState(group_id="g1", attention_score=5.0, phase="rise")
    state.last_decay_at = now[0]
    state.focus_acquired_at = now[0]
    state.steady_since = now[0]               # 蜜月 60 秒，从此刻起算
    service._write_state(state)

    now[0] += 61
    after = service._apply_decay(service._load_state("g1"), now[0])
    assert after.phase == "fall"
