# -*- coding: utf-8 -*-
"""频率软提示：把「硬闸」那道断崖变成坡。

现状的问题：硬闸（`reply_burst_max_replies` 条 / `reply_burst_window_seconds`）到点
**直接静默** —— 用户看到的是"她突然不理我了"，而她并不知道自己刚才说多了。
软提示在到点**之前**把这件事写进提示词，让她自己收敛；硬闸只当兜底。

口径刻意与硬闸共用两个键（`reply_burst_*`），不新开一份窗口/上限：两处漂移的话，
"提醒她收敛"与"强制静默"会各说各话。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.attention_service import QQAttentionService

GROUP = "1048307485"

SETTINGS = {
    "attention_max_score": 10.0,
    "attention_focus_threshold": 4.0,
    "attention_focus_hold_threshold": 2.0,
    "attention_min_threshold": 1.0,
    "attention_emotion_multipliers": {"calm": 0.0},
    "backlog_labels": [],
    "reply_burst_window_seconds": 60,
    "reply_burst_max_replies": 3,
    "pacing_hint_enabled": True,
    "pacing_hint_ratio": 0.6,
}


class _Clock:
    def __init__(self, start: int = 100_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now


def _service(**overrides):
    settings = dict(SETTINGS)
    settings.update(overrides)
    plugin = SimpleNamespace(
        group_permission_mgr=SimpleNamespace(list_groups=lambda: [{"group_id": GROUP}]),
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


def _reply(svc, gid=GROUP):
    asyncio.run(svc.update_on_reply(gid))


# ── 计数口径 ────────────────────────────────────────────────────────

def test_replies_are_counted_within_the_window():
    svc, clock = _service()
    assert svc.recent_reply_count(GROUP) == 0
    _reply(svc)
    clock.now += 5
    _reply(svc)
    assert svc.recent_reply_count(GROUP) == 2


def test_window_expiry_drops_old_replies():
    svc, clock = _service()
    _reply(svc)
    clock.now += 61          # 窗口 60 秒
    assert svc.recent_reply_count(GROUP) == 0


def test_other_groups_do_not_share_the_counter():
    svc, clock = _service()
    _reply(svc, GROUP)
    assert svc.recent_reply_count("985066274") == 0


# ── 触发区间 ────────────────────────────────────────────────────────

def test_hint_appears_before_the_hard_gate():
    """3 条闸 + 0.6 → 第 2 条开始提醒；到第 3 条（硬闸）就不再提醒。"""
    svc, clock = _service()
    _reply(svc)
    assert svc.pacing_hint(GROUP) == "", "第一条之后不该提醒"

    clock.now += 3
    _reply(svc)
    hint = svc.pacing_hint(GROUP)
    assert "注意节奏" in hint and "已经说了 2 条" in hint

    clock.now += 3
    _reply(svc)
    assert svc.pacing_hint(GROUP) == "", "已到硬闸，此时由强制静默接管（再提醒没有意义）"


def test_hint_can_be_switched_off():
    svc, clock = _service(pacing_hint_enabled=False)
    for _ in range(2):
        clock.now += 3
        _reply(svc)
    assert svc.pacing_hint(GROUP) == ""


def test_ratio_moves_the_trigger_point():
    """比例 0.9 + 上限 10 → 第 9 条才开始提醒。"""
    svc, clock = _service(reply_burst_max_replies=10, pacing_hint_ratio=0.9)
    for _ in range(8):
        clock.now += 3
        _reply(svc)
    assert svc.pacing_hint(GROUP) == ""
    clock.now += 3
    _reply(svc)
    assert "注意节奏" in svc.pacing_hint(GROUP)


def test_window_and_limit_come_from_the_burst_keys():
    """口径必须与硬闸共用 —— 改 burst 键，提示跟着改。"""
    svc, clock = _service(reply_burst_window_seconds=600, reply_burst_max_replies=5, pacing_hint_ratio=0.6)
    for _ in range(3):
        clock.now += 3
        _reply(svc)
    # 3 >= round(5*0.6)=3 → 提醒，且文案里的窗口是 600 秒
    assert "600 秒" in svc.pacing_hint(GROUP)


# ── 注入路径 ────────────────────────────────────────────────────────

def test_hint_reaches_the_prompt_context():
    svc, clock = _service()
    for _ in range(2):
        clock.now += 3
        _reply(svc)
    context = svc.get_attention_context(GROUP)
    assert "注意节奏" in context, "提示没有进提示词 —— 那她永远不会知道该收敛"


def test_disabled_hint_never_reaches_the_prompt_context():
    svc, clock = _service(pacing_hint_enabled=False)
    for _ in range(2):
        clock.now += 3
        _reply(svc)
    assert "注意节奏" not in svc.get_attention_context(GROUP)
