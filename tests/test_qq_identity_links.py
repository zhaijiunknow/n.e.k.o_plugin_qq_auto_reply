# -*- coding: utf-8 -*-
"""换通道后把人和群认回来（openid → 旧 QQ 号 / 群号）。见 ``identity_links`` 的模块说明。

钉四件事：认领只能凭管理员发的一次性码；认领之后名单和记忆键都落回旧账号；没认领的
一切照旧；撤销就是删一行映射。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply.identity_links import QQIdentityLinks
from plugin.plugins.qq_auto_reply.memory_bridge import QQMemoryBridge
from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService

OWNER_QQ, FRIEND_QQ, GROUP_QQ = "10001", "20002", "30003"
OPENID_PRIVATE = "A" * 32
OPENID_IN_GROUP = "B" * 32
GROUP_OPENID = "C" * 32


class _Client:
    CHANNEL = "open"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, user_id, text):
        self.sent.append((str(user_id), text))
        return "m1"

    async def send_group_message_segments(self, group_id, segments, **kw):
        self.sent.append((str(group_id), "".join(
            f"[reply:{s['data']['id']}]" if s["type"] == "reply" else s["data"]["text"] for s in segments)))
        return "m1"


def _plugin(tmp_path):
    plugin = SimpleNamespace(
        data_path=lambda name="": str(tmp_path / name),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        qq_client=_Client(),
        _refresh_admin_qq=lambda: None,
    )
    plugin.identity_links = QQIdentityLinks(plugin)
    QQSettingsService(plugin).rebuild_permission_managers({
        "trusted_users": [{"qq": OWNER_QQ, "level": "admin"},
                          {"qq": FRIEND_QQ, "level": "trusted", "nickname": "阿喵"}],
        "trusted_groups": [{"group_id": GROUP_QQ, "level": "trusted", "sticker_collect_enabled": True}],
    })
    plugin.memory_bridge = QQMemoryBridge(plugin)
    return plugin


def _say(plugin, text, *, sender=OPENID_PRIVATE, group="", at=True):
    message = {"message_type": "group" if group else "private", "user_id": sender, "group_id": group,
               "content": text, "is_at_bot": at, "message_id": "in-1"}
    return asyncio.run(plugin.identity_links.handle_message(message))


# ── 认领码 ──────────────────────────────────────────────────────────

def test_a_code_is_only_issued_for_someone_on_the_roster(tmp_path):
    links = _plugin(tmp_path).identity_links
    issued = links.issue_code("user", FRIEND_QQ)
    assert len(issued["code"]) == 8 and issued["legacy_id"] == FRIEND_QQ
    for kind, legacy in (("user", "99999"), ("group", FRIEND_QQ), ("user", ""), ("owner", OWNER_QQ)):
        with pytest.raises(ValueError):
            links.issue_code(kind, legacy)


def test_without_a_code_nobody_can_claim_anything(tmp_path):
    """没有待用的码时，这句话就是普通聊天：不拦、不回、不建映射。"""
    plugin = _plugin(tmp_path)
    assert _say(plugin, f"认领 {OWNER_QQ}") is False
    assert _say(plugin, "认领 ABCDEFGH") is False
    assert plugin.qq_client.sent == [] and plugin.identity_links.user(OPENID_PRIVATE) == OPENID_PRIVATE


def test_a_wrong_or_expired_code_links_nothing(tmp_path):
    plugin = _plugin(tmp_path)
    links = plugin.identity_links
    code = links.issue_code("user", OWNER_QQ, now=1000.0)["code"]
    assert links.claim("ZZZZZZZZ", sender_id=OPENID_PRIVATE, now=1001.0)["status"] == "invalid"
    assert links.claim(code, sender_id=OPENID_PRIVATE, now=1000.0 + links.CODE_TTL_SECONDS)["status"] == "invalid"
    assert links.user(OPENID_PRIVATE) == OPENID_PRIVATE
    assert plugin.permission_mgr.get_permission_level(OPENID_PRIVATE) == "none"


def test_a_code_works_once(tmp_path):
    """群里发出来的码别人看得见：第一次用过即废。"""
    plugin = _plugin(tmp_path)
    code = plugin.identity_links.issue_code("user", FRIEND_QQ)["code"]
    assert _say(plugin, f"认领 {code.lower()}", sender=OPENID_IN_GROUP, group=GROUP_OPENID) is True
    assert plugin.identity_links.user(OPENID_IN_GROUP) == FRIEND_QQ
    assert plugin.identity_links.claim(code, sender_id=OPENID_PRIVATE)["status"] == "invalid"
    assert plugin.identity_links.user(OPENID_PRIVATE) == OPENID_PRIVATE
    assert plugin.qq_client.sent[0][1].startswith("[reply:in-1]认领成功")


def test_a_group_code_must_be_used_inside_the_group_and_survives_a_misfire(tmp_path):
    plugin = _plugin(tmp_path)
    code = plugin.identity_links.issue_code("group", GROUP_QQ)["code"]
    assert _say(plugin, f"认领 {code}") is True                      # 发在了私聊
    assert "群" in plugin.qq_client.sent[-1][1] and plugin.identity_links.group(GROUP_OPENID) == GROUP_OPENID
    assert _say(plugin, f"认领 {code}", sender=OPENID_IN_GROUP, group=GROUP_OPENID, at=False) is False   # 没 @ 她
    assert _say(plugin, f"认领 {code}", sender=OPENID_IN_GROUP, group=GROUP_OPENID) is True
    assert plugin.identity_links.group(GROUP_OPENID) == GROUP_QQ
    assert plugin.identity_links.user(OPENID_IN_GROUP) == OPENID_IN_GROUP, "群码只认群，不顺手认人"


def test_claims_are_ignored_on_onebot(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.qq_client.CHANNEL = "onebot"
    code = plugin.identity_links.issue_code("user", FRIEND_QQ)["code"]
    assert _say(plugin, f"认领 {code}") is False


# ── 认领之后：名单与记忆键落回旧账号 ─────────────────────────────────

def test_a_claimed_person_gets_the_old_roster_entry_and_memory_keys(tmp_path):
    plugin = _plugin(tmp_path)
    bridge = plugin.memory_bridge
    assert plugin.permission_mgr.get_permission_level(OPENID_PRIVATE) == "none"
    assert bridge.participant_subject(OPENID_PRIVATE)["subject_id"] == f"qq:{OPENID_PRIVATE}"

    _say(plugin, f"认领 {plugin.identity_links.issue_code('user', FRIEND_QQ)['code']}")

    assert plugin.permission_mgr.get_permission_level(OPENID_PRIVATE) == "trusted"
    assert plugin.permission_mgr.get_nickname(OPENID_PRIVATE) == "阿喵"
    assert bridge.participant_subject(OPENID_PRIVATE) == QQMemoryBridge.participant_subject(FRIEND_QQ)
    assert bridge.speaker_account_id(OPENID_PRIVATE) == f"qq:{FRIEND_QQ}"
    # 没认领的人不受影响；类级调用（不经实例）给的仍是原样的键。
    assert bridge.participant_subject(OPENID_IN_GROUP)["subject_id"] == f"qq:{OPENID_IN_GROUP}"
    assert QQMemoryBridge.participant_subject(OPENID_PRIVATE)["subject_id"] == f"qq:{OPENID_PRIVATE}"


def test_a_claimed_group_gets_the_old_settings_and_memory_keys(tmp_path):
    plugin = _plugin(tmp_path)
    links, bridge = plugin.identity_links, plugin.memory_bridge
    _say(plugin, f"认领 {links.issue_code('group', GROUP_QQ)['code']}", sender=OPENID_IN_GROUP, group=GROUP_OPENID)
    _say(plugin, f"认领 {links.issue_code('user', OWNER_QQ)['code']}", sender=OPENID_IN_GROUP, group=GROUP_OPENID)

    groups = plugin.group_permission_mgr
    assert groups.get_group_level(GROUP_OPENID) == "trusted"
    assert groups.is_sticker_collection_enabled(GROUP_OPENID) is True
    assert bridge.group_subject(GROUP_OPENID) == QQMemoryBridge.group_subject(GROUP_QQ)
    assert bridge.group_participant_subject(GROUP_OPENID, OPENID_IN_GROUP) == (
        QQMemoryBridge.group_participant_subject(GROUP_QQ, OWNER_QQ))
    assert plugin.permission_mgr.get_permission_level(OPENID_IN_GROUP) == "admin", "主人在群里也被认成主人"


def test_an_id_with_its_own_roster_entry_keeps_that_entry(tmp_path):
    """扫码绑定时已经登记成管理员的 openid：认领别人的号不会改掉它自己的级别。"""
    plugin = _plugin(tmp_path)
    plugin.permission_mgr.add_user(OPENID_PRIVATE, "admin")
    _say(plugin, f"认领 {plugin.identity_links.issue_code('user', FRIEND_QQ)['code']}")
    assert plugin.permission_mgr.get_permission_level(OPENID_PRIVATE) == "admin"


# ── 撤销与持久化 ────────────────────────────────────────────────────

def test_links_survive_a_restart_and_unlinking_removes_one_row(tmp_path):
    plugin = _plugin(tmp_path)
    _say(plugin, f"认领 {plugin.identity_links.issue_code('user', FRIEND_QQ)['code']}")
    saved = json.loads((tmp_path / QQIdentityLinks.FILENAME).read_text(encoding="utf-8"))
    assert saved["users"] == {OPENID_PRIVATE: FRIEND_QQ} and "code" not in json.dumps(saved)

    restarted = _plugin(tmp_path)
    assert restarted.permission_mgr.get_permission_level(OPENID_PRIVATE) == "trusted"
    assert restarted.identity_links.has_pending() is False, "认领码不落盘：重启即作废"
    snapshot = restarted.identity_links.snapshot()
    assert snapshot["users"] == {OPENID_PRIVATE: FRIEND_QQ}
    assert [row["id"] for row in snapshot["candidates"]["users"]] == [OWNER_QQ, FRIEND_QQ]

    assert restarted.identity_links.unlink("user", OPENID_PRIVATE) is True
    assert restarted.identity_links.unlink("user", OPENID_PRIVATE) is False
    assert restarted.permission_mgr.get_permission_level(OPENID_PRIVATE) == "none"
    assert restarted.memory_bridge.participant_subject(OPENID_PRIVATE)["subject_id"] == f"qq:{OPENID_PRIVATE}"


def test_an_already_linked_id_cannot_be_pointed_somewhere_else(tmp_path):
    plugin = _plugin(tmp_path)
    links = plugin.identity_links
    _say(plugin, f"认领 {links.issue_code('user', FRIEND_QQ)['code']}")
    second = links.issue_code("user", OWNER_QQ)["code"]
    assert links.claim(second, sender_id=OPENID_PRIVATE)["status"] == "already"
    assert links.user(OPENID_PRIVATE) == FRIEND_QQ
    assert links.claim(second, sender_id=OPENID_IN_GROUP)["status"] == "linked", "没成功的那次不消耗码"


# ── 接线：入口、派发、界面 ──────────────────────────────────────────

def test_the_trust_entry_issues_codes_lists_and_unlinks(tmp_path):
    from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin

    plugin = _plugin(tmp_path)
    def call(action, **kw):
        return QQAutoReplyPlugin._trust_identity_links(plugin, action, kw)

    issued = call("link_code", qq_number=FRIEND_QQ).value
    assert issued["legacy_id"] == FRIEND_QQ and len(issued["code"]) == 8
    assert call("link_code", qq_number="99999").is_err(), "不在名单里的号不发码"
    group_code = call("link_code", group_id=GROUP_QQ).value
    assert group_code["kind"] == "group"

    _say(plugin, f"认领 {issued['code']}")
    listed = call("links").value
    assert listed["users"] == {OPENID_PRIVATE: FRIEND_QQ}
    assert "code" not in json.dumps(listed), "列表里不带认领码"
    assert call("unlink", user_id=OPENID_PRIVATE).value["users"] == {}
    assert call("unlink", user_id=OPENID_PRIVATE).is_err()


def test_the_dispatcher_handles_claims_after_the_blacklist_and_before_everything_else():
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "message_dispatcher.py").read_text(encoding="utf-8")
    body = source[source.index("    async def handle_message("):]
    blacklist = body.index("self._is_blacklisted_user(blacklist_sender)")
    claim = body.index("await links.handle_message(message)")
    poke = body.index('message.get("notice_type") == "poke"')
    assert blacklist < claim < poke, "黑名单用户不能认领；认领指令不进对话、不进群消息汇总"


def test_the_console_has_the_claim_card_wired():
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "static" / "open_platform.html").read_text(encoding="utf-8")
    assert 'id="identity-links-card"' in html and 'id="link-table"' in html and 'id="link-code-box"' in html
    for name in ("loadIdentityLinks", "renderIdentityLinks", "doLinkCode", "doUnlink"):
        assert re.search(rf"function {name}\(", html), f"控制台缺 {name}()"
    assert "if(sub==='accounts'){loadIdentityLinks();" in html, "进「用户」页时要加载认领记录"
    for action in ("action:'links'", "action:'link_code'", "action:'unlink'"):
        assert action in html
    keys = set(re.findall(r"'(ui\.openplat\.links\.[a-z_]+)'", html)) | set(
        re.findall(r'data-i18n="(ui\.openplat\.links\.[a-z_]+)"', html))
    assert len(keys) >= 15
    for name in ("zh-CN.json", "en.json"):
        bundle = json.loads((root / "i18n" / name).read_text(encoding="utf-8"))
        assert not sorted(key for key in keys if not bundle.get(key)), f"{name} 缺认领相关文案"

