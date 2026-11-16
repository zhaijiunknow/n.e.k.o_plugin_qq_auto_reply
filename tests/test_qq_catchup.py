"""断线补消息的判据。

这块代码的价值全在"判得对"上：判错一个方向是**她对着几小时前的话一口气回
十条**，判错另一个方向是**漏掉的话永远补不回来，而且日志一片绿**。所以每个
边界都单独钉一条，包括三个不显眼但真会出事的：

* 拿不到 ``message_seq`` 时不许拿时间戳凑（凑出来的数会让游标永久错位）；
* ``{}`` 不是"真没消息"（超时会被 `or {}` 吞掉，开放平台干脆返回空实现）；
* 第一次遇到的会话只对齐、不注入（否则新加一个好友就"抽风"）。
"""

from __future__ import annotations

import asyncio
import functools
import time
from pathlib import Path
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.catchup_service import QQMessageCatchupService


def _sync(fn):
    """把 async 测试包成同步的。

    仓库里没有 pytest-asyncio（上游测试一直是 ``asyncio.run(...)`` 手写），
    引一个新依赖只为跑这几条断言不值得，CI 也不保证装。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _RecordingLogger(_Logger):
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, message="", *a, **k):
        self.warnings.append(str(message))

    def info(self, message="", *a, **k):
        self.infos.append(str(message))


class _Client:
    """假连接：history 里 key 是 ``group:<id>`` / ``private:<id>``。"""

    def __init__(self) -> None:
        self.connected = True
        self.self_id = "999"
        self.history: dict[str, list[dict]] = {}
        self.empty_payload = False
        self.history_calls = 0
        #: 第几次问登录信息时才答得上来（0 = 永远答不上）。只在 ``self_id`` 为空时会被问到。
        self.login_after = 0
        self.login_calls = 0

    def is_connected(self) -> bool:
        return self.connected

    async def get_login_info(self) -> dict:
        self.login_calls += 1
        if self.login_after and self.login_calls >= self.login_after:
            self.self_id = "999"
            return {"user_id": "999"}
        raise RuntimeError("not logged in yet")

    async def get_group_msg_history(self, group_id, message_seq: int = 0, count: int = 40):
        self.history_calls += 1
        if self.empty_payload:
            # 开放平台的同名空实现 / 超时被 `or {}` 吞掉，长这样
            return {}
        if message_seq:
            # 第二次翻页返回空，让分页循环正常停下
            return {"messages": []}
        return {"messages": list(self.history.get(f"group:{group_id}", []))}

    async def get_friend_msg_history(self, user_id, message_seq: int = 0, count: int = 40):
        self.history_calls += 1
        if self.empty_payload:
            return {}
        if message_seq:
            return {"messages": []}
        return {"messages": list(self.history.get(f"private:{user_id}", []))}


class _Dispatcher:
    def __init__(self) -> None:
        self.injected: list[dict] = []

    async def handle_message(self, message: dict) -> None:
        self.injected.append(dict(message))


def _plugin(tmp_path: Path, *, settings: dict | None = None):
    client = _Client()
    dispatcher = _Dispatcher()
    plugin = SimpleNamespace(
        data_path=lambda: str(tmp_path),
        logger=_Logger(),
        _qq_settings=dict(settings or {}),
        _running=True,
        qq_client=client,
        message_dispatcher=dispatcher,
        _emit_log=lambda *a, **k: None,
        _admin_qq="",
    )
    return plugin, client, dispatcher


def _msg(seq: int, *, mid: str = "", sender: str = "123", ts: int | None = None) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "500",
        "user_id": sender,
        "message_id": mid or f"m{seq}",
        "message_seq": seq,
        "time": int(ts if ts is not None else time.time()),
        "message": [{"type": "text", "data": {"text": f"第{seq}条"}}],
    }


# ── 游标 ────────────────────────────────────────────────────


def test_note_message_advances_the_cursor(tmp_path: Path) -> None:
    plugin, _client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)

    svc.note_message(_msg(5, mid="a"))
    svc.note_message(_msg(7, mid="b"))

    assert svc._cursors["group:500"]["seq"] == 7
    assert "a" in svc._recent_ids and "b" in svc._recent_ids


def test_a_smaller_seq_never_moves_the_cursor_backwards(tmp_path: Path) -> None:
    """乱序到达不该把位置往回拉——拉回去下次就会重补一遍。"""
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)

    svc.note_message(_msg(9))
    svc.note_message(_msg(4))

    assert svc._cursors["group:500"]["seq"] == 9


def test_missing_seq_marks_the_cursor_untrusted_instead_of_faking_one(tmp_path: Path) -> None:
    """拿不到 message_seq 时绝不能拿时间戳凑。

    实测踩过：凑出来的数（time*1000，一万亿量级）比所有历史消息的 seq 都大，
    之后补消息永久失效——判据变成"没有一条比它新"。
    """
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)

    raw = _msg(0, mid="x")
    raw.pop("message_seq")
    svc.note_message(raw)

    rec = svc._cursors["group:500"]
    assert rec["seq"] == 0, "不许编一个位置出来"
    assert rec["seq_unknown"] is True
    assert svc._diag["seq_missing"] == 1


def test_notice_messages_do_not_touch_the_cursor(tmp_path: Path) -> None:
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)

    svc.note_message({"post_type": "notice", "notice_type": "poke", "group_id": "500", "user_id": "123"})

    assert svc._cursors == {}


# ── 首次会话只对齐 ──────────────────────────────────────────


@_sync
async def test_a_conversation_seen_for_the_first_time_only_aligns(tmp_path: Path) -> None:
    """新加的好友 / 首次启动：只记位置，不把历史倒进管道。

    不这么做的话，第一次跑起来她会对着几小时前的话一口气回十二条，像抽风。
    """
    plugin, client, dispatcher = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    client.history["group:500"] = [_msg(100), _msg(101), _msg(102)]
    # 现场事件先来过一条，但没带 seq → 位置不可信
    raw = _msg(0, mid="first")
    raw.pop("message_seq")
    svc.note_message(raw)

    # 不用 force：force=True 是给手动补救留的口子，本来就会跳过这道闸
    result = await svc.catch_up()

    assert dispatcher.injected == []
    assert result["injected"] == 0
    detail = result["details"][0]
    assert detail["status"] == "aligned"
    assert svc._cursors["group:500"]["seq"] == 102
    assert not svc._cursors["group:500"].get("seq_unknown"), "对齐完要把脏标记清掉"


@_sync
async def test_first_sight_of_a_brand_new_group_does_not_replay_history(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    client.history["group:500"] = [_msg(1), _msg(2)]
    svc.note_message(_msg(3))  # 有 seq，但 updated 刚被写入
    # 模拟"从没补过"：updated 为 0 且游标为 0
    svc._cursors["group:500"]["seq"] = 0
    svc._cursors["group:500"]["updated"] = 0

    result = await svc.catch_up()

    assert dispatcher.injected == []
    assert result["details"][0]["status"] == "aligned"


# ── 真补消息 ────────────────────────────────────────────────


@_sync
async def test_messages_newer_than_the_cursor_get_injected(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(10, mid="seen"), _msg(11, mid="new1"), _msg(12, mid="new2")]

    result = await svc.catch_up(force=True)

    assert [m["message_id"] for m in dispatcher.injected] == ["new1", "new2"]
    assert result["injected"] == 2
    assert all(m["_catchup"] is True for m in dispatcher.injected)
    assert svc._cursors["group:500"]["seq"] == 12


@_sync
async def test_an_already_handled_message_id_is_skipped_even_if_the_cursor_is_behind(tmp_path: Path) -> None:
    """游标慢一拍时靠名单兜住。

    实测事故：插件重载时游标还没落盘（写完有 1 秒防抖），重连后上一轮刚回过的
    消息被当成漏消息又补了一遍，她对着同一条链接回了两次。
    """
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    svc._recent_ids["dup"] = time.time()
    client.history["group:500"] = [_msg(11, mid="dup"), _msg(12, mid="ok")]

    result = await svc.catch_up(force=True)

    assert [m["message_id"] for m in dispatcher.injected] == ["ok"]
    assert result["details"][0]["skipped_dup"] == 1


@_sync
async def test_her_own_messages_are_never_replayed(tmp_path: Path) -> None:
    """自己发的不补：那不是"漏掉的消息"，补进去等于让她自问自答。"""
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [
        _msg(11, mid="mine", sender=client.self_id),
        _msg(12, mid="theirs"),
    ]

    result = await svc.catch_up(force=True)

    assert [m["message_id"] for m in dispatcher.injected] == ["theirs"]
    assert result["details"][0]["skipped_self"] == 1


@_sync
async def test_messages_older_than_the_window_are_skipped(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(
        tmp_path, settings={"catchup_stagger_ms": 0, "catchup_window_hours": 12},
    )
    svc = QQMessageCatchupService(plugin)
    old_ts = int(time.time()) - 20 * 3600
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(11, mid="old", ts=old_ts)]

    result = await svc.catch_up(force=True)

    assert dispatcher.injected == []
    assert result["details"][0]["skipped_old"] == 1


@_sync
async def test_per_conversation_cap_is_respected(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(
        tmp_path, settings={"catchup_stagger_ms": 0, "catchup_max_per_conversation": 2},
    )
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(n, mid=f"n{n}") for n in range(11, 16)]

    result = await svc.catch_up(force=True)

    assert len(dispatcher.injected) == 2
    assert result["details"][0]["skipped_capped"] == 3


# ── 失败分类 ────────────────────────────────────────────────


@_sync
async def test_empty_dict_is_a_failure_not_an_empty_history(tmp_path: Path) -> None:
    """``{}`` 不能算"真没消息"。

    它可能是超时被 `or {}` 吞掉，也可能是开放平台的同名空实现。当成"没消息"
    的话，用户会看到"补完了"，而实际一条都没补——静默失效比报错糟得多。
    """
    plugin, client, dispatcher = _plugin(tmp_path)
    client.empty_payload = True
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))

    result = await svc.catch_up(force=True)

    assert result["details"][0]["status"] == "failed"
    assert svc._diag["conv_failed"] == 1


# ── 登录后补一轮 ────────────────────────────────────────────
#
# 只在"登录上了"的时候补一轮（使用者 2026-11-16）：自动回复启动后第一次连上，以及运行中
# 掉线后重新连上。其余时间不碰历史接口。
#
# 之前是每 30 秒采样一次连接状态，而且只要哪个会话带着"缺口"标记就把最近的会话全拉一遍；
# 序号跳一下就会打上标记，拉取失败时标记又清不掉。真机 11-15 全天：历史接口 7726 次，
# 其中 5312 次失败，补回 0 条。


def _fake_rounds(svc) -> list[str]:
    calls: list[str] = []

    async def _fake_catch_up(*, reason: str = "", force: bool = False, window_seconds=None):
        calls.append(reason)
        return {"status": "ok"}

    svc.catch_up = _fake_catch_up  # type: ignore[assignment]
    return calls


@_sync
async def test_the_first_login_after_start_runs_one_round(tmp_path: Path) -> None:
    """启动后第一次连上就补 —— 插件停着的那段时间里别人说的话，以前是不补的。"""
    plugin, _client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    calls = _fake_rounds(svc)

    await svc._watch_tick()

    assert calls == ["startup"]
    assert svc._was_connected is True


@_sync
async def test_the_startup_round_waits_for_the_connection(tmp_path: Path) -> None:
    """反向 WS：自动回复启动时后端还没连进来。等它连上再补，而且仍然算"启动"那一轮。"""
    plugin, client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    calls = _fake_rounds(svc)

    client.connected = False
    await svc._watch_tick()
    await svc._watch_tick()
    assert calls == []
    client.connected = True
    await svc._watch_tick()

    assert calls == ["startup"]


@_sync
async def test_the_round_waits_for_the_login_identity(tmp_path: Path) -> None:
    """连上不等于登录上：不知道自己是谁就分不清哪些是她自己发的，先不补。"""
    plugin, client, _d = _plugin(tmp_path)
    client.self_id = ""
    client.login_after = 3
    svc = QQMessageCatchupService(plugin)
    calls = _fake_rounds(svc)

    await svc._watch_tick()
    await svc._watch_tick()
    assert calls == [] and client.login_calls == 2
    await svc._watch_tick()          # 这一次登录信息拿到了

    assert calls == ["startup"]
    await svc._watch_tick()
    assert calls == ["startup"] and client.login_calls == 3, "拿到之后不再问"


@_sync
async def test_a_login_that_never_completes_drops_the_round(tmp_path: Path) -> None:
    plugin, client, _d = _plugin(tmp_path)
    client.self_id = ""
    svc = QQMessageCatchupService(plugin)
    svc.LOGIN_WAIT_TICKS = 3
    calls = _fake_rounds(svc)

    for _ in range(8):
        await svc._watch_tick()

    assert calls == []
    assert client.login_calls == 3, "等够了就不再问，也不再补这一轮"


@_sync
async def test_reconnect_after_a_drop_runs_one_round(tmp_path: Path) -> None:
    plugin, client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    calls = _fake_rounds(svc)

    await svc._watch_tick()          # 启动后第一次连上
    client.connected = False
    await svc._watch_tick()          # 掉线
    client.connected = True
    await svc._watch_tick()          # 重新连上 → 再补一轮

    assert calls == ["startup", "reconnect"]


@_sync
async def test_staying_connected_runs_nothing_more(tmp_path: Path) -> None:
    plugin, _client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    calls = _fake_rounds(svc)

    for _ in range(5):
        await svc._watch_tick()

    assert calls == ["startup"]


@_sync
async def test_disabled_switch_stops_everything(tmp_path: Path) -> None:
    plugin, client, _d = _plugin(tmp_path, settings={"catchup_enabled": False})
    svc = QQMessageCatchupService(plugin)
    calls = _fake_rounds(svc)

    await svc._watch_tick()
    client.connected = False
    await svc._watch_tick()
    client.connected = True
    await svc._watch_tick()

    assert calls == []


@_sync
async def test_switching_it_on_later_does_not_replay_from_the_start_bookmark(tmp_path: Path) -> None:
    """关着的时候不欠账：中途打开，不该拿启动时的位置去翻这段时间早就处理过的消息。"""
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_enabled": False, "catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))
    svc._bookmark_gaps()                         # start() 做的那一步
    await svc._watch_tick()
    client.history["group:500"] = [_msg(11), _msg(12)]
    svc.note_message(_msg(12))
    svc._recent_ids.clear()                      # 最坏情况：去重名单已经轮换掉了
    plugin._qq_settings["catchup_enabled"] = True
    for _ in range(3):
        await svc._watch_tick()

    assert client.history_calls == 0 and dispatcher.injected == []
    assert "pending_seq" not in svc._cursors["group:500"]


@_sync
async def test_bookmarks_written_by_the_old_logic_are_dropped_on_load(tmp_path: Path) -> None:
    """旧版会因为序号跳号记书签，而且可能很久没清：升级后第一轮不能从那么旧的位置翻起。"""
    import json

    plugin, _client, _d = _plugin(tmp_path)
    cursor = {"seq": 500, "at": 0, "kind": "group", "peer": "500", "updated": 1, "pending_seq": 12}
    path = tmp_path / QQMessageCatchupService.CURSOR_FILENAME
    path.write_text(json.dumps({"version": 1, "conversations": {"group:500": cursor}, "recent_ids": {}}), encoding="utf-8")
    old = QQMessageCatchupService(plugin)
    await old._load()
    assert old._cursors["group:500"]["seq"] == 500 and "pending_seq" not in old._cursors["group:500"]

    path.write_text(json.dumps({"version": 2, "conversations": {"group:500": cursor}, "recent_ids": {}}), encoding="utf-8")
    current = QQMessageCatchupService(plugin)
    await current._load()
    assert current._cursors["group:500"]["pending_seq"] == 12


@_sync
async def test_the_startup_round_only_reaches_back_an_hour(tmp_path: Path) -> None:
    """插件停了半天再启动，她不该对着几小时前的话开口：启动那一轮只补最近一小时。"""
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    now = int(time.time())
    client.history["group:500"] = [
        _msg(11, mid="two-hours-ago", ts=now - 7200), _msg(12, mid="ten-minutes-ago", ts=now - 600),
    ]

    await svc._watch_tick()

    assert [m["message_id"] for m in dispatcher.injected] == ["ten-minutes-ago"]


@_sync
async def test_the_reconnect_round_uses_the_configured_window(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    await svc._watch_tick()                      # 启动那一轮：没有新的
    client.connected = False
    await svc._watch_tick()
    client.history["group:500"] = [_msg(11, mid="two-hours-ago", ts=int(time.time()) - 7200)]
    client.connected = True
    svc._last_catchup_at = 0                     # 这条不测限流
    await svc._watch_tick()

    assert [m["message_id"] for m in dispatcher.injected] == ["two-hours-ago"]


@_sync
async def test_a_sequence_jump_alone_never_polls_history(tmp_path: Path) -> None:
    """序号跳一下不是掉线的证据 —— 多半只是她自己发的那条占了一个号。"""
    plugin, client, _dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))
    await svc._watch_tick()
    after_login = client.history_calls
    for seq in (12, 15, 19):
        svc.note_message(_msg(seq))
        await svc._watch_tick()

    assert client.history_calls == after_login
    assert "pending_seq" not in svc._cursors["group:500"]


@_sync
async def test_a_failing_conversation_is_not_retried_until_the_next_login(tmp_path: Path) -> None:
    """拉不到就记一笔、到此为止。以前失败的会话每个周期重试一遍，没有上限。"""
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    client.empty_payload = True
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))

    for _ in range(6):
        await svc._watch_tick()
    assert client.history_calls == 1
    assert "pending_seq" not in svc._cursors["group:500"]

    client.connected = False
    await svc._watch_tick()
    client.empty_payload = False
    client.history["group:500"] = [_msg(11)]
    client.connected = True
    svc._last_catchup_at = 0
    await svc._watch_tick()
    assert [m["message_seq"] for m in dispatcher.injected] == [11]


@_sync
async def test_flapping_defers_the_round_instead_of_dropping_it(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))
    await svc._watch_tick()                      # 启动那一轮
    client.connected = False
    await svc._watch_tick()
    client.history["group:500"] = [_msg(11)]
    client.connected = True
    await svc._watch_tick()                      # 冷却期内：先不打接口
    assert dispatcher.injected == []
    svc._last_catchup_at -= svc.MIN_INTERVAL_SECONDS + 1
    await svc._watch_tick()                      # 冷却过了：补上，而不是丢掉这一轮

    assert [m["message_seq"] for m in dispatcher.injected] == [11]


@_sync
async def test_napcat_reports_an_empty_conversation_as_an_error(tmp_path: Path) -> None:
    """NapCat 对"这个会话里一条都没有"抛 ``消息<序号>不存在``（它的实现是列表为空就抛）。

    真机 11-15：5312 条「历史失败: 消息0不存在」全是这个 —— 是空，不是故障。
    """
    plugin, client, dispatcher = _plugin(tmp_path)
    plugin.logger = logger = _RecordingLogger()
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))

    async def fetch(*args, **kwargs):
        raise RuntimeError("消息0不存在")

    client.get_group_msg_history = fetch
    result = await svc.catch_up(force=True)

    assert result["details"][0]["status"] == "empty"
    assert svc._diag["conv_failed"] == 0 and logger.warnings == []
    assert dispatcher.injected == []


@_sync
async def test_each_round_leaves_exactly_one_summary_line(tmp_path: Path) -> None:
    plugin, client, _d = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    plugin.logger = logger = _RecordingLogger()
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(11, mid="new")]

    for _ in range(4):
        await svc._watch_tick()

    lines = [line for line in logger.infos if "登录后补消息" in line]
    assert len(lines) == 1 and "启动" in lines[0] and "补回 1 条" in lines[0]


# ── 限流 ────────────────────────────────────────────────────


@_sync
async def test_two_catch_ups_within_the_cooldown_are_throttled(tmp_path: Path) -> None:
    """连接抖动时可能一秒触发好几次，没必要每次都打接口。"""
    plugin, client, _d = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(11, mid="n")]

    first = await svc.catch_up(force=True)
    second = await svc.catch_up()

    assert first["status"] == "ok"
    assert second["status"] == "throttled"


# ── 持久化 ──────────────────────────────────────────────────


@_sync
async def test_cursor_survives_a_restart(tmp_path: Path) -> None:
    """游标要落盘：插件重载后不该从零开始重新对齐。"""
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(42, mid="keep"))
    await svc._flush(force=True)

    fresh = QQMessageCatchupService(plugin)
    await fresh._load()

    assert fresh._cursors["group:500"]["seq"] == 42
    assert "keep" in fresh._recent_ids


def test_state_snapshot_exposes_diag(tmp_path: Path) -> None:
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(1))

    state = svc.describe_state()

    assert state["enabled"] is True
    assert state["conversations"] == 1
    assert "conv_failed" in state["diag"]


@_sync
async def test_a_live_message_before_the_round_does_not_hide_what_was_missed(tmp_path):
    """重新连上之后、补之前先到了一条实时消息：游标被它推过去了，漏掉的那条仍要补。"""
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))
    await svc._watch_tick()
    client.connected = False
    await svc._watch_tick()                      # 掉线：记下"从 10 之后可能有漏的"
    client.connected = True
    svc.note_message(_msg(12))                   # 实时消息先到
    client.history["group:500"] = [_msg(10), _msg(11), _msg(12)]
    svc._last_catchup_at = 0
    await svc._watch_tick()

    assert [m["message_seq"] for m in dispatcher.injected] == [11]
    assert svc._cursors["group:500"]["seq"] == 12
    assert "pending_seq" not in svc._cursors["group:500"]


@_sync
async def test_start_remembers_where_the_last_run_stopped(tmp_path):
    """重启同理：启动到补之间来的实时消息，不能盖掉上次运行停下的位置。"""
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    previous = QQMessageCatchupService(plugin)
    previous.note_message(_msg(10))
    await previous._flush(force=True)

    svc = QQMessageCatchupService(plugin)
    client.history["group:500"] = [_msg(11), _msg(12)]
    await svc.start()
    svc.note_message(_msg(12))                   # 第一次采样之前先到的实时消息
    await asyncio.sleep(0.05)
    await svc.stop()

    assert [m["message_seq"] for m in dispatcher.injected] == [11]


@_sync
async def test_the_disconnect_bookmark_survives_a_restart(tmp_path):
    plugin, client, _dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))
    await svc._watch_tick()
    client.connected = False
    await svc._watch_tick()
    await svc._flush(force=True)

    fresh = QQMessageCatchupService(plugin)
    await fresh._load()

    assert fresh._cursors["group:500"]["pending_seq"] == 10


@_sync
async def test_failed_injection_is_retried_even_if_dispatcher_noted_it(tmp_path):
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))
    client.history["group:500"] = [_msg(11), _msg(12)]
    original = dispatcher.handle_message

    async def fail(message):
        svc.note_message(message)
        raise RuntimeError("temporary failure")

    dispatcher.handle_message = fail
    first = await svc.catch_up(force=True)
    assert first["details"][0]["status"] == "failed"
    dispatcher.handle_message = original
    await svc.catch_up(force=True)
    assert [m["message_seq"] for m in dispatcher.injected] == [11, 12]


@_sync
async def test_live_messages_during_the_fetch_leave_no_bookmark_behind(tmp_path):
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))

    async def fetch(*args, **kwargs):
        svc.note_message(_msg(14))
        return {"messages": [_msg(11), _msg(12)]}

    client.get_group_msg_history = fetch
    await svc.catch_up(force=True)

    assert [m["message_seq"] for m in dispatcher.injected] == [11, 12]
    assert svc._cursors["group:500"]["seq"] == 14
    assert "pending_seq" not in svc._cursors["group:500"], "没有'下一轮'了：留着只会让下次登录重翻一遍"


@_sync
async def test_overlapping_history_pages_do_not_replay_a_message_twice(tmp_path):
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))

    async def fetch(peer, message_seq=0, **kwargs):
        seqs = [12, 13] if not message_seq else [11, 12]
        return {"messages": [_msg(n, ts=0) for n in seqs]}

    client.get_group_msg_history = fetch
    await svc.catch_up(force=True)
    assert [m["message_seq"] for m in dispatcher.injected] == [11, 12, 13]


# ── 开放平台：没有历史消息接口，这个通道上不补 ──────────────────
#
# 真机（2026-11-16，从 OneBot 切到开放平台之后）：补消息每 30 秒对每个会话报一次
#   [Catchup] 拉取 group:… 历史失败: QQOpenPlatformConnection.get_group_msg_history()
#   takes 1 positional argument but 2 were given
# 一个多小时 1179 条。官方 v2 没有"按会话翻历史"这个接口：#3709 的适配器对这两个方法
# 直接抛"平台不提供"，而且只收关键字参数 —— 这边按位置传，连那句"不提供"都没走到。


class _OpenPlatformClient:
    """形状照 #3709 的 ``QQOpenPlatformConnection``：历史接口只收关键字参数，并且直接抛。"""

    CHANNEL = "open"

    def __init__(self) -> None:
        self.connected = True
        self.self_id = "bot-openid"
        self.history_calls = 0

    def is_connected(self) -> bool:
        return self.connected

    async def get_group_msg_history(self, **kw):
        self.history_calls += 1
        raise RuntimeError("QQ Open Platform does not offer message history: no such interface in v2")

    async def get_friend_msg_history(self, **kw):
        self.history_calls += 1
        raise RuntimeError("QQ Open Platform does not offer message history: no such interface in v2")


def _open_platform_service(tmp_path):
    plugin, _onebot_client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    plugin.qq_client = client = _OpenPlatformClient()
    plugin.logger = logger = _RecordingLogger()
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10))       # OneBot 时期留下的会话
    return svc, client, logger, dispatcher


@_sync
async def test_the_open_platform_never_touches_the_history_interface(tmp_path):
    svc, client, logger, dispatcher = _open_platform_service(tmp_path)
    for _ in range(4):
        await svc._watch_tick()
    assert client.history_calls == 0
    assert logger.warnings == [], "这个通道上补不了不是故障，不该告警"
    assert dispatcher.injected == []
    # 为什么没补要说一次，而且只说一次。
    assert len([line for line in logger.infos if "[Catchup]" in line and "开放平台" in line]) == 1


@_sync
async def test_an_open_platform_reconnect_does_not_start_a_round(tmp_path):
    svc, client, logger, _dispatcher = _open_platform_service(tmp_path)
    await svc._watch_tick()
    client.connected = False
    await svc._watch_tick()          # 掉线
    client.connected = True
    await svc._watch_tick()          # 重连
    assert client.history_calls == 0 and logger.warnings == []
    assert "pending_seq" not in svc._cursors["group:500"], "补不了的通道上不记缺口"
    assert svc._was_connected is True, "连接状态照常跟踪（状态页要看）"


@_sync
async def test_a_manual_catch_up_on_the_open_platform_says_unsupported(tmp_path):
    svc, client, logger, _dispatcher = _open_platform_service(tmp_path)
    result = await svc.catch_up(reason="manual", force=True)
    assert result["status"] == "unsupported" and result["injected"] == 0
    assert client.history_calls == 0 and logger.warnings == []
    assert svc.describe_state()["last_result"]["status"] == "unsupported"


@_sync
async def test_switching_back_to_onebot_catches_up_from_where_it_left(tmp_path):
    """在开放平台上按兵不动；换回有历史接口的通道后，第一次登录照常从原来的位置补。"""
    svc, _client, _logger, dispatcher = _open_platform_service(tmp_path)
    await svc._watch_tick()
    await svc._watch_tick()
    onebot = _Client()
    onebot.history["group:500"] = [_msg(11)]
    svc.plugin.qq_client = onebot
    await svc._watch_tick()
    assert [m["message_seq"] for m in dispatcher.injected] == [11]
