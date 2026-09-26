"""注意力行为基线：用**固定的消息/回复时序**断言行为，而不是断言内部数值。

为什么需要这一份（草案 docs/attention-redesign-draft.md §7 步骤 0）：
  注意力子系统此前几乎没有行为覆盖 —— 已有测试多在验「某个阈值算得对」，而它
  真实的失败模式是**时序性的静默失效**（相位被打断、分数被抽干、焦点悄悄丢失）。
  这类问题只有「模拟一段真实对话、断言它没掉线」才抓得住。

本文件同时充当重构基线：步 1/2 改完这些断言必须仍然全绿。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply.attention_service import QQAttentionService

#: 使用者 business_config.json 里的真实参数
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
    "backlog_labels": [],
}


class _Clock:
    """可推进的冻结时钟 —— 时序断言必须能精确控制时间。"""

    def __init__(self, start: int = 100_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now


def _service(groups=("A", "B")):
    plugin = SimpleNamespace(
        group_permission_mgr=SimpleNamespace(
            list_groups=lambda: [{"group_id": g} for g in groups]
        ),
        _qq_settings=dict(LIVE),
        backlog_store=None,          # 落盘不是本文件被测对象
        permission_mgr=None,
        _emit_log=lambda *a, **k: None,
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    svc = QQAttentionService(plugin)
    clock = _Clock()
    svc._current_time = clock
    return svc, clock


def _seed(svc, gid, score, *, now, focus=True):
    st = svc._load_state(gid)
    st.attention_score = score
    st.last_decay_at = now
    st.phase_started_at = now
    if focus:
        st.focus_acquired_at = now
        st.last_focus_at = now
    svc._write_state(st)


def _message(svc, gid, clock):
    asyncio.run(svc.update_on_message({
        "group_id": gid, "user_id": "u1", "content": "继续聊",
        "timestamp": clock.now, "is_at_bot": False,
    }))


def _reply(svc, gid):
    asyncio.run(svc.update_on_reply(gid))


# ── 意图 1：热群必须能「一直聊很久」 ────────────────────────────────
#
# 使用者原话：「这个群有我喜欢的话题，我能一直聊很久」。
# 一段稳定活跃的对话里，猫娘不该中途掉出可回复状态。
#
# 场景：群每 30 秒有人说话，猫娘每 60 秒回一条，持续 4 分钟。

def _simulate_long_conversation(svc, clock, *, gid="A", start=4.5, minutes=4):
    """返回 [(经过秒数, 分数, 相位)] 轨迹。"""
    t0 = clock.now
    _seed(svc, gid, start, now=t0)
    traj: list[tuple[int, float, str]] = []
    for step in range(1, minutes * 2 + 1):          # 每步 30 秒
        clock.now = t0 + step * 30
        elapsed = clock.now - t0
        # 每 30 秒群里有新消息；每 60 秒猫娘回一条
        _message(svc, gid, clock)
        if elapsed % 60 == 0:
            _reply(svc, gid)
        st = svc._apply_decay(svc._load_state(gid), clock.now)
        svc._write_state(st)
        traj.append((elapsed, st.attention_score, st.phase))
    return traj


def test_hot_group_stays_replyable_through_a_long_conversation():
    """持续活跃 4 分钟期间，分数必须始终高于发送门控线。"""
    svc, clock = _service()
    traj = _simulate_long_conversation(svc, clock)
    floor = svc._focus_send_threshold()
    low = [(t, s, p) for t, s, p in traj if s < floor]
    detail = "\n".join(f"    +{t:3d}s  score={s:5.2f}  phase={p}" for t, s, p in traj)
    assert not low, (
        f"热群在持续对话中掉出可回复状态（发送线 {floor}）：\n{detail}\n"
        f"  掉线点：{low}"
    )


# ── 意图 2：回复的代价不该随当前分数放大 ────────────────────────────
#
# 现在是乘性消耗（score *= 1 - ratio）：分数越高扣得越多。
# 使用者反复撞上「越热越容易掉」。用户要的是「能一直聊」，热群不该罚得更重。

def test_reply_cost_is_not_proportional_to_current_score():
    """同样比例下，高分群与低分群被扣掉的**绝对量**应当相同（减性语义）。"""
    svc, _ = _service()
    _seed(svc, "A", 4.0, now=svc._current_time())
    _seed(svc, "B", 8.0, now=svc._current_time())

    before_a = svc._load_state("A").attention_score
    _reply(svc, "A")
    cost_a = before_a - svc._load_state("A").attention_score

    before_b = svc._load_state("B").attention_score
    _reply(svc, "B")
    cost_b = before_b - svc._load_state("B").attention_score

    assert cost_a == pytest.approx(cost_b, rel=1e-3), (
        f"回复代价随分数放大：4.0 扣 {cost_a:.2f}、8.0 扣 {cost_b:.2f}。"
        f"使用者要的是一直聊，热群不该被罚得更重。"
    )


# ── 意图 3：回血不能被相位压制 ──────────────────────────────────────
#
# fall 相位把消息加成乘 0.3，于是「跌下去就爬不回来」：
#   30 秒内 衰减 0.45，群友发 1 条只补 0.045 —— 净增速恒为负。

def test_message_boost_is_not_attenuated_in_fall_phase():
    """`fall` 相位里一条消息的加成不得被压到几乎无效。"""
    svc, clock = _service()
    now = clock.now

    _seed(svc, "A", 5.0, now=now)              # rise
    _message(svc, "A", clock)
    rise_boost = svc._load_state("A").attention_score - 5.0

    _seed(svc, "B", 5.0, now=now)
    st = svc._load_state("B")
    st.phase = "fall"
    st.phase_started_at = now
    svc._write_state(st)
    _message(svc, "B", clock)
    fall_boost = svc._load_state("B").attention_score - 5.0

    assert fall_boost == pytest.approx(rise_boost, rel=0.05), (
        f"fall 相位的消息加成被衰减：rise={rise_boost:.3f} fall={fall_boost:.3f}。"
        f"这条衰减让回落期净增速恒为负，群无法回血。"
    )


# ── 意图 4：分数必须有余量支撑「一直聊」 ────────────────────────────
#
# 现在自然增长被 min(focus_threshold, ...) 封顶在焦点线上：夺冠即「刚好够线、
# 零余量」，于是每次回复消耗都会把群打到线下。要能一直聊，线上必须有空间。

def test_score_can_exceed_focus_threshold():
    """夺冠之后自然增长不得被焦点线封顶。"""
    svc, clock = _service()
    now = clock.now
    _seed(svc, "A", 4.0, now=now)

    # 甜蜜期里持续推进（期间没有回复消耗），分数应能涨过焦点线
    for step in range(1, 7):                    # 6 × 10s
        clock.now = now + step * 10
        st = svc._apply_decay(svc._load_state("A"), clock.now)
        svc._write_state(st)

    score = svc._load_state("A").attention_score
    assert score > svc._focus_threshold(), (
        f"分数被焦点线封顶（score={score:.2f}, 焦点线={svc._focus_threshold()}）。"
        f"夺冠后没有余量，任何一次回复消耗都会把群打到线下。"
    )


# ── 意图 5：锁（@猫娘 / 唤醒词）─────────────────────────────────────
#
# 设计（草案 §2/§3）：无锁时归属 = 分数最高的群、**随时可变**（这就是「看一眼
# 新群、没兴趣就回旧群」）；有锁时该群独占，其余群不参与竞争。
# 两者是**独立信号** —— 分数是「我多想聊」，锁是「有人点名叫我」。

def _focus_of(svc, clock):
    """当前归属（走真实的焦点选择路径）。"""
    states = [svc._load_state(g) for g in ("A", "B")]
    for s in states:
        svc._write_state(s)
    chosen = svc._choose_focus_state(states, clock.now)
    return chosen.group_id if chosen else ""


def test_lock_wins_over_a_higher_scoring_group():
    """被锁的群即使分数明显低于别人，也必须保持归属。"""
    svc, clock = _service()
    now = clock.now
    _seed(svc, "A", 3.0, now=now)      # 被锁，但分数低
    _seed(svc, "B", 9.0, now=now)      # 分数高得多，想抢
    svc.lock_group("A")

    assert _focus_of(svc, clock) == "A", (
        "锁未生效：高分群抢走了被 @ 的群 —— 有人点名叫我时我必须回头应对"
    )


def test_lock_expires_and_score_arbitration_resumes():
    """锁到期后回到纯分数仲裁。"""
    svc, clock = _service()
    now = clock.now
    _seed(svc, "A", 3.0, now=now)
    _seed(svc, "B", 9.0, now=now)
    svc.lock_group("A")
    assert _focus_of(svc, clock) == "A"

    clock.now = now + svc._lock_seconds() + 1     # 锁过期
    assert _focus_of(svc, clock) == "B", "锁过期后应交还给分数最高的群"


def test_relocking_resets_the_timer():
    """再被 @ 一次应重新锁满（与人对重复召唤的直觉一致）。"""
    svc, clock = _service()
    now = clock.now
    _seed(svc, "A", 3.0, now=now)
    _seed(svc, "B", 9.0, now=now)

    svc.lock_group("A")
    clock.now = now + svc._lock_seconds() - 5     # 快到期了
    svc.lock_group("A")                            # 又被叫一次

    clock.now = now + svc._lock_seconds() + 1     # 按第一次算已过期
    assert _focus_of(svc, clock) == "A", (
        "重复 @ 未重置计时：按第一次的时间算已经过期了"
    )


def test_lock_seconds_zero_disables_locking():
    """`attention_lock_seconds=0` 时锁不生效 —— 回到纯分数仲裁。"""
    svc, clock = _service()
    svc.plugin._qq_settings["attention_lock_seconds"] = 0
    now = clock.now
    _seed(svc, "A", 3.0, now=now)
    _seed(svc, "B", 9.0, now=now)
    svc.lock_group("A")
    assert _focus_of(svc, clock) == "B", "锁时长 0 时不应锁住"


def test_lock_is_persisted():
    """锁必须能存下来/读回来（重启后 lock_until 不能丢）。"""
    svc, clock = _service()
    _seed(svc, "A", 3.0, now=clock.now)
    svc.lock_group("A")
    raw = svc._load_state("A").to_dict()
    assert int(raw.get("lock_until") or 0) > 0, "to_dict 丢了 lock_until"
    restored = type(svc._load_state("A")).from_dict(raw, group_id="A")
    assert restored.lock_until == raw["lock_until"], "from_dict 丢了 lock_until"
