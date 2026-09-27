# -*- coding: utf-8 -*-
"""破冰没人接 → 该群休眠；@ 可唤醒（使用者 2026-09-27 的口径）。

> 「需要修一下破冰，如果破冰一次还是没有人接话，可以直接把这个群拖入休眠状态，
>   用其他群竞态。休眠的群可以用 @ 唤醒」

设计与「锁」是**相反**的两个信号，刻意不合并：

| | 锁（`@` / 唤醒词） | 休眠（破冰没人接） |
| --- | --- | --- |
| 含义 | 有人叫我，我独占焦点 | 我主动开口也没人理，我把位置让出去 |
| 效果 | 期内**只**看这个群 | 期内**不参与**竞争，别的群自由竞态 |
| 结束 | 到期 | @ / 引用她 / 到期 |

两个容易写错的地方，本文件各有一条用例钉住：

1. **判据只认破冰那一轮**（`proactive_pending`）：普通回复没人接是常态（她在热闹群
   里插一句本就未必有人应），拿它当休眠理由会把好群一个个睡掉；
2. **全员休眠不等于全体静音**：如果所有群都在睡，必须退回"最高分群"而不是返回
   空焦点 —— 否则所有群的消息都会被判 `non_focus`，那不是让位，是彻底不说话。

「没人接」用的是现成的接话反馈窗口（`attention_feedback_window_seconds`，默认 90s），
不另开计时器：同一个结论不该有两个判据。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.attention_service import QQAttentionService

CALM = "985066274"      # 破冰的那个群
BUSY = "1048307485"     # 另一个群

BASE = {
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
    # 本次要测的：
    "icebreaker_hold_seconds": 120,
    # 使用者口径「**没 @ 一直休**」：0 = 不自动醒
    "icebreaker_dormant_enabled": True,
    "icebreaker_dormant_seconds": 0,
}


class _Clock:
    def __init__(self, start: int = 100_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now


def _service(**overrides):
    settings = dict(BASE)
    settings.update(overrides)
    plugin = SimpleNamespace(
        group_permission_mgr=SimpleNamespace(
            list_groups=lambda: [{"group_id": CALM}, {"group_id": BUSY}]
        ),
        _qq_settings=settings,
        backlog_store=None,
        permission_mgr=None,
        _emit_log=lambda *a, **k: None,
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        _maybe_push_status_event=lambda: None,
    )
    svc = QQAttentionService(plugin)
    clock = _Clock()
    svc._current_time = clock
    return svc, clock


def _seed(svc, gid, score, *, focus: bool = False):
    st = svc._load_state(gid)
    st.attention_score = score
    if focus:
        st.focus_acquired_at = 100_000
        st.last_focus_at = 100_000
    svc._write_state(st)


def _focus_of(svc, clock) -> str:
    states = [svc._load_state(g) for g in (CALM, BUSY)]
    for s in states:
        svc._write_state(s)
    chosen = svc._choose_focus_state(states, clock.now)
    return chosen.group_id if chosen else ""


def _message(svc, gid, clock):
    asyncio.run(svc.update_on_message({
        "group_id": gid, "user_id": "u1", "content": "接一句",
        "timestamp": clock.now, "is_at_bot": False,
    }))


def _reply(svc, gid):
    asyncio.run(svc.update_on_reply(gid))


def _tick(svc):
    asyncio.run(svc.decay_all())


def _break_ice_and_wait(svc, clock, *, group=CALM, wait=91):
    """走一遍真实时序：破冰开口 → 等过反馈窗口 → 让衰减轮次结算。"""
    svc.note_proactive_speech(group)
    clock.now += wait
    _tick(svc)


# ── 一、破冰没人接 → 休眠 ───────────────────────────────────────────

def test_silent_icebreaker_puts_the_group_to_sleep():
    """**本文件的核心**：破冰后过满反馈窗口仍无人接话 → 该群进入休眠。"""
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _break_ice_and_wait(svc, clock)

    st = svc._load_state(CALM)
    assert svc.is_dormant(CALM) is True, "破冰没人接却没有休眠"
    assert st.dormant_forever is True, "默认口径是「没 @ 一直休」"
    assert int(st.dormant_until) == 0
    assert st.last_focus_reason == "dormant:icebreaker_no_reply"
    assert st.feedback_tier == "silent"


def test_sleeping_group_stops_competing_so_others_take_over():
    """**让其他群竞态**：睡觉的群哪怕分数高得多，也不该再占着焦点。"""
    svc, clock = _service()
    _seed(svc, CALM, 9.0, focus=True)      # 破冰群分数极高、本来还持有焦点
    _seed(svc, BUSY, 1.0)                  # 另一个群冷得多
    _break_ice_and_wait(svc, clock)

    assert _focus_of(svc, clock) == BUSY, (
        "休眠群还在抢焦点：其他群拿不到竞态机会（本用例就是使用者要的那个行为）"
    )


def test_no_at_means_it_keeps_sleeping():
    """**使用者口径「没 @ 一直休」**：时间过去多久都不会自己醒。

    时间往未来推一年，休眠群仍然不参与竞争 —— 群里自己聊热了也不会提前结束
    （这是使用者明确选的那一项："只有 @ 能提前唤醒"）。
    """
    svc, clock = _service()
    _seed(svc, CALM, 9.0, focus=True)
    _seed(svc, BUSY, 1.0)
    _break_ice_and_wait(svc, clock)

    clock.now += 365 * 24 * 3600
    assert svc.is_dormant(CALM) is True, "没人 @ 却自己醒了 —— 与「没 @ 一直休」不符"
    assert _focus_of(svc, clock) == BUSY
    # 群里聊得再热闹也不醒（分数会涨，但不会参与竞争）
    _seed(svc, CALM, 10.0)
    assert _focus_of(svc, clock) == BUSY, "休眠群仅凭分数就回来了"


def test_configured_seconds_still_auto_wakes():
    """填了正数才有"到期自动醒"—— 这条旋钮仍然可用，只是默认不用。"""
    svc, clock = _service(icebreaker_dormant_seconds=600)
    _seed(svc, CALM, 6.0)
    _seed(svc, BUSY, 3.0)
    _break_ice_and_wait(svc, clock)
    assert svc._load_state(CALM).dormant_forever is False
    assert int(svc._load_state(CALM).dormant_until) == clock.now + 600
    assert _focus_of(svc, clock) == BUSY

    clock.now += 601
    assert svc.is_dormant(CALM) is False
    assert _focus_of(svc, clock) == CALM, "休眠到期后应重新参与竞争（分数更高者拿回焦点）"


def test_the_enable_switch_turns_the_sleep_off():
    """关掉总开关 = 回到旧行为（`seconds=0` 现在表示"一直休"，关不了功能）。"""
    svc, clock = _service(icebreaker_dormant_enabled=False)
    _seed(svc, CALM, 6.0)
    _break_ice_and_wait(svc, clock)
    assert svc.is_dormant(CALM) is False
    assert svc._load_state(CALM).feedback_tier == "silent", "关掉的是休眠，不是结算"


def test_enter_dormancy_respects_the_enable_switch():
    svc, _ = _service(icebreaker_dormant_enabled=False)
    assert svc.enter_dormancy(CALM, reason="no_reply") is False


# ── 二、@ 唤醒 ─────────────────────────────────────────────────────

def test_at_mention_wakes_a_sleeping_group():
    """**休眠的群可以用 @ 唤醒**：锁在焦点选择里优先，且必须先把休眠清掉。"""
    svc, clock = _service()
    _seed(svc, CALM, 9.0, focus=True)
    _seed(svc, BUSY, 5.0)
    _break_ice_and_wait(svc, clock)
    assert svc.is_dormant(CALM) is True and _focus_of(svc, clock) == BUSY

    svc.lock_group(CALM)                    # ← @ 走的就是这条（gate 第 2 步）
    assert svc.is_dormant(CALM) is False, "@ 没有唤醒休眠群"
    assert _focus_of(svc, clock) == CALM, "@ 之后焦点必须回到这个群"


def test_mark_focus_wakes_a_sleeping_group():
    """唤醒词 / 引用她也算点名 —— 同一类"直接叫她"，同办。"""
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _seed(svc, BUSY, 5.0)
    _break_ice_and_wait(svc, clock)

    svc.mark_focus(CALM)
    assert svc.is_dormant(CALM) is False


def test_wake_reason_is_recorded():
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _break_ice_and_wait(svc, clock)
    svc.wake_from_dormancy(CALM, reason="at")
    assert svc._load_state(CALM).last_focus_reason == "wake:at"


# ── 三、只有破冰那一轮算数 ─────────────────────────────────────────

def test_a_normal_reply_that_nobody_answers_does_not_sleep_the_group():
    """普通回复没人接**不**休眠 —— 那是常态，不是冷场。"""
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _reply(svc, CALM)
    clock.now += 91
    _tick(svc)

    assert svc._load_state(CALM).feedback_tier == "silent"
    assert svc.is_dormant(CALM) is False, "普通回复没人接也睡了 —— 会把好群一个个睡掉"


def test_replying_after_an_icebreaker_cancels_the_sleep():
    """破冰之后**有人接、她也答了** → 这一轮已经不是"没人接"，不该睡。"""
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    svc.note_proactive_speech(CALM)
    _message(svc, CALM, clock)          # 群友接话
    _reply(svc, CALM)                   # 她作答（回复周期重开）
    assert svc._load_state(CALM).proactive_pending is False

    clock.now += 91
    _tick(svc)
    assert svc.is_dormant(CALM) is False


def test_quiet_or_warm_icebreaker_does_not_sleep():
    """有人应（哪怕只有一条）就不睡：quiet 是"有人在应"，只是还看不出热度。"""
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    svc.note_proactive_speech(CALM)
    _message(svc, CALM, clock)
    clock.now += 91
    _tick(svc)

    assert svc._load_state(CALM).feedback_tier == "quiet"
    assert svc.is_dormant(CALM) is False


def test_warm_icebreaker_does_not_sleep():
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    svc.note_proactive_speech(CALM)
    for _ in range(3):
        _message(svc, CALM, clock)
    clock.now += 91
    _tick(svc)

    assert svc._load_state(CALM).feedback_tier == "warm"
    assert svc.is_dormant(CALM) is False


def test_the_flag_is_judged_only_once():
    """一次破冰只判一次：结算过之后旗子必须落，别在下一轮没人的时候翻旧账。"""
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _break_ice_and_wait(svc, clock)
    assert svc._load_state(CALM).proactive_pending is False

    svc.wake_from_dormancy(CALM)
    clock.now += 91                      # 又过了一个窗口，仍然没人说话
    _tick(svc)
    assert svc.is_dormant(CALM) is False, "旧旗子翻旧账：第二次结算又把群睡了"


# ── 四、与锁的关系 ─────────────────────────────────────────────────

def test_sleeping_releases_the_icebreaker_hold_lock():
    """休眠要顺手放掉破冰那把锁，否则"让位"还要再迟 30 秒才生效。

    锁在 `_choose_focus_state` 里优先于休眠过滤：120s 的按住没过期时，光把群标成
    休眠是不够的 —— 焦点仍被锁独占。
    """
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _seed(svc, BUSY, 1.0)
    svc.lock_group(CALM, seconds=120, reason="icebreaker")   # 破冰成功路径做的事
    svc.note_proactive_speech(CALM)
    clock.now += 91                                          # 还没到 120s
    _tick(svc)

    assert svc._load_state(CALM).lock_until == 0, "休眠时没放锁"
    assert _focus_of(svc, clock) == BUSY, "还锁着 → 其他群要等到 120s 才能竞态"


def test_a_dormant_group_can_still_be_locked_by_an_at():
    """@ 的锁不被休眠吃掉（顺序：先判锁，再筛休眠）。"""
    svc, clock = _service()
    _seed(svc, CALM, 2.0)
    _seed(svc, BUSY, 9.0)
    svc.enter_dormancy(CALM, reason="no_reply")
    svc.lock_group(CALM, seconds=90, reason="at")
    assert _focus_of(svc, clock) == CALM


# ── 五、边界 ───────────────────────────────────────────────────────

def test_all_groups_dormant_is_not_a_global_mute():
    """**全员休眠 ≠ 全体静音**：一个能选的都没有时，退回最高分群。

    否则 `_choose_focus_state` 返回 None，所有群的消息都会被判 `non_focus`——
    那不是"让位给别的群"，而是她从此不说话了。
    """
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _seed(svc, BUSY, 8.0)
    svc.enter_dormancy(CALM, reason="no_reply")
    svc.enter_dormancy(BUSY, reason="no_reply")

    assert _focus_of(svc, clock) == BUSY, "全都在睡时必须仍有一个焦点（最高分）"


def test_disabling_the_switch_wakes_everyone_immediately():
    """**关掉开关 = 让它们都回来**：休眠标记是落过盘的，判定必须当场让路。

    只在"新入睡"那一侧问开关的话，用户关掉之后没有任何变化 ——
    一个关不掉的开关比没有开关更糟。
    """
    svc, clock = _service()
    _seed(svc, CALM, 9.0, focus=True)
    _seed(svc, BUSY, 1.0)
    _break_ice_and_wait(svc, clock)
    assert _focus_of(svc, clock) == BUSY

    svc.plugin._qq_settings["icebreaker_dormant_enabled"] = False
    assert svc.is_dormant(CALM) is False, "关掉开关后它还在睡"
    clock.now += 1800
    assert _focus_of(svc, clock) == CALM, "关掉开关后休眠群仍被排除在竞争之外"


def test_disabling_the_switch_also_clears_the_flag_on_the_next_tick():
    """标记也要清干净：否则关掉再打开，旧标记会让群立刻重新睡下。"""
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _break_ice_and_wait(svc, clock)
    assert svc._load_state(CALM).dormant_forever is True

    svc.plugin._qq_settings["icebreaker_dormant_enabled"] = False
    _tick(svc)
    st = svc._load_state(CALM)
    assert st.dormant_forever is False and int(st.dormant_until) == 0

    svc.plugin._qq_settings["icebreaker_dormant_enabled"] = True
    assert svc.is_dormant(CALM) is False, "重新打开开关后旧标记又生效了"


def test_enter_dormancy_is_idempotent_and_reports_first_time_only():
    svc, _ = _service()
    assert svc.enter_dormancy(CALM, reason="no_reply") is True
    assert svc.enter_dormancy(CALM, reason="no_reply") is False


def test_enter_dormancy_ignores_an_empty_group_id():
    svc, _ = _service()
    assert svc.enter_dormancy("") is False


def test_only_at_can_wake_a_sleeping_group():
    """**唤醒口只有 @** —— 这条钉的是门控里的顺序（结构级断言，不是行为猜测）。

    gate 第 2 步「@ 必回」在焦点门控**之前**，所以休眠群里 @ 她走得到
    `lock_group`；而关键词唤醒（第 6 步）与"引用她"（第 7 步）都在**焦点群分支**
    里，休眠群不是焦点 → 走不到。写清楚这件事，是为了让"休眠的群可以用 @ 唤醒"
    这条口径有据可查，而不是让人以为引用她也能唤醒。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "attention_gate_service.py").read_text(
        encoding="utf-8"
    )
    at_step = src.index("if is_at_bot and not is_reply_to_bot:")
    focus_gate = src.index("if focus_group != normalized_group_id:")
    keyword_step = src.index('if category and category != "chat":')
    reply_step = src.index("if is_reply_to_bot:")

    assert at_step < focus_gate, "@ 必回必须排在焦点门控之前（否则休眠群 @ 不醒）"
    assert focus_gate < keyword_step, "关键词唤醒在焦点群分支里 —— 顺序变了就要重新看这条口径"
    assert focus_gate < reply_step, "「引用她」也在焦点群分支里"
    assert "attention.lock_group(normalized_group_id)" in src[at_step:focus_gate], (
        "@ 那条路必须调 lock_group —— 它才是唤醒休眠的那个入口"
    )


def test_dormancy_survives_a_restart():
    """重启不该让她忘记"这个群我破冰没人接"——存档往返必须带上休眠状态。"""
    svc, clock = _service()
    _seed(svc, CALM, 6.0)
    _break_ice_and_wait(svc, clock)

    raw = svc._load_state(CALM).to_dict()
    assert raw.get("dormant_forever") is True, "to_dict 丢了 dormant_forever"
    restored = type(svc._load_state(CALM)).from_dict(raw, group_id=CALM)
    assert restored.dormant_forever is True, "from_dict 丢了 dormant_forever"


def test_a_timed_dormancy_also_survives_a_restart():
    svc, clock = _service(icebreaker_dormant_seconds=600)
    _seed(svc, CALM, 6.0)
    _break_ice_and_wait(svc, clock)

    raw = svc._load_state(CALM).to_dict()
    restored = type(svc._load_state(CALM)).from_dict(raw, group_id=CALM)
    assert restored.dormant_until == raw["dormant_until"]
    assert restored.dormant_forever is False


def test_old_archive_does_not_wake_up_sleeping():
    """旧存档没有这两个键 → 0/False（没睡过）。**不能**回落到"从现在开始睡"。"""
    from plugin.plugins.qq_auto_reply.attention_service import QQGroupAttentionState

    st = QQGroupAttentionState.from_dict({"attention_score": 5.0}, group_id=CALM)
    assert st.dormant_until == 0
    assert st.dormant_forever is False
    assert st.proactive_pending is False


def test_schema_defaults_and_floor():
    """出厂值只有一处说法，且 `seconds=0` 就是使用者的默认口径「没 @ 一直休」。"""
    from plugin.plugins.qq_auto_reply import settings_schema

    spec = settings_schema.BY_KEY["icebreaker_dormant_seconds"]
    assert spec.default == 0, "0 = 一直休（只有 @ 能唤醒）"
    assert spec.floor == 0
    assert spec.saveable is True
    assert "icebreaker_dormant_seconds" in settings_schema.SAVEABLE_KEYS

    switch = settings_schema.BY_KEY["icebreaker_dormant_enabled"]
    assert switch.default is True, "默认启用休眠（这就是使用者要的行为）"
    assert switch.saveable is True

    svc, _ = _service()
    assert svc._dormant_seconds() == spec.default
    assert svc._dormant_enabled() is True
