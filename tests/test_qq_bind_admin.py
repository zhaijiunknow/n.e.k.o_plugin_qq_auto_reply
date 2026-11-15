"""扫码绑定成功时，把**扫码者**自动设成管理员。

开放平台拿不到 QQ 号，只给 openid。而绑定回包里的 ``user_openid`` 就是**扫码那个人**
的 openid —— 这是唯一一个"谁是主人"由**平台背书**的时刻。

不这么做的话，开放平台上只能靠 ``_maybe_reserve_open_platform_admin`` 那条
bootstrap：**第一个私聊机器人的人**成为管理员，而且只在信任名单为空时生效一次。
把一个陌生人的第一条私信当作"主人声明"，比平台回执弱得多。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
from plugin.plugins.qq_auto_reply import qq_official_bind as bind
from plugin.plugins.qq_auto_reply.permission import PermissionManager


def _plugin(monkeypatch, *, openid: str = "OPENID_OF_SCANNER", mode: str = "open_platform"):
    async def _poll(session):
        return {"status": "completed", "appid": "1903565393",
                "secret": "s", "user_openid": openid}

    async def _persist():
        return True

    async def _restart(auto_start):
        return {"ok": True, "status": "started", "error": ""}

    async def _stop(*, stop_napcat):
        p.stopped.append(stop_napcat)

    p = QQAutoReplyPlugin.__new__(QQAutoReplyPlugin)
    p._qq_settings = {"qq_connection_mode": mode}
    p.stopped = []
    p._stop_auto_reply_runtime = _stop
    p._qq_bind_session = bind.BindSession(task_id="t", bind_key="k", qrcode="")
    p.permission_mgr = PermissionManager()
    p.logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                               error=lambda *a: None)
    p.settings_service = SimpleNamespace(persist_business_config=_persist)
    p._refresh_admin_qq = lambda: None
    p._restart_auto_reply_runtime = _restart
    p._auto_start_fields = QQAutoReplyPlugin._auto_start_fields

    monkeypatch.setattr(bind, "poll_bind_result", _poll)

    # ⚠️ 必须打桩：真实的 verify_credentials 会去 POST 腾讯的取 token 接口，
    # 失败还重试 3 轮 —— 不打桩的话这几个用例会真的发网络请求，慢且看天吃饭。
    async def _verify(appid, secret, **kw):
        return {"ok": True, "attempts": 1, "expires_in": 0, "error": ""}

    monkeypatch.setattr(bind, "verify_credentials", _verify)
    return p


# ── 扫码者成为管理员 ────────────────────────────────────────

async def test_scanner_becomes_admin(monkeypatch):
    p = _plugin(monkeypatch)

    r = await p.deploy(action="bind_poll")

    assert r.is_ok(), r
    assert p.permission_mgr.is_admin("OPENID_OF_SCANNER")
    assert [u["qq"] for u in p.permission_mgr.list_users()] == ["OPENID_OF_SCANNER"]


async def test_scanning_again_does_not_duplicate(monkeypatch):
    """同一个号再扫一次（例如换机器人）不该在名单里堆两条。"""
    p = _plugin(monkeypatch)

    await p.deploy(action="bind_poll")
    p._qq_bind_session = bind.BindSession(task_id="t2", bind_key="k", qrcode="")
    await p.deploy(action="bind_poll")

    assert len(p.permission_mgr.list_users()) == 1


async def test_existing_trust_list_is_not_wiped(monkeypatch):
    """名单里已有别人时，只加扫码者，不动其他人。"""
    p = _plugin(monkeypatch)
    p.permission_mgr.add_user("111", "trusted")

    await p.deploy(action="bind_poll")

    levels = {u["qq"]: u["level"] for u in p.permission_mgr.list_users()}
    assert levels == {"111": "trusted", "OPENID_OF_SCANNER": "admin"}


# ── 平台没给 openid 时不能把绑定拖垮 ────────────────────────

async def test_missing_openid_still_completes_the_bind(monkeypatch):
    """平台没回 openid（或改了字段名）→ 不设管理员，但绑定本身照常成功 ——
    凭据已经拿到了，不该因为这一条把整次绑定判失败。"""
    p = _plugin(monkeypatch, openid="")

    r = await p.deploy(action="bind_poll")

    assert r.is_ok(), r
    assert r.value["appid"] == "1903565393"
    assert p.permission_mgr.list_users() == []


async def test_invalid_openid_is_logged_not_raised(monkeypatch):
    """空白/非法 openid 走 add_user 的 False 分支，只记日志。"""
    p = _plugin(monkeypatch, openid="   ")

    r = await p.deploy(action="bind_poll")

    assert r.is_ok(), r
    assert p.permission_mgr.list_users() == []


# ── 绑定完成 = 改用开放平台 ─────────────────────────────────
#
# 没有 OneBot 界面的版本里页面上没有「连接方式」按钮，扫码绑定就是"改用开放平台"的那个
# 动作。绑定回包只带凭据；连接方式不跟着切的话，收尾会按旧方式再起一条 OneBot 连接。

@pytest.mark.parametrize("mode", ["napcat", "napcat_forward", ""])
async def test_bind_switches_an_onebot_setup_to_the_open_platform(monkeypatch, mode):
    p = _plugin(monkeypatch, mode=mode)

    r = await p.deploy(action="bind_poll")

    assert r.is_ok(), r
    assert p._qq_settings["qq_connection_mode"] == "open_platform"
    assert p.stopped == [True], "旧的 OneBot 运行时要连同插件拉起的后端进程一起停"


async def test_bind_on_the_open_platform_leaves_the_runtime_to_the_rebuild(monkeypatch):
    p = _plugin(monkeypatch, mode="open_platform")

    await p.deploy(action="bind_poll")

    assert p._qq_settings["qq_connection_mode"] == "open_platform"
    assert p.stopped == [], "本来就是开放平台：只走收尾重建，不额外停后端"


async def test_an_unfinished_bind_does_not_touch_the_connection_mode(monkeypatch):
    """中途的轮询（还在等扫码 / 二维码过期）不能动现有连接。"""
    p = _plugin(monkeypatch, mode="napcat")

    async def _waiting(session):
        return {"status": "pending"}

    monkeypatch.setattr(bind, "poll_bind_result", _waiting)

    r = await p.deploy(action="bind_poll")

    assert r.is_ok() and r.value["status"] == "pending"
    assert p._qq_settings["qq_connection_mode"] == "napcat"
    assert p.stopped == []
