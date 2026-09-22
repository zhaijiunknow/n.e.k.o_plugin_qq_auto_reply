"""新暴露的节奏/疲劳参数：改配置 → 行为真的跟着变。

归并到 ``settings_schema`` 只保证"存得下、读得到"；这些测试保证**读到了会生效**。
每条都直连消费点，不经界面。
"""
from __future__ import annotations

from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.attention_gate_service import QQAttentionGateService
from plugin.plugins.qq_auto_reply.fatigue_service import QQFatigueService
from plugin.plugins.qq_auto_reply.reply_buffer_service import QQReplyBufferService


def _fatigue(settings: dict) -> QQFatigueService:
    return QQFatigueService(SimpleNamespace(_qq_settings=dict(settings)))


# ── 疲劳总开关 ────────────────────────────────────────────────────────────

def test_fatigue_disabled_returns_zero_and_stops_accumulating():
    """关掉疲劳：恒为 0，且**不再堆积**全局消息时间戳。

    后半句是这条测试的真正标的：``_global_msg_timestamps`` 的唯一清理点在读路径里，
    ``calculate_fatigue`` 一旦短路那条路径就不可达 —— 如果 ``record_incoming_message``
    不一起短路，这个列表会随每条入站消息无界增长（永不回收）。
    """
    svc = _fatigue({"fatigue_enabled": False})

    for _ in range(500):
        svc.record_incoming_message()
        svc.mark_active("group:1")

    assert svc.calculate_fatigue("group:1") == 0.0
    assert list(svc._global_msg_timestamps) == []
    assert dict(svc._session_fatigue_values) == {}


def test_fatigue_enabled_accumulates_as_before():
    """开着（默认）时行为与历史一致：有值、有记录。"""
    svc = _fatigue({"fatigue_enabled": True})

    for _ in range(50):
        svc.record_incoming_message()

    assert len(svc._global_msg_timestamps) == 50
    assert svc.calculate_fatigue("group:1") > 0.0


def test_missing_fatigue_key_stays_enabled():
    """**缺键按开** —— 老配置里没有这个键，行为必须与历史一致。"""
    svc = _fatigue({})

    svc.record_incoming_message()

    assert len(svc._global_msg_timestamps) == 1


def test_time_context_survives_fatigue_being_disabled():
    """时间上下文与疲劳无关，关掉疲劳也必须照常产出。

    这条挡的是"用不构造服务来关疲劳"那种改法 —— 那会把时间层提示词一起静默降级。
    """
    for enabled in (True, False):
        context = _fatigue({"fatigue_enabled": enabled}).get_dynamic_time_context()
        assert isinstance(context, str) and context.strip()


# ── 回复爆发闸 ────────────────────────────────────────────────────────────

def _gate(settings: dict) -> QQAttentionGateService:
    gate = QQAttentionGateService.__new__(QQAttentionGateService)
    gate.plugin = SimpleNamespace(_qq_settings=dict(settings), _emit_log=lambda *a, **k: None)
    gate._reply_timestamps = {}
    return gate


def test_burst_window_and_max_are_configurable():
    """默认 60s/3 与历史一致；两个键都改得动。"""
    now = 10_000

    # 默认：窗口内 3 条 → 拦第 4 条
    gate = _gate({})
    gate._reply_timestamps["g"] = [now - 1, now - 2, now - 3]
    assert gate._check_reply_burst("g", now) is True
    gate._reply_timestamps["g"] = [now - 1, now - 2]
    assert gate._check_reply_burst("g", now) is False

    # 放宽到 5 条 → 3 条不再触发
    loose = _gate({"reply_burst_max_replies": 5})
    loose._reply_timestamps["g"] = [now - 1, now - 2, now - 3]
    assert loose._check_reply_burst("g", now) is False

    # 缩短窗口到 5 秒 → 10 秒前的那条不再计入
    short = _gate({"reply_burst_window_seconds": 5})
    short._reply_timestamps["g"] = [now - 10, now - 1, now - 2]
    assert short._check_reply_burst("g", now) is False


def test_burst_never_locks_out_completely():
    """上限被钳到 ≥1：配 0 也不能变成"永远静默"。"""
    gate = _gate({"reply_burst_max_replies": 0, "reply_burst_window_seconds": 0})
    assert gate._check_reply_burst("g", 10_000) is False


# ── 回复节奏取样 ──────────────────────────────────────────────────────────

def test_buffer_sampler_uses_configured_band():
    """配了区间就落在区间里（这是"手感可调"的直接证据）。"""
    settings = {
        "buffer_delay_mean_seconds": 20.0,
        "buffer_delay_sigma_seconds": 0.1,
        "buffer_delay_min_seconds": 19.0,
        "buffer_delay_max_seconds": 21.0,
    }
    samples = [QQReplyBufferService.sample_wait_seconds(settings=settings) for _ in range(50)]
    assert all(19.0 <= s <= 21.0 for s in samples), samples


def test_buffer_sampler_zero_delay_is_respected():
    """延迟设成 0 有意义（立即发），不能被 ``or`` 当未设置吞掉。"""
    settings = {"buffer_delay_mean_seconds": 0.0, "buffer_delay_sigma_seconds": 0.0,
                "buffer_delay_min_seconds": 0.0, "buffer_delay_max_seconds": 0.0}
    assert QQReplyBufferService.sample_wait_seconds(settings=settings) == 0.0


def test_buffer_sampler_without_settings_is_unchanged():
    """不传 settings 的旧调用方（既有测试）行为不变。"""
    samples = [QQReplyBufferService.sample_wait_seconds(private=False) for _ in range(50)]
    assert all(
        QQReplyBufferService.MIN_WAIT_SECONDS <= s <= QQReplyBufferService.MAX_WAIT_SECONDS
        for s in samples
    )


def test_buffer_max_count_is_configurable():
    """缓冲上限可配，"我在听"的门槛跟着推导（否则调小上限会让那条分支永久不可达）。"""
    svc = QQReplyBufferService.__new__(QQReplyBufferService)
    svc.plugin = SimpleNamespace(_qq_settings={})
    assert svc._max_buffer_count() == 17
    assert svc._ack_floor() == 10

    svc.plugin = SimpleNamespace(_qq_settings={"buffer_max_count": 6})
    assert svc._max_buffer_count() == 6
    assert svc._ack_floor() == 5  # min(10, 6-1)

    svc.plugin = SimpleNamespace(_qq_settings={"buffer_max_count": 1})
    assert svc._max_buffer_count() == 1
    assert svc._ack_floor() == 1  # 不越界
