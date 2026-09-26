# -*- coding: utf-8 -*-
"""`reply_necessity` 单测：把打分曲线、短反应、噪声剥离、状态窗口与退避全部钉住。

这些数字不是拍脑袋：打分项来自 MaiBot `src/maisaka/reply_necessity.py` 的常量表
（见模块 docstring 的出处），阈值是本插件在「焦点群内部」这一层的选择（见模块常数注释）。
测试里**用公式推期望值**而不是抄一遍魔法数，这样常量改了测试仍能表达意图。
"""

from __future__ import annotations

import math

import pytest
from plugin.plugins.qq_auto_reply.reply_necessity import (
    AT_SCORE,
    DEFAULT_TRIGGER_SCORE,
    FOCUS_SCORE,
    MENTION_SCORE,
    PRESSURE_STANDARD_SCORE,
    QUESTION_SCORE,
    SELF_PENALTY_MAX,
    SHORT_REACTION_PENALTY,
    GroupSpeechTracker,
    IdleBackoff,
    NecessitySignals,
    is_short_reaction_batch,
    score_necessity,
    strip_noise,
)

#: 单测统一用这个阈值（显式传入），使断言表达「分数与阈值的关系」而不绑定默认值。
TEST_THRESHOLD = 60.0


def _sig(**kw) -> NecessitySignals:
    base = dict(message_text="在吗", is_group=True, focus_active=True, pending_count=3, pending_threshold=3)
    base.update(kw)
    return NecessitySignals(**base)


# ── 打分组件 ────────────────────────────────────────────────────────────────

def test_at_bot_always_triggers():
    verdict = score_necessity(_sig(is_at_bot=True, self_ratio=1.0), threshold=TEST_THRESHOLD)
    assert verdict.decision == "trigger"
    assert verdict.breakdown.relevance == AT_SCORE


def test_reply_to_bot_counts_as_mention():
    verdict = score_necessity(_sig(is_reply_to_bot=True), threshold=TEST_THRESHOLD)
    assert verdict.breakdown.relevance == MENTION_SCORE
    assert verdict.decision == "trigger"


def test_plain_short_reaction_is_a_wait():
    """安静时刻的纯短反应（"哈哈"）：focus 40 + 短反应 −25 + 轻微积压 6 = 21 < 30 → 不接。"""
    verdict = score_necessity(_sig(message_text="哈哈哈", pending_count=1, pending_threshold=3), threshold=TEST_THRESHOLD)
    assert verdict.breakdown.content == SHORT_REACTION_PENALTY
    assert verdict.decision == "wait"


def test_question_in_busy_focus_group_triggers():
    """焦点群里有人提问且积压到位 → 接。分数用公式推：40 + 15 + 50。"""
    verdict = score_necessity(_sig(message_text="这个怎么弄？", pending_count=3, pending_threshold=3), threshold=TEST_THRESHOLD)
    expected = FOCUS_SCORE + QUESTION_SCORE + int(PRESSURE_STANDARD_SCORE)
    assert verdict.score == expected
    assert verdict.decision == "trigger"


def test_presence_penalty_quietens_a_talkative_bot():
    """存在感占比 0.60 → 扣满 25 分；在**低压力**场景里足以把 trigger 翻成 wait。

    高压力场景（积压满 + 提问）单靠存在感压不下去 —— 这是有意的：别人明确在问，
    即使她刚才话多也应该接。测试同时钉住这两面。
    """
    # 积压刚好让 calm 过线（40 + 22 = 62 ≥ 60），翻转只能由存在感惩罚造成
    calm = _sig(message_text="今天天气不错", pending_count=2, pending_threshold=3, self_ratio=0.0)
    busy = _sig(message_text="今天天气不错", pending_count=2, pending_threshold=3, self_ratio=0.6)
    assert score_necessity(busy, threshold=TEST_THRESHOLD).breakdown.presence == -int(SELF_PENALTY_MAX)
    assert score_necessity(busy, threshold=TEST_THRESHOLD).breakdown.score < score_necessity(calm, threshold=TEST_THRESHOLD).breakdown.score
    assert score_necessity(calm, threshold=TEST_THRESHOLD).decision == "trigger"
    assert score_necessity(busy, threshold=TEST_THRESHOLD).decision == "wait"

    # 反面：被明确提问 + 积压满时，存在感不该压掉这一次回复
    asked = _sig(message_text="这个怎么弄？", pending_count=3, pending_threshold=3, self_ratio=0.6)
    assert score_necessity(asked, threshold=TEST_THRESHOLD).decision == "trigger"


def test_presence_penalty_is_free_below_the_ratio_floor():
    assert score_necessity(_sig(self_ratio=0.25), threshold=TEST_THRESHOLD).breakdown.presence == 0


@pytest.mark.parametrize("pending,expected_ratio", [(1, 1 / 3), (3, 1.0)])
def test_pressure_grows_quadratically_within_threshold(pending, expected_ratio):
    verdict = score_necessity(_sig(pending_count=pending, pending_threshold=3), threshold=TEST_THRESHOLD)
    expected = min(int(round(PRESSURE_STANDARD_SCORE * expected_ratio * expected_ratio)), int(PRESSURE_STANDARD_SCORE))
    assert verdict.breakdown.pressure == expected


def test_pressure_uses_log_curve_beyond_threshold_and_is_capped():
    verdict = score_necessity(_sig(pending_count=12, pending_threshold=3), threshold=TEST_THRESHOLD)  # ratio = 4
    expected = int(round(PRESSURE_STANDARD_SCORE + 50.0 * math.log1p(3.0) / math.log1p(4.0)))
    assert verdict.breakdown.pressure == expected
    capped = score_necessity(_sig(pending_count=999, pending_threshold=3), threshold=TEST_THRESHOLD)
    assert capped.breakdown.pressure == 100


def test_no_pending_means_no_pressure():
    assert score_necessity(_sig(pending_count=0), threshold=TEST_THRESHOLD).breakdown.pressure == 0


def test_threshold_zero_disables_the_gate():
    """threshold=0 ⇒ 恒 trigger（回到「交给 LLM 自判」的老行为），便于一键关闭。"""
    verdict = score_necessity(_sig(message_text="哈哈"), threshold=0)
    assert verdict.decision == "trigger"


def test_frequency_factor_attenuates_the_score():
    """频率因子 0.5~1.0：低频群整体压分。"""
    loud = score_necessity(_sig(pending_count=3, pending_threshold=3), threshold=TEST_THRESHOLD, frequency=1.0)
    quiet = score_necessity(_sig(pending_count=3, pending_threshold=3), threshold=TEST_THRESHOLD, frequency=0.0)
    assert quiet.breakdown.score < loud.breakdown.score
    assert quiet.breakdown.frequency_factor == 0.5


def test_reason_is_diagnosable():
    verdict = score_necessity(_sig(message_text="帮我看看", pending_count=3, pending_threshold=3), threshold=TEST_THRESHOLD)
    assert verdict.reason.startswith("necessity_")
    assert "focus" in verdict.reason
    assert "@" not in verdict.reason.replace("necessity_", "")


# ── 文本处理 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("哈哈", True),
        ("哈哈哈好", True),
        ("666", True),
        ("？", True),
        ("哈哈哈哈哈哈哈", False),   # 超 8 字符
        ("今天天气不错", False),
        ("", False),
    ],
)
def test_short_reaction_detection(text, expected):
    assert is_short_reaction_batch(text) is expected


def test_strip_noise_removes_quote_and_at_markers():
    raw = "[CQ:reply,id=1]@<1234> [回复了张三的消息: 你好] 那你去看看"
    cleaned = strip_noise(raw)
    assert "CQ:reply" not in cleaned
    assert "回复了" not in cleaned
    assert "那你去看看" in cleaned
    assert "<" not in cleaned


def test_request_score_is_dropped_when_addressed_to_another_assistant():
    """开头点名别的助手 → 请求/征询加分作废（这话不是冲我们来的）。"""
    ours = score_necessity(_sig(message_text="帮我看看这个"), threshold=TEST_THRESHOLD)
    theirs = score_necessity(_sig(message_text="DeepSeek，帮我看看这个"), threshold=TEST_THRESHOLD)
    assert theirs.breakdown.content < ours.breakdown.content


# ── 状态窗口 ────────────────────────────────────────────────────────────────

def test_tracker_counts_pending_since_her_last_reply():
    tracker = GroupSpeechTracker()
    tracker.record("G", now=100, speaker="A")
    tracker.record("G", now=110, speaker="B")
    tracker.record_self("G", now=120)
    tracker.record("G", now=130, speaker="C")
    tracker.record("G", now=140, speaker="D")
    assert tracker.pending_count("G", now=150) == 2


def test_tracker_self_ratio_and_window_pruning():
    tracker = GroupSpeechTracker(window_seconds=100)
    tracker.record_self("G", now=10)
    tracker.record("G", now=20, speaker="A")
    assert tracker.self_ratio("G", now=30) == 0.5
    # 窗口滑过之后旧事件被剪掉：只剩自己那条也过期 → 比例为 0
    assert tracker.self_ratio("G", now=200) == 0.0
    assert tracker.pending_count("G", now=200) == 0


def test_tracker_ignores_empty_group_id():
    tracker = GroupSpeechTracker()
    tracker.record("", now=1, speaker="A")
    assert tracker.pending_count("", now=2) == 0


# ── 空闲退避 ────────────────────────────────────────────────────────────────

def test_backoff_starts_after_start_count_and_grows_exponentially():
    backoff = IdleBackoff()
    assert backoff.record_wait("G", now=0) == 0.0          # 第 1 次：不设退避
    assert backoff.record_wait("G", now=1) == 15.0         # 第 2 次（START_COUNT）：15s
    assert backoff.record_wait("G", now=2) == 30.0
    assert backoff.record_wait("G", now=3) == 60.0


def test_backoff_is_capped():
    backoff = IdleBackoff()
    for i in range(20):
        last = backoff.record_wait("G", now=i)
    assert last == IdleBackoff.CAP_SECONDS


def test_backoff_delay_decays_and_can_be_bypassed_by_pending():
    backoff = IdleBackoff()
    backoff.record_wait("G", now=0)
    backoff.record_wait("G", now=0)                        # until = 15
    assert backoff.delay_seconds("G", now=0, pending_count=1) == 15.0
    assert backoff.delay_seconds("G", now=10, pending_count=1) == 5.0
    # 积压到绕过阈值 → 立刻评估
    assert backoff.delay_seconds("G", now=0, pending_count=IdleBackoff.BYPASS_PENDING) == 0.0


def test_backoff_reset_clears_state():
    backoff = IdleBackoff()
    backoff.record_wait("G", now=0)
    backoff.record_wait("G", now=0)
    backoff.reset("G")
    assert backoff.delay_seconds("G", now=0, pending_count=1) == 0.0
    assert backoff.record_wait("G", now=1) == 0.0          # 计数也归零


# ── 默认阈值（行为旋钮）──────────────────────────────────────────────────────

def test_default_threshold_is_pinned():
    """默认阈值是**行为旋钮**，改动必须是有意的：它决定「她多沉默」。

    取 60 的理由见模块里 DEFAULT_TRIGGER_SCORE 的注释（关键约束：必须 > focus 的 40，
    否则焦点群里的普通消息光靠 focus 分就通过，这一关等于不存在）。
    """
    assert DEFAULT_TRIGGER_SCORE == 60.0


def test_default_curve_quiet_group_vs_busy_group():
    """用默认阈值刻画真实曲线：安静闲聊不接，忙起来/被问到才接。"""
    quiet_plain = _sig(message_text="今天天气不错", pending_count=1, pending_threshold=10)
    busy_plain = _sig(message_text="今天天气不错", pending_count=10, pending_threshold=10)
    asked_quiet = _sig(message_text="这个怎么弄？", pending_count=1, pending_threshold=10)
    asked_busy = _sig(message_text="这个怎么弄？", pending_count=3, pending_threshold=3)

    assert score_necessity(quiet_plain).decision == "wait"      # 安静时的闲聊：不接
    assert score_necessity(busy_plain).decision == "trigger"    # 群里攒了 10 条：接
    assert score_necessity(asked_quiet).decision == "wait"      # 只问了一句、没积压：仍不接
    assert score_necessity(asked_busy).decision == "trigger"    # 有人问 + 积压到位：接
    assert score_necessity(_sig(is_at_bot=True)).decision == "trigger"
