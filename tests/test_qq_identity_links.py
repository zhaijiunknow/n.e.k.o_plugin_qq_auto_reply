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

    #: 发送的返回值。开放平台的发送失败不抛异常，返回 None。
    result = "m1"

    async def send_message(self, user_id, text):
        raise AssertionError("不带回复锚点的私聊发送：在开放平台上算主动消息，可能发不出去")

    async def _segments(self, target, segments):
        self.sent.append((str(target), "".join(
            f"[reply:{s['data']['id']}]" if s["type"] == "reply" else s["data"]["text"] for s in segments)))
        return self.result

    async def send_private_message_segments(self, user_id, segments, **kw):
        return await self._segments(user_id, segments)

    async def send_group_message_segments(self, group_id, segments, **kw):
        return await self._segments(group_id, segments)


def _plugin(tmp_path):
    plugin = SimpleNamespace(
        data_path=lambda name="": str(tmp_path / name),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda message="", *a, **k: warnings.append(str(message))),
        qq_client=_Client(),
        _refresh_admin_qq=lambda: None,
    )
    plugin.warnings = warnings = []
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


def test_an_id_with_its_own_entry_at_another_level_cannot_be_linked(tmp_path):
    """自己在名单里是一个级别、认领的旧账号是另一个级别：权限按自己的条目、记忆却落到旧账号，

    两边会分叉（比如普通用户认到管理员头上，读到的是管理员的记忆）。认领和直接匹配都拒绝，
    而且不消耗认领码。
    """
    plugin = _plugin(tmp_path)
    links = plugin.identity_links
    plugin.permission_mgr.add_user(OPENID_PRIVATE, "normal")
    plugin.group_permission_mgr.add_group(GROUP_OPENID, "normal")

    code = links.issue_code("user", OWNER_QQ)["code"]
    assert _say(plugin, f"认领 {code}") is True
    assert "名单" in plugin.qq_client.sent[-1][1]
    assert links.user(OPENID_PRIVATE) == OPENID_PRIVATE
    assert plugin.memory_bridge.speaker_account_id(OPENID_PRIVATE) == f"qq:{OPENID_PRIVATE}"
    assert links.claim(code, sender_id=OPENID_IN_GROUP)["status"] == "linked", "被拒的那次不消耗码"

    with pytest.raises(ValueError):
        links.link("user", OPENID_PRIVATE, OWNER_QQ)
    with pytest.raises(ValueError):
        links.link("group", GROUP_OPENID, GROUP_QQ)          # 名单里 GROUP_QQ 是 trusted


def test_an_id_with_its_own_entry_at_the_same_level_can_be_linked(tmp_path):
    """级别一致就不算分叉 —— 管理员自动匹配正是这种情况（两边都是 admin）。"""
    plugin = _plugin(tmp_path)
    plugin.permission_mgr.add_user(OPENID_PRIVATE, "trusted")
    _say(plugin, f"认领 {plugin.identity_links.issue_code('user', FRIEND_QQ)['code']}")
    assert plugin.identity_links.user(OPENID_PRIVATE) == FRIEND_QQ
    assert plugin.permission_mgr.get_permission_level(OPENID_PRIVATE) == "trusted"


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
        return QQAutoReplyPlugin._identity_link_dispatch(plugin, action, kw)

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
    assert 'id="seen-table"' in html and "action:'link'," in html, "控制台缺「直接匹配」"
    for name in ("loadIdentityLinks", "renderIdentityLinks", "doLinkCode", "doUnlink", "renderSeenIds", "doLinkDirect"):
        assert re.search(rf"function {name}\(", html), f"控制台缺 {name}()"
    assert "if(sub==='accounts'){loadIdentityLinks();" in html, "进「用户」页时要加载认领记录"
    for action in ("action:'links'", "action:'link_code'", "action:'unlink'"):
        assert action in html
    assert html.count("call('identity_link',") == 4, "认领的四个调用都走对 agent 隐藏的 identity_link 入口"
    assert not re.search(r"call\('trust',[^)]*action:'(?:links?|link_code|unlink)'", html)
    keys = set(re.findall(r"'(ui\.openplat\.links\.[a-z_]+)'", html)) | set(
        re.findall(r'data-i18n="(ui\.openplat\.links\.[a-z_]+)"', html))
    assert len(keys) >= 15
    for name in ("zh-CN.json", "en.json"):
        bundle = json.loads((root / "i18n" / name).read_text(encoding="utf-8"))
        assert not sorted(key for key in keys if not bundle.get(key)), f"{name} 缺认领相关文案"


# ── 不用码：管理员自动匹配、控制台直接匹配 ──────────────────────────

def _rebuild(plugin, users, groups=()):
    QQSettingsService(plugin).rebuild_permission_managers({"trusted_users": list(users), "trusted_groups": list(groups)})
    plugin.memory_bridge = QQMemoryBridge(plugin)


def test_the_scan_bound_admin_is_matched_to_the_old_admin_automatically(tmp_path):
    """扫码绑定把扫码者的 openid 登记成管理员；名单里只有一个 QQ 管理员时，两者就是同一个人。"""
    plugin = _plugin(tmp_path)
    _rebuild(plugin, [{"qq": OWNER_QQ, "level": "admin"}, {"qq": OPENID_PRIVATE, "level": "admin"},
                      {"qq": FRIEND_QQ, "level": "trusted"}])
    assert plugin.identity_links.user(OPENID_PRIVATE) == OWNER_QQ
    assert plugin.memory_bridge.speaker_account_id(OPENID_PRIVATE) == f"qq:{OWNER_QQ}"
    assert plugin.identity_links.user(FRIEND_QQ) == FRIEND_QQ
    assert json.loads((tmp_path / QQIdentityLinks.FILENAME).read_text(encoding="utf-8"))["users"] == {
        OPENID_PRIVATE: OWNER_QQ}


@pytest.mark.parametrize("users", [
    [{"qq": OWNER_QQ, "level": "admin"}, {"qq": "10009", "level": "admin"}, {"qq": OPENID_PRIVATE, "level": "admin"}],
    [{"qq": OPENID_PRIVATE, "level": "admin"}, {"qq": FRIEND_QQ, "level": "trusted"}],
    [{"qq": OWNER_QQ, "level": "admin"}, {"qq": OPENID_PRIVATE, "level": "trusted"}],
])
def test_the_automatic_match_never_guesses(tmp_path, users):
    """两个 QQ 管理员、没有 QQ 管理员、或那个 openid 不是管理员：都不自动匹配。"""
    plugin = _plugin(tmp_path)
    _rebuild(plugin, users)
    assert plugin.identity_links.user(OPENID_PRIVATE) == OPENID_PRIVATE


def test_an_undone_automatic_match_stays_undone(tmp_path):
    plugin = _plugin(tmp_path)
    users = [{"qq": OWNER_QQ, "level": "admin"}, {"qq": OPENID_PRIVATE, "level": "admin"}]
    _rebuild(plugin, users)
    assert plugin.identity_links.unlink("user", OPENID_PRIVATE) is True
    _rebuild(plugin, users)
    restarted = _plugin(tmp_path)
    _rebuild(restarted, users)
    assert plugin.identity_links.user(OPENID_PRIVATE) == restarted.identity_links.user(OPENID_PRIVATE) == OPENID_PRIVATE


def test_the_admin_can_match_a_seen_id_directly(tmp_path):
    plugin = _plugin(tmp_path)
    links = plugin.identity_links
    assert _say(plugin, "大家好呀", sender=OPENID_IN_GROUP, group=GROUP_OPENID, at=False) is False
    seen = links.snapshot()["seen"]
    assert {(row["kind"], row["id"]) for row in seen} == {("user", OPENID_IN_GROUP), ("group", GROUP_OPENID)}
    assert seen[0]["text"] == "大家好呀" and seen[0]["group"] == GROUP_OPENID

    links.link("group", GROUP_OPENID, GROUP_QQ)
    links.link("user", OPENID_IN_GROUP, OWNER_QQ)
    assert plugin.group_permission_mgr.get_group_level(GROUP_OPENID) == "trusted"
    assert plugin.permission_mgr.get_permission_level(OPENID_IN_GROUP) == "admin"
    assert links.snapshot()["seen"] == [], "匹配过的不再列在待匹配里"
    assert "大家好呀" not in (tmp_path / QQIdentityLinks.FILENAME).read_text(encoding="utf-8"), "预览不落盘"

    for kind, linked, legacy in (("user", OPENID_PRIVATE, "99999"), ("user", OPENID_IN_GROUP, FRIEND_QQ),
                                 ("user", FRIEND_QQ, FRIEND_QQ), ("group", "", GROUP_QQ),
                                 ("user", OPENID_PRIVATE, OPENID_IN_GROUP)):
        with pytest.raises(ValueError):
            links.link(kind, linked, legacy)


def test_the_trust_entry_links_directly(tmp_path):
    from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin

    plugin = _plugin(tmp_path)

    def call(**kw):
        return QQAutoReplyPlugin._identity_link_dispatch(plugin, "link", kw)

    assert call(user_id=OPENID_PRIVATE, target_user_id=FRIEND_QQ).value["users"] == {OPENID_PRIVATE: FRIEND_QQ}
    assert call(group_id=GROUP_OPENID, target_group_id=GROUP_QQ).value["groups"] == {GROUP_OPENID: GROUP_QQ}
    assert call(user_id=OPENID_IN_GROUP, target_user_id="99999").is_err()
    assert call(group_id=GROUP_OPENID).is_err()


# ── 审查报告 A621 ───────────────────────────────────────────────────

def test_the_private_reply_is_anchored_to_the_incoming_message(tmp_path):
    """私聊的认领结果要带回复锚点：开放平台上没有它就是主动消息，有额度，可能发不出去。"""
    plugin = _plugin(tmp_path)
    assert _say(plugin, f"认领 {plugin.identity_links.issue_code('user', FRIEND_QQ)['code']}") is True
    target, text = plugin.qq_client.sent[0]
    assert target == OPENID_PRIVATE and text.startswith("[reply:in-1]认领成功")


def test_an_unconfirmed_reply_leaves_a_warning(tmp_path):
    """适配器发送失败不抛异常、只返回 None：不能让"认领了却没人告诉他"悄悄过去。"""
    plugin = _plugin(tmp_path)
    plugin.qq_client.result = None
    _say(plugin, f"认领 {plugin.identity_links.issue_code('user', FRIEND_QQ)['code']}")
    assert plugin.identity_links.user(OPENID_PRIVATE) == FRIEND_QQ, "认领本身照常生效"
    assert any("[Identity]" in line and "没发出去" in line for line in plugin.warnings)


def test_wrong_codes_from_one_sender_stop_getting_replies(tmp_path):
    """连着猜错的人不再得到回复，也不再被受理 —— 不拿机器人当回声筒。别人不受影响。"""
    plugin = _plugin(tmp_path)
    links = plugin.identity_links
    code = links.issue_code("user", FRIEND_QQ)["code"]
    limit = links.MAX_FAILED_ATTEMPTS

    for _ in range(limit + 4):
        assert _say(plugin, "认领 ZZZZZZZZ", sender=OPENID_IN_GROUP) is True, "仍然不进对话"
    assert len(plugin.qq_client.sent) == limit
    assert _say(plugin, f"认领 {code}", sender=OPENID_IN_GROUP) is True
    assert links.user(OPENID_IN_GROUP) == OPENID_IN_GROUP and len(plugin.qq_client.sent) == limit

    assert _say(plugin, f"认领 {code}") is True              # 另一个人
    assert links.user(OPENID_PRIVATE) == FRIEND_QQ


def test_noting_a_seen_id_does_not_rebuild_the_roster(tmp_path):
    """每条消息都会走到这里：查名单用单次查表，不为此把整份名单重建一遍。"""
    plugin = _plugin(tmp_path)

    def _boom():
        raise AssertionError("note_seen 不该在热路径上重建名单列表")

    plugin.permission_mgr.list_users = _boom
    plugin.group_permission_mgr.list_groups = _boom
    assert _say(plugin, "路过", sender=OPENID_IN_GROUP, group=GROUP_OPENID, at=False) is False
    assert (("user", OPENID_IN_GROUP) in plugin.identity_links._seen
            and ("group", GROUP_OPENID) in plugin.identity_links._seen)


def test_claim_actions_live_on_an_agent_hidden_entry():
    """能把任意 ID 匹配到管理员旧账号的动作，不放在模型能调的入口上。"""
    from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
    from plugin.sdk.shared.core.decorators import EVENT_META_ATTR

    meta = getattr(QQAutoReplyPlugin.identity_link, EVENT_META_ATTR)
    assert (meta.metadata or {}).get("agent_auto") is False
    assert set(meta.input_schema["properties"]["action"]["enum"]) == {"links", "link", "link_code", "unlink"}
    trust = getattr(QQAutoReplyPlugin.trust, EVENT_META_ATTR)
    assert not {"links", "link", "link_code", "unlink"} & set(trust.input_schema["properties"]["action"]["enum"])
    assert "target_group_id" not in trust.input_schema["properties"]
