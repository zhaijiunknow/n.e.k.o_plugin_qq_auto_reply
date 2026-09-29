"""按群维护循环：谁静得够久谁破冰、谁的群记忆有增量谁推摘要。

2026-09-29 跨群取舍删除前，这两件事都挂在**焦点切换**上（切换时给旧焦点群推摘要、
给新焦点群做回溯补回，冷场计数也数的是"焦点切换了几次"）。删掉跨群取舍后没有切换
事件可挂，判据全部改成**每个群自己的状态**（见 `_maintenance_loop`）。

这些用例钉住每一条判据，尤其是"跳过"的那些条件 —— 它们决定她会不会在死群里反复开口、
或者每 tick 都去问一次 LLM。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.attention_gate_service import QQAttentionGateService

GROUP_A = "1048307485"
GROUP_B = "985066274"
NOW = 1_000_000


class _FakeAttention:
    """最小注意力桩：给出群清单与每群状态（状态字段由用例自己摆）。"""

    def __init__(self, states: dict[str, SimpleNamespace] | None = None, now: int = NOW):
        self.states = states if states is not None else {}
        self._now = now
        self.persisted = 0

    def _enabled(self) -> bool:
        return True

    def _current_time(self) -> int:
        return self._now

    def _normalized_groups(self) -> list[str]:
        return list(self.states)

    def get_state(self, group_id: str) -> SimpleNamespace:
        return self.states[str(group_id)]

    def participates_in_attention(self, group_id: str) -> bool:
        return True


def _state(**kwargs) -> SimpleNamespace:
    base = dict(
        group_id="", attention_score=1.0, last_message_at=0,
        dormant_until=0, dormant_forever=False, lock_until=0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class _Gate(QQAttentionGateService):
    """把破冰与摘要替换成可观察的记账，专注测"判据"。"""

    def __init__(self, plugin):
        super().__init__(plugin)
        self.icebreakers: list[str] = []
        self.digests: list[str] = []
        self.icebreaker_result = True

    async def _try_icebreaker(self, group_id: str) -> bool:
        self.icebreakers.append(group_id)
        return self.icebreaker_result

    async def _push_group_digest(self, group_id: str) -> None:
        self.digests.append(group_id)


def _gate(states: dict[str, SimpleNamespace], settings: dict | None = None) -> _Gate:
    plugin = SimpleNamespace(
        attention_service=_FakeAttention(states),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        _emit_log=lambda *a, **k: None,
        _qq_settings=dict(settings or {}),
    )
    return _Gate(plugin)


# ── 破冰：判据是"这个群自己静了多久" ──────────────────────────────

def test_idle_group_gets_an_icebreaker():
    """静默超过阈值 → 主动开口一次。"""
    gate = _gate({GROUP_A: _state(last_message_at=NOW - 3600)})

    assert asyncio.run(gate._maybe_break_ice(GROUP_A, NOW)) is True
    assert gate.icebreakers == [GROUP_A]


def test_a_busy_group_is_left_alone():
    """刚有人说话的群不该被"破冰"打断。"""
    gate = _gate({GROUP_A: _state(last_message_at=NOW - 30)})

    assert asyncio.run(gate._maybe_break_ice(GROUP_A, NOW)) is False
    assert gate.icebreakers == []


def test_other_groups_silence_does_not_count():
    """别的群静了多久与这个群无关（跨群取舍的回归形态）。"""
    gate = _gate({
        GROUP_A: _state(last_message_at=NOW - 30),        # 这个群刚有人说话
        GROUP_B: _state(last_message_at=NOW - 7200),      # 另一个群静了两小时
    })

    asyncio.run(gate._maybe_break_ice(GROUP_A, NOW))

    assert gate.icebreakers == [], "拿别的群的静默当理由，去打扰一个正在聊的群"


def test_dormant_group_is_not_woken_by_an_icebreaker():
    """休眠 = "我主动开口没人接，先安静" —— 破冰循环不许把它又叫起来。"""
    gate = _gate({
        GROUP_A: _state(last_message_at=NOW - 7200, dormant_forever=True),
        GROUP_B: _state(last_message_at=NOW - 7200, dormant_until=NOW + 600),
    })

    asyncio.run(gate._maybe_break_ice(GROUP_A, NOW))
    asyncio.run(gate._maybe_break_ice(GROUP_B, NOW))

    assert gate.icebreakers == []


def test_locked_group_is_not_interrupted():
    """有人点名叫她、锁还没到期 → 她该回应人，不该另起话题。"""
    gate = _gate({GROUP_A: _state(last_message_at=NOW - 7200, lock_until=NOW + 60)})

    assert asyncio.run(gate._maybe_break_ice(GROUP_A, NOW)) is False
    assert gate.icebreakers == []


def test_a_group_that_never_spoke_is_skipped():
    """从没说过话的群（没有时间戳）不破冰 —— 那是"没见过"，不是"冷场"。"""
    gate = _gate({GROUP_A: _state(last_message_at=0)})

    assert asyncio.run(gate._maybe_break_ice(GROUP_A, NOW)) is False
    assert gate.icebreakers == []


def test_icebreaker_has_a_cooldown():
    """连试两次之间要隔够久，否则每 tick 都问一次 LLM，"破冰"变成刷屏。"""
    gate = _gate({GROUP_A: _state(last_message_at=NOW - 7200)})

    assert asyncio.run(gate._maybe_break_ice(GROUP_A, NOW)) is True
    assert asyncio.run(gate._maybe_break_ice(GROUP_A, NOW + 60)) is False, "冷却期内又破了一次"
    assert asyncio.run(gate._maybe_break_ice(GROUP_A, NOW + 3600)) is True, "冷却过后应该还能再试"
    assert gate.icebreakers == [GROUP_A, GROUP_A]


def test_icebreaker_can_be_disabled_with_zero():
    """`icebreaker_idle_seconds=0` = 关掉主动破冰（0 是合法值，不能被 `or` 吞掉）。"""
    gate = _gate({GROUP_A: _state(last_message_at=NOW - 100000)}, {"icebreaker_idle_seconds": 0})

    assert gate._icebreaker_idle_seconds() == 0
    assert asyncio.run(gate._maybe_break_ice(GROUP_A, NOW)) is False


def test_icebreaker_idle_seconds_defaults_and_clamps():
    """默认 1800 秒；垃圾值退回默认；负数钳到 0。"""
    default_gate = _gate({GROUP_A: _state()})
    assert default_gate._icebreaker_idle_seconds() == 1800

    garbage = _gate({GROUP_A: _state()}, {"icebreaker_idle_seconds": "abc"})
    assert garbage._icebreaker_idle_seconds() == 1800

    negative = _gate({GROUP_A: _state()}, {"icebreaker_idle_seconds": -5})
    assert negative._icebreaker_idle_seconds() == 0


# ── 群记忆摘要：按每群自己的节奏推 ────────────────────────────────

def test_digest_is_pushed_on_its_own_interval():
    """两次推送之间要隔够 `group_memory_digest_interval_seconds`。"""
    gate = _gate(
        {GROUP_A: _state()},
        {"group_memory_enabled": True, "group_memory_digest_interval_seconds": 300},
    )

    assert asyncio.run(gate._maybe_push_digest(GROUP_A, NOW)) is True
    assert asyncio.run(gate._maybe_push_digest(GROUP_A, NOW + 60)) is False
    assert asyncio.run(gate._maybe_push_digest(GROUP_A, NOW + 301)) is True
    assert gate.digests == [GROUP_A, GROUP_A]


def test_digest_interval_is_per_group():
    """一个群推过不影响另一个群（每群自己的节奏）。"""
    gate = _gate(
        {GROUP_A: _state(), GROUP_B: _state()},
        {"group_memory_enabled": True, "group_memory_digest_interval_seconds": 300},
    )

    asyncio.run(gate._maybe_push_digest(GROUP_A, NOW))
    asyncio.run(gate._maybe_push_digest(GROUP_B, NOW))

    assert gate.digests == [GROUP_A, GROUP_B]


def test_digest_is_skipped_when_group_memory_is_off():
    """群记忆是显式 opt-in —— 关着就一条都不许推。"""
    gate = _gate({GROUP_A: _state()}, {"group_memory_enabled": False})

    assert asyncio.run(gate._maybe_push_digest(GROUP_A, NOW)) is False
    assert gate.digests == []


def test_digest_can_be_disabled_with_zero_interval():
    gate = _gate(
        {GROUP_A: _state()},
        {"group_memory_enabled": True, "group_memory_digest_interval_seconds": 0},
    )

    assert asyncio.run(gate._maybe_push_digest(GROUP_A, NOW)) is False


# ── 一整轮维护：所有群都过一遍 ────────────────────────────────────

def test_maintenance_tick_covers_every_group():
    """一轮维护要把每个群各自判一遍（破冰只看参与竞争的群，摘要有开关管）。"""
    states = {
        GROUP_A: _state(last_message_at=NOW - 7200),
        GROUP_B: _state(last_message_at=NOW - 30),
    }
    gate = _gate(states, {"group_memory_enabled": True})

    asyncio.run(gate.run_maintenance_tick())

    assert gate.icebreakers == [GROUP_A], "只有静得够久的那个群该破冰"
    assert sorted(gate.digests) == sorted([GROUP_A, GROUP_B]), "摘要该覆盖所有群"


def test_maintenance_tick_breaks_ice_in_at_most_one_group():
    """一轮最多破冰一次 —— 她是一个人，不该同一秒往五个冷群各丢一句。

    这条同时挡住"插件重载后所有冷群一起被破冰"的启动爆发（重载时每个群的
    `last_message_at` 都还是旧的，判据会同时成立）。
    """
    states = {
        GROUP_A: _state(last_message_at=NOW - 7200),
        GROUP_B: _state(last_message_at=NOW - 7200),
    }
    gate = _gate(states)

    asyncio.run(gate.run_maintenance_tick())
    assert gate.icebreakers == [GROUP_A]

    asyncio.run(gate.run_maintenance_tick())   # 下一轮轮到另一个群
    assert gate.icebreakers == [GROUP_A, GROUP_B]


def test_a_failed_icebreaker_still_uses_the_turns_slot():
    """**投递失败/模型不开口也算用掉名额**。

    真机 2026-09-29 16:42 的教训：那一轮 QQ 连接断着，破冰投递失败 → `_try_icebreaker`
    返回 False → 闸门以为这一轮还没破过冰，于是**同一个 tick 里接着破了下一个群**
    （16:42:23 群 1048307485、16:42:30 群 985066274，相隔 7 秒）。
    """
    states = {
        GROUP_A: _state(last_message_at=NOW - 7200),
        GROUP_B: _state(last_message_at=NOW - 7200),
    }
    gate = _gate(states)
    gate.icebreaker_result = False          # 她没能说出去（断连 / 模型决定不开口）

    asyncio.run(gate.run_maintenance_tick())

    assert gate.icebreakers == [GROUP_A], "失败的破冰把名额还回去了，同一轮又破了下一个群"


def test_maintenance_tick_survives_a_broken_group():
    """某个群的状态坏掉不许让整轮停摆（否则后面的群永远轮不到）。"""
    class _Exploding(_FakeAttention):
        def get_state(self, group_id: str):
            if group_id == GROUP_A:
                raise RuntimeError("boom")
            return self.states[group_id]

    plugin = SimpleNamespace(
        attention_service=_Exploding({GROUP_B: _state(last_message_at=NOW - 7200)}),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        _emit_log=lambda *a, **k: None,
        _qq_settings={},
    )
    gate = _Gate(plugin)

    asyncio.run(gate.run_maintenance_tick())

    assert gate.icebreakers == [GROUP_B], "一个坏群把后面的群全挡住了"


def test_maintenance_loop_keeps_running_after_a_failing_round():
    """单轮炸了循环必须活着 —— 死掉的循环只表现为"她再也不主动说话了"。"""
    gate = _gate({GROUP_A: _state(last_message_at=NOW - 7200)})
    calls: list[int] = []

    async def failing_tick() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("第一轮炸了")

    gate.run_maintenance_tick = failing_tick  # type: ignore[method-assign]

    async def run_three_rounds() -> None:
        task = asyncio.create_task(gate._maintenance_loop(0.01))
        for _ in range(60):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_three_rounds())

    assert len(calls) >= 3, "第一轮的异常把维护循环杀死了"


def test_stop_proactive_loop_is_idempotent():
    """没启动过 / 停两次都不许炸（插件停止时会调）。"""
    gate = _gate({GROUP_A: _state()})

    asyncio.run(gate.stop_proactive_loop())
    asyncio.run(gate.start_proactive_loop())
    asyncio.run(gate.stop_proactive_loop())
    asyncio.run(gate.stop_proactive_loop())


def test_starting_the_loop_leaves_a_log_line():
    """启动时留一行凭据。

    这条循环是纯后台的（她主动开口、推群记忆），"她为什么一直不说话"在没有这行日志时
    完全无法区分"循环没起来"和"群里就是不冷场"。
    """
    lines: list[str] = []
    plugin = SimpleNamespace(
        attention_service=_FakeAttention({GROUP_A: _state()}),
        logger=SimpleNamespace(info=lambda msg, *a, **k: lines.append(str(msg)),
                               warning=lambda *a, **k: None),
        _emit_log=lambda *a, **k: None,
        _qq_settings={},
    )
    gate = _Gate(plugin)

    async def run() -> None:
        await gate.start_proactive_loop()
        await gate.stop_proactive_loop()

    asyncio.run(run())

    assert any("按群维护循环已启动" in line for line in lines), lines


def test_the_loop_starts_once_per_process():
    """宿主把插件初始化跑不止一遍 —— 第二条循环必须被挡住。

    真机依据：2026-09-29 16:32:08 的插件日志里「按群维护循环已启动」出现了**两次**，
    也就是两条循环同时在跑：破冰与群记忆摘要各做两遍。她是一个人，不是两支队伍。
    """
    from plugin.plugins.qq_auto_reply import attention_gate_service as gate_module

    def _make(lines: list[str]) -> _Gate:
        plugin = SimpleNamespace(
            attention_service=_FakeAttention({GROUP_A: _state()}),
            logger=SimpleNamespace(info=lambda msg, *a, **k: lines.append(str(msg)),
                                   warning=lambda *a, **k: None),
            _emit_log=lambda *a, **k: None,
            _qq_settings={},
        )
        return _Gate(plugin)

    first_lines: list[str] = []
    second_lines: list[str] = []
    first = _make(first_lines)
    second = _make(second_lines)

    async def run() -> None:
        try:
            await first.start_proactive_loop()
            await second.start_proactive_loop()      # 第二个实例（宿主第二次初始化）
            assert second._maintenance_task is None, "第二个实例也起了一条循环"
        finally:
            await second.stop_proactive_loop()
            await first.stop_proactive_loop()

    asyncio.run(run())

    assert sum("按群维护循环已启动" in line for line in first_lines) == 1
    assert not any("按群维护循环已启动" in line for line in second_lines), second_lines
    assert gate_module._maintenance_owner() is None, "停掉之后归属者要清掉，否则下次重载起不来"


def test_the_single_flight_marker_survives_a_module_reimport():
    """单飞标记挂在 `sys` 上，不是本模块的全局。

    插件重载会把本模块**重新导入**（新模块对象 = 新的模块全局），标记若放在模块全局里
    就会跟着重置 —— 而"两条循环同时在跑"正是重载/多次初始化时才出现的问题。
    """
    import sys

    from plugin.plugins.qq_auto_reply import attention_gate_service as gate_module

    plugin = SimpleNamespace(
        attention_service=_FakeAttention({GROUP_A: _state()}),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        _emit_log=lambda *a, **k: None,
        _qq_settings={},
    )
    gate = _Gate(plugin)

    async def run() -> None:
        try:
            await gate.start_proactive_loop()
            assert getattr(sys, gate_module._MAINTENANCE_OWNER_ATTR) is gate, (
                "标记不在 sys 上 —— 重载后再导入一次本模块，闸门就没了"
            )
        finally:
            await gate.stop_proactive_loop()

    asyncio.run(run())


def test_a_stopped_loop_can_be_started_again():
    """重载/重连都要能再起一条（归属者清理干净了）。"""
    lines: list[str] = []
    plugin = SimpleNamespace(
        attention_service=_FakeAttention({GROUP_A: _state()}),
        logger=SimpleNamespace(info=lambda msg, *a, **k: lines.append(str(msg)),
                               warning=lambda *a, **k: None),
        _emit_log=lambda *a, **k: None,
        _qq_settings={},
    )
    gate = _Gate(plugin)

    async def run() -> None:
        await gate.start_proactive_loop()
        await gate.stop_proactive_loop()
        await gate.start_proactive_loop()
        await gate.stop_proactive_loop()

    asyncio.run(run())

    assert sum("按群维护循环已启动" in line for line in lines) == 2, lines
