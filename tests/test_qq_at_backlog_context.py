"""被 @ 的那一轮必须带上群聊前文（源码级契约）。

`run_retroactive_review` 只挂在**焦点切换**上，而 @ 不锁焦点
（`test_qq_group_participation.py::test_normal_group_at_bot_replies_without_locking_focus`）
—— 于是"被 @"成了唯一一个**从不**补前文的入口。真机上就是：非焦点群里有人 @ 她，
她手上只有那一句，backlog 里前面几十条一条都没进 prompt，回得很突兀。

**为什么是源码级断言**（同 `test_qq_failure_log_says_why.py` 的取舍）：这三件事埋
在 800 行的 `handle_group_message` 里，为"看见注入生效"搭一整套 dispatcher harness
（会话锁 / 门控 / 标记 / 多个假服务）不划算，而且测出来的是 harness 不是契约。
真正的契约只有四条，读源码就能判：

1. 只在 `is_at_bot` 且非合成轮时补；
2. 补的是 backlog 里**未审阅**那批；
3. 不把这条 @ 自己当前文重复一遍；
4. 喂过的就地标已审 —— 否则下一次真正的回溯会把它们再答一遍。
"""

from __future__ import annotations

import pathlib
import re

SRC_PATH = pathlib.Path(__file__).resolve().parent.parent / "message_dispatcher.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def test_source_is_where_we_think_it_is():
    """护栏：改错文件/搬走文件时这条先红，而不是下面几条静默通过。"""
    assert "async def handle_group_message(" in SRC
    assert "async def _build_at_backlog_tail(" in SRC


def test_at_turn_and_only_at_turn_pulls_the_tail():
    """① 只在被 @ 且非合成轮时补。"""
    guard = re.search(
        r"if is_at_bot and not synthetic_source and group_id:\s*\n"
        r"\s*backlog_tail = await self\._build_at_backlog_tail\(",
        SRC,
    )
    assert guard, "被 @ 时补前文的守卫没了（或条件被放宽/收紧）"


def test_tail_comes_from_unreviewed_backlog():
    """② 原料是 backlog 的未审阅消息，受既有上限约束。"""
    body = SRC.split("async def _build_at_backlog_tail(", 1)[1]
    assert "get_unreviewed_messages_since(" in body
    assert "retroactive_review_max_messages" in body
    assert "get_last_focus_at(" in body


def test_tail_skips_the_message_that_triggered_it():
    """③ 这条 @ 自己刚进 backlog，不能再被当前文喂一遍。"""
    body = SRC.split("async def _build_at_backlog_tail(", 1)[1]
    assert "current_message_id" in body
    assert "!= current" in body


def test_fed_messages_are_marked_reviewed():
    """④ 喂过就标已审，否则下一次回溯会重复回应同一批。"""
    body = SRC.split("async def _build_at_backlog_tail(", 1)[1]
    assert "mark_group_reviewed_payload(" in body
    assert "message_ids=reviewed_ids" in body


def test_a_broken_backlog_never_blocks_the_reply():
    """读不到前文只是少点上下文，不该让"有人 @ 她"变成没有回应。"""
    body = SRC.split("async def _build_at_backlog_tail(", 1)[1]
    assert 'return ""' in body
    assert "[@上下文] 读取 backlog 失败" in body


def test_member_labels_are_resolved_at_receipt():
    """①② 的原料：接收边界就把"本轮涉及的人"解析出来。"""
    assert "member_labels = await self._resolve_member_labels(" in SRC
    assert "member_labels=dict(member_labels or {})," in SRC
