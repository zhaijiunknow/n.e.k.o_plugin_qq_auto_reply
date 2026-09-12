"""一键部署后的「等扫码登录」轮询。

出码之后用户还得拿手机扫，这段时间以前没人盯着 —— 得靠用户自己想起来点
「补写 OneBot 配置」。现在前端定时调 `deploy(action="login_poll")`，后端扫到
登录成功就把收尾做完。

要紧的是**防重入**：收尾会写配置、起运行时，而前端是 5 秒一次 —— 不记状态的话
每轮都重做一遍。

（收尾**不重启** NapCat：它热读 OneBot 配置，而重启会掐断用户刚建立的登录会话。）
"""
from __future__ import annotations

from types import SimpleNamespace

from plugin.plugins.qq_auto_reply import napcat_onebot_config as cfg
from plugin.plugins.qq_auto_reply.deploy_service import QQDeployService


def _svc(tmp_path, *, logged_uin: str = "", auto_ok: bool = True):
    calls = {"applied": [], "runtime": []}

    async def _apply(*, uin="", restart=False, emit=None):
        calls["applied"].append(uin)
        calls.setdefault("restart", []).append(restart)
        return {"ok": True, "uin": uin, "restarted": restart}

    async def _restart(auto_start):
        calls["runtime"].append(auto_start)
        if not auto_start:
            # 与真实实现一致：auto_start=False 时早退，不动运行时
            return {"ok": False, "status": "", "error": ""}
        return {"ok": auto_ok, "status": "started" if auto_ok else "",
                "error": "" if auto_ok else "启动被拒"}

    svc = QQDeployService.__new__(QQDeployService)
    svc.plugin = SimpleNamespace(
        _qq_settings={},
        _emit_log=lambda *a, **k: None,
        napcat_service=SimpleNamespace(get_napcat_directory=lambda: tmp_path),
        _auto_start_fields=staticmethod(
            lambda auto: {"auto_started": bool(auto.get("ok")),
                          "auto_start_status": str(auto.get("status") or ""),
                          "auto_start_error": str(auto.get("error") or "")}),
    )
    svc.apply_onebot_config = _apply
    svc._restart_auto_reply_runtime = _restart

    d = cfg.config_dir_of(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    if logged_uin:
        (d / f"onebot11_{logged_uin}.json").write_text("{}", encoding="utf-8")
    return svc, calls


# ── 还没登录 ────────────────────────────────────────────────

async def test_pending_when_nobody_has_logged_in(tmp_path):
    svc, calls = _svc(tmp_path)

    assert await svc.poll_login() == {"status": "pending"}
    assert calls["applied"] == [], "没登录就不该动手"


# ── 登录了 ──────────────────────────────────────────────────

async def test_completes_once_the_account_appears(tmp_path):
    """登录成功的信号 = ``onebot11_<uin>.json`` 出现（NapCat 登录后自己建的）。"""
    svc, calls = _svc(tmp_path, logged_uin="3281414178")

    r = await svc.poll_login()

    assert r["status"] == "completed"
    assert r["uin"] == "3281414178"
    assert calls["applied"] == ["3281414178"]


async def test_second_poll_is_a_noop(tmp_path):
    """**防重入**：前端每 5 秒调一次，第二次必须什么都不做 ——
    否则每轮都会 stop/start 一次 NapCat。"""
    svc, calls = _svc(tmp_path, logged_uin="3281414178")

    assert (await svc.poll_login())["status"] == "completed"
    assert (await svc.poll_login())["status"] == "already"
    assert (await svc.poll_login())["status"] == "already"

    assert calls["applied"] == ["3281414178"], "只能收尾一次"


async def test_a_different_account_completes_again(tmp_path):
    """换号登录时要重新收尾 —— 只按 uin 记，不是"做过了就永远不做"。"""
    svc, calls = _svc(tmp_path, logged_uin="111")

    assert (await svc.poll_login())["status"] == "completed"

    d = cfg.config_dir_of(tmp_path)
    (d / "onebot11_222.json").write_text("{}", encoding="utf-8")
    r = await svc.poll_login()

    assert r["status"] == "completed" and r["uin"] == "222"
    assert calls["applied"] == ["111", "222"]


# ── 边界 ────────────────────────────────────────────────────

async def test_pending_when_the_filename_carries_no_uin(tmp_path):
    """目录里只有一个不合规的名字时不能崩，也不能拿它去收尾。"""
    d = cfg.config_dir_of(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "onebot11_.json").write_text("{}", encoding="utf-8")
    svc, calls = _svc(tmp_path)

    assert (await svc.poll_login())["status"] == "pending"
    assert calls["applied"] == []


async def test_missing_napcat_dir_raises(tmp_path):
    """还没定位到 NapCat 就来轮询 —— 明确报错，让前端把它当失败停下。"""
    import pytest

    svc = QQDeployService.__new__(QQDeployService)
    svc.plugin = SimpleNamespace(
        _qq_settings={},
        _emit_log=lambda *a, **k: None,
        napcat_service=SimpleNamespace(get_napcat_directory=lambda: None),
    )

    with pytest.raises(RuntimeError):
        await svc.poll_login()


# ── 收尾必须**把自动回复拉起来** ────────────────────────────

async def test_completion_also_starts_the_runtime(tmp_path):
    """回归：服务层的 apply_onebot_config 只"写配置 + 重启 NapCat"，
    "启动自动回复"那一步在入口包装层。poll_login 直接调服务，绕过了那层 ——
    不自己补上就是收尾收一半：配置对了、NapCat 重启了，自动回复却没起来，
    用户还得手动点一次「启动」。
    """
    svc, calls = _svc(tmp_path, logged_uin="111")

    r = await svc.poll_login()

    assert calls["runtime"] == [True], "收尾没启动自动回复"
    assert r["auto_started"] is True


async def test_auto_start_false_only_writes_config(tmp_path):
    svc, calls = _svc(tmp_path, logged_uin="111")

    r = await svc.poll_login(auto_start=False)

    assert calls["applied"] == ["111"]        # 配置照写
    assert calls["runtime"] == [False]        # 但不动运行时
    assert r["auto_started"] is False


async def test_runtime_failure_is_reported_not_raised(tmp_path):
    """运行时起不来不能让整个收尾变失败 —— 配置此时已经写好了。"""
    svc, calls = _svc(tmp_path, logged_uin="111", auto_ok=False)

    r = await svc.poll_login()

    assert r["status"] == "completed"
    assert r["auto_started"] is False
    assert len(calls["runtime"]) == 1


async def test_completion_does_not_restart_napcat(tmp_path):
    """**不重启**：NapCat 热读 OneBot 配置，改完直接生效。

    而重启的代价很实在 —— 用户刚扫码登录成功，重启就把会话掐断了，只能靠
    快速登录捞回来。
    """
    svc, calls = _svc(tmp_path, logged_uin="111")

    await svc.poll_login()

    assert calls["restart"] == [False]
