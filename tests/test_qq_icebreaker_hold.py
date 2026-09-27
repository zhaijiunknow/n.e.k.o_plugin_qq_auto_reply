# -*- coding: utf-8 -*-
"""主动破冰之后必须把焦点**按住** —— 真机 bug 的回归测试。

真机现场（2026-09-27 14:28，群 985066274 / 1048307485）：

    14:28:25  [Icebreaker] 破冰消息已发送: 你们最近有没有吃到什么超好吃的小零食呀？
    14:28:39  群友接了话
    14:28:41  她也答了（24 字）
    14:28:41  [AttentionGate] 焦点切换: 985066274 → 1048307485   ← 破冰当场白破
    14:28:41 ~ 14:29:42  最热闹的群连做 6 轮，破冰的这个群一句都没有

使用者的话是「发送破冰之后我回复了，猫娘没有后续」。根因在 `_try_icebreaker`
的成功路径：它只写了 `state.last_reply_at` ——

* 不锁（而 `_choose_focus_state` 的优先级 1 恰好就是锁），下一拍焦点就被更热闹的
  群按分数抢走，接她话的人作为 `non_focus` 被丢掉；
* 不清 `msgs_after_reply` → 接话反馈闭环（"有人接我的破冰吗"）没有起点，
  上一轮的计数会漏进这一轮，结算永远算在别人头上；
* 不记频率环 → 主动发言不计入 `[Pacing]` 软提示/硬闸的口径，她可以一边"刚说过话"
  一边被判定为"这个群我没怎么说话"。

本文件同时钉住"破冰已送出"这件事**不可被记账失败改写**：消息发出去了就返回成功，
后面的记账出错只许记 warning（空文本那次事故就是"发出去的东西被报成没发"）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.attention_gate_service import QQAttentionGateService
from plugin.plugins.qq_auto_reply.attention_service import QQAttentionService

#: 与使用者 business_config.json 一致的注意力参数
LIVE = {
    "attention_max_score": 10.0,
    "attention_focus_threshold": 4.0,
    "attention_focus_hold_threshold": 2.0,
    "attention_min_threshold": 1.0,
    "attention_base_rise_rate": 0.08,
    "attention_message_boost": 0.15,
    "attention_keyword_boost_ratio": 1.8,
    "attention_honeymoon_seconds": 60,
    "attention_fall_seconds": 30,
    "attention_fall_rate": 0.015,
    "attention_consume_ratio": 0.1,
    "attention_fall_boost_attenuation": 0.3,
    "attention_at_bot_boost": 3.0,
    "attention_question_boost": 1.5,
    "attention_wake_boost_ratio": 0.75,
    "attention_decay_interval_seconds": 5.0,
    "attention_frequency_target_gap": 30.0,
    "attention_frequency_min_multiplier": 0.15,
    "attention_frequency_max_multiplier": 1.8,
    "attention_emotion_multipliers": {"calm": 0.0},
    "attention_lock_seconds": 90,
    "attention_feedback_enabled": True,
    "attention_feedback_window_seconds": 90.0,
    "attention_feedback_silent_penalty": 0.4,
    "attention_feedback_warm_bonus": 0.4,
    "attention_feedback_warm_count": 3,
    "pacing_hint_enabled": True,
    "pacing_hint_ratio": 0.6,
    "backlog_labels": [],
}

#: 真机里那个刚破冰就被抢走焦点的群，与抢走它的热闹群
CALM_GROUP = "985066274"
BUSY_GROUP = "1048307485"


class _Clock:
    def __init__(self, start: int = 100_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now


def _service(**overrides):
    settings = dict(LIVE)
    settings.update(overrides)
    plugin = SimpleNamespace(
        group_permission_mgr=SimpleNamespace(
            list_groups=lambda: [{"group_id": CALM_GROUP}, {"group_id": BUSY_GROUP}]
        ),
        _qq_settings=settings,
        backlog_store=None,
        permission_mgr=None,
        _emit_log=lambda *a, **k: None,
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    svc = QQAttentionService(plugin)
    clock = _Clock()
    svc._current_time = clock
    return svc, clock


def _seed(svc, gid, score):
    st = svc._load_state(gid)
    st.attention_score = score
    svc._write_state(st)


def _focus_of(svc, clock) -> str:
    states = [svc._load_state(g) for g in (CALM_GROUP, BUSY_GROUP)]
    for s in states:
        svc._write_state(s)
    chosen = svc._choose_focus_state(states, clock.now)
    return chosen.group_id if chosen else ""


def _lock_until(svc, gid) -> int:
    return int(svc._load_state(gid).lock_until or 0)


# ── 一、按住焦点 ───────────────────────────────────────────────────

def test_icebreaker_hold_beats_a_much_hotter_group():
    """**本文件的核心断言**：破冰群 1.5 分、热闹群 9.0 分，按住期间焦点必须还在破冰群。

    这是真机 14:28:41 那一拍的复现：没有这一步，破冰发出 2 秒后焦点就被抢走。
    """
    svc, clock = _service()
    _seed(svc, CALM_GROUP, 1.5)      # 刚破冰的冷场群：分数很低
    _seed(svc, BUSY_GROUP, 9.0)      # 热闹的群：分数碾压

    svc.lock_group(CALM_GROUP, seconds=120, reason="icebreaker")
    assert _focus_of(svc, clock) == CALM_GROUP, (
        "破冰没有按住焦点：热闹的群立刻把它抢走了，接她话的人会被 non_focus 丢掉"
    )


def test_icebreaker_hold_expires_and_arbitration_resumes():
    """按住是**临时**的：到期必须交还给分数仲裁，不能变成长期霸占。"""
    svc, clock = _service()
    now = clock.now
    _seed(svc, CALM_GROUP, 1.5)
    _seed(svc, BUSY_GROUP, 9.0)
    svc.lock_group(CALM_GROUP, seconds=120, reason="icebreaker")

    clock.now = now + 119
    assert _focus_of(svc, clock) == CALM_GROUP
    clock.now = now + 121
    assert _focus_of(svc, clock) == BUSY_GROUP, "按住到期后应交还给分数最高的群"


def test_icebreaker_hold_defaults_to_the_configured_seconds():
    """不传 `seconds` 时仍是 `attention_lock_seconds`（@ 的既有行为，不能被改坏）。"""
    svc, clock = _service()
    now = clock.now
    svc.lock_group(CALM_GROUP)
    assert _lock_until(svc, CALM_GROUP) == now + 90


def test_explicit_hold_seconds_overrides_the_config():
    """破冰的来由与 @ 不同 —— 时长可以分开给。"""
    svc, clock = _service(attention_lock_seconds=90)
    now = clock.now
    svc.lock_group(CALM_GROUP, seconds=120, reason="icebreaker")
    assert _lock_until(svc, CALM_GROUP) == now + 120


def test_hold_seconds_zero_locks_nothing():
    """0 = 不按（可对照复现旧行为）。"""
    svc, clock = _service()
    _seed(svc, CALM_GROUP, 1.5)
    _seed(svc, BUSY_GROUP, 9.0)
    svc.lock_group(CALM_GROUP, seconds=0, reason="icebreaker")
    assert _lock_until(svc, CALM_GROUP) == 0
    assert _focus_of(svc, clock) == BUSY_GROUP


def test_icebreaker_reason_is_recorded_separately_from_at():
    """锁的来由要能事后分辨：`@` 是 `lock`，破冰是 `lock:icebreaker`。"""
    svc, _ = _service()
    svc.lock_group(CALM_GROUP)
    assert svc._load_state(CALM_GROUP).last_focus_reason == "lock"
    svc.lock_group(CALM_GROUP, seconds=120, reason="icebreaker")
    assert svc._load_state(CALM_GROUP).last_focus_reason == "lock:icebreaker"


# ── 二、主动发言的记账（不消耗注意力）───────────────────────────────

def test_note_proactive_speech_does_not_consume_attention():
    """主动开口**不扣分**：她不是"回完了该让位"，而是"我刚开口，等人接"。"""
    svc, clock = _service()
    _seed(svc, CALM_GROUP, 5.0)
    svc.note_proactive_speech(CALM_GROUP)
    assert float(svc._load_state(CALM_GROUP).attention_score) == 5.0


def test_note_proactive_speech_updates_activity_time():
    """要盖 `last_reply_at` —— 破冰的防重复触发依赖它。"""
    svc, clock = _service()
    svc.note_proactive_speech(CALM_GROUP)
    assert int(svc._load_state(CALM_GROUP).last_reply_at) == clock.now


def test_note_proactive_speech_starts_a_fresh_feedback_cycle():
    """`msgs_after_reply` 必须清零，且清零后**下一条**消息才算 1。

    不清零的话，上一轮遗留的 7 条会漏进这一轮，`_settle_feedback` 会把别人的旧消息
    算成"对破冰的回应"。
    """
    svc, clock = _service()
    st = svc._load_state(CALM_GROUP)
    st.msgs_after_reply = 7
    svc._write_state(st)

    svc.note_proactive_speech(CALM_GROUP)
    assert int(svc._load_state(CALM_GROUP).msgs_after_reply) == 0

    asyncio.run(svc.update_on_message({
        "group_id": CALM_GROUP, "user_id": "u1", "content": "有啊，薯片",
        "timestamp": clock.now, "is_at_bot": False,
    }))
    assert int(svc._load_state(CALM_GROUP).msgs_after_reply) == 1, (
        "破冰之后的第一条回应应当是 1 —— 计数没有从破冰那一刻重新开始"
    )


def test_note_proactive_speech_records_the_pacing_ring():
    """主动发言也是她说了话，频率软提示/硬闸的口径必须算上它。"""
    svc, clock = _service()
    assert svc.recent_reply_count(CALM_GROUP) == 0
    svc.note_proactive_speech(CALM_GROUP)
    assert svc.recent_reply_count(CALM_GROUP) == 1


def test_the_icebreaker_cycle_settles_as_warm():
    """**破冰开的那一轮真的会被结算**：3 条回应 + 过窗口 → `warm`（+0.4）。

    这才是"清 `msgs_after_reply`"要换来的东西。不清零的话，上一轮的旧计数会混进来，
    结算出来的档位就不是"这次破冰有没有人接"。
    """
    svc, clock = _service()
    _seed(svc, CALM_GROUP, 5.0)
    svc.note_proactive_speech(CALM_GROUP)
    for i in range(3):
        asyncio.run(svc.update_on_message({
            "group_id": CALM_GROUP, "user_id": f"u{i}", "content": "有！我最近迷上烤海苔",
            "timestamp": clock.now, "is_at_bot": False,
        }))
    clock.now += 91                       # 越过 attention_feedback_window_seconds
    asyncio.run(svc.decay_all())

    st = svc._load_state(CALM_GROUP)
    assert st.feedback_tier == "warm", f"破冰这一轮没结算成 warm: {st.feedback_tier!r}"
    assert int(st.feedback_msgs) == 3


def test_note_proactive_speech_ignores_an_empty_group_id():
    """空群号不记账、不抛（入口层传空是常事）。"""
    svc, _ = _service()
    svc.note_proactive_speech("")
    assert svc.recent_reply_count("") == 0


# ── 三、门控：成功送出破冰之后的两步 ───────────────────────────────

class _FakeAttention:
    """记录破冰之后被要求做了什么。"""

    def __init__(self, *, boom: bool = False) -> None:
        self.locks: list[tuple] = []
        self.notes: list[str] = []
        self._boom = boom

    def lock_group(self, group_id, *, seconds=None, reason="at"):
        self.locks.append((group_id, seconds, reason))

    def note_proactive_speech(self, group_id):
        self.notes.append(group_id)
        if self._boom:
            raise RuntimeError("记账炸了")


def _icebreaker_gate(*, attention, settings=None, action="reply", reply_text="你们最近有吃到什么好吃的零食吗？"):
    outcome = SimpleNamespace(action=action, reply_text=reply_text)

    async def _run(request):
        return outcome

    async def _with_lock(key, factory):
        return await factory()

    plugin = SimpleNamespace(
        _qq_settings={"proactive_topics": ["聊聊最近吃到的好东西"], **(settings or {})},
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        _emit_log=lambda *a, **k: None,
        reply_buffer_service=None,
        attention_service=attention,
        _admin_qq="820040531",
        session_memory_service=SimpleNamespace(
            session_history_len=lambda key: 0,
            record_synthetic_prompt_rows=lambda key, before: None,
        ),
        reply_pipeline=SimpleNamespace(run=_run),
        runtime_service=SimpleNamespace(record_pipeline_outcome=lambda **kw: None),
        _build_session_key=lambda **kw: f"group:{kw.get('group_id')}",
        _run_with_session_lock=_with_lock,
    )
    return QQAttentionGateService(plugin)


def test_successful_icebreaker_locks_then_notes():
    """送出成功 → 先按住焦点，再记账（顺序即语义：按住优先于记账）。"""
    attn = _FakeAttention()
    gate = _icebreaker_gate(attention=attn)
    assert asyncio.run(gate._try_icebreaker(CALM_GROUP)) is True
    assert attn.locks == [(CALM_GROUP, 120, "icebreaker")]
    assert attn.notes == [CALM_GROUP]


def test_icebreaker_hold_zero_still_notes_but_skips_the_lock():
    """`icebreaker_hold_seconds=0` → 只记账不按（关掉按住不等于关掉记账）。"""
    attn = _FakeAttention()
    gate = _icebreaker_gate(attention=attn, settings={"icebreaker_hold_seconds": 0})
    assert asyncio.run(gate._try_icebreaker(CALM_GROUP)) is True
    assert attn.locks == []
    assert attn.notes == [CALM_GROUP]


def test_broken_bookkeeping_cannot_turn_a_sent_icebreaker_into_a_failure():
    """**消息已经送出去了**，后面的记账炸了也只许 warning，不许报成失败。"""
    attn = _FakeAttention(boom=True)
    gate = _icebreaker_gate(attention=attn)
    assert asyncio.run(gate._try_icebreaker(CALM_GROUP)) is True, (
        "破冰已送出却返回 False：上层会记成失败，而群里的人已经看到消息了"
    )


def test_no_hold_when_nothing_was_sent():
    """模型决定不开口 → 不按、不记账、也不假装成功。"""
    attn = _FakeAttention()
    gate = _icebreaker_gate(attention=attn, action="skip", reply_text="")
    assert asyncio.run(gate._try_icebreaker(CALM_GROUP)) is False
    assert attn.locks == []
    assert attn.notes == []


def test_empty_reply_text_is_not_a_successful_icebreaker():
    """空文本不算送出（`action=reply` 但正文为空的那种脏 outcome）。"""
    attn = _FakeAttention()
    gate = _icebreaker_gate(attention=attn, action="reply", reply_text="")
    assert asyncio.run(gate._try_icebreaker(CALM_GROUP)) is False
    assert attn.locks == []


# ── 四、配置读取 ───────────────────────────────────────────────────

def test_hold_seconds_reads_the_default():
    gate = _icebreaker_gate(attention=_FakeAttention())
    assert gate._icebreaker_hold_seconds() == 120


def test_hold_seconds_honours_zero_and_negative():
    """0 是有意义的值，不能被 `or 120` 吞掉；负数当 0。"""
    assert _icebreaker_gate(
        attention=_FakeAttention(), settings={"icebreaker_hold_seconds": 0}
    )._icebreaker_hold_seconds() == 0
    assert _icebreaker_gate(
        attention=_FakeAttention(), settings={"icebreaker_hold_seconds": -5}
    )._icebreaker_hold_seconds() == 0


def test_hold_seconds_falls_back_on_garbage():
    """脏配置回落到默认值，而不是抛异常把破冰整个弄失败。"""
    assert _icebreaker_gate(
        attention=_FakeAttention(), settings={"icebreaker_hold_seconds": "abc"}
    )._icebreaker_hold_seconds() == 120
    assert _icebreaker_gate(
        attention=_FakeAttention(), settings={"icebreaker_hold_seconds": None}
    )._icebreaker_hold_seconds() == 120


def test_default_matches_between_the_schema_and_the_gate():
    """出厂值只有**一处**说法：门控的回落值必须与 settings_schema 的默认值相同。

    否则「配置里没有这个键」与「用户点一次保存」会得到两个不同的按住时长。
    """
    from plugin.plugins.qq_auto_reply import settings_schema

    spec = settings_schema.BY_KEY["icebreaker_hold_seconds"]
    assert spec.default == 120
    assert spec.floor == 0, "地板必须是 0 —— 0 是「关掉按住」这个合法取值"
    assert spec.saveable is True
    assert "icebreaker_hold_seconds" in settings_schema.SAVEABLE_KEYS

    gate = _icebreaker_gate(attention=_FakeAttention(), settings={})
    assert gate._icebreaker_hold_seconds() == spec.default
