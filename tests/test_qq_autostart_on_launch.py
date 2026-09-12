"""自启：插件启动时自动拉起 NapCat 并接上自动回复。

是 opt-in（``auto_start_on_launch``，默认关）：NapCat 为注入会接管 QQ 进程
（必要时结束掉正在运行的那个），不该由插件替用户决定。
"""
from __future__ import annotations

from types import MethodType, SimpleNamespace

from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
from plugin.sdk.plugin import Err, Ok, SdkError


def _plugin(settings: dict, *, startup_error: str = "", napcat_raises=None,
            start_result=None, ready=True):
    calls: list[str] = []

    async def _ensure():
        calls.append("ensure")
        if napcat_raises is not None:
            raise napcat_raises

    async def _wait():
        calls.append("wait")
        return ready

    async def _start():
        calls.append("start")
        return start_result if start_result is not None else Ok({"status": "started"})

    logs: list[str] = []
    p = SimpleNamespace(
        _qq_settings=settings,
        _emit_log=lambda level, msg: logs.append(f"{level}:{msg}"),
        napcat_service=SimpleNamespace(
            ensure_napcat_started=_ensure,
            get_startup_error=lambda: startup_error,
            wait_for_onebot_ready=_wait,
        ),
        runtime_ops_service=SimpleNamespace(start_auto_reply=_start),
    )
    p._autostart_on_launch = MethodType(QQAutoReplyPlugin._autostart_on_launch, p)
    return p, calls, logs


# ── 关着的时候什么都不做 ────────────────────────────────────

async def test_disabled_does_nothing():
    p, calls, logs = _plugin({})

    await p._autostart_on_launch()

    assert calls == []
    assert logs == []


async def test_explicit_false_does_nothing():
    p, calls, _ = _plugin({"auto_start_on_launch": False})

    await p._autostart_on_launch()

    assert calls == []


# ── 开着的时候按顺序走三步 ──────────────────────────────────

async def test_enabled_starts_napcat_then_reply():
    """顺序要紧：先拉 NapCat、等 OneBot 就绪，最后才起自动回复。"""
    p, calls, logs = _plugin({"auto_start_on_launch": True})

    await p._autostart_on_launch()

    assert calls == ["ensure", "wait", "start"]
    assert any("已启动" in m for m in logs)


# ── NapCat 起不来就别硬起自动回复 ───────────────────────────

async def test_startup_error_skips_the_runtime():
    """NapCat 有硬错误时还去 start_auto_reply，只会再报一个与真因无关的错。"""
    p, calls, logs = _plugin({"auto_start_on_launch": True}, startup_error="找不到启动器")

    await p._autostart_on_launch()

    assert calls == ["ensure"]
    assert any("找不到启动器" in m for m in logs)


async def test_exception_is_swallowed_and_logged():
    """自启失败不该把插件带下去 —— 插件本身是好的，用户还能手动开。"""
    p, calls, logs = _plugin({"auto_start_on_launch": True},
                             napcat_raises=RuntimeError("QQ 被占用"))

    await p._autostart_on_launch()          # 不抛

    assert calls == ["ensure"]
    assert any("QQ 被占用" in m for m in logs)


async def test_runtime_rejection_is_logged_not_raised():
    p, calls, logs = _plugin({"auto_start_on_launch": True},
                             start_result=Err(SdkError("NOT_INITIALIZED: 未初始化")))

    await p._autostart_on_launch()

    assert calls == ["ensure", "wait", "start"]
    assert any("已启动" in m for m in logs)   # 记的是"已启动"，被拒由服务层自己报
