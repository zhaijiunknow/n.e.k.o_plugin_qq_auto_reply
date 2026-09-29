# -*- coding: utf-8 -*-
"""看门狗：前端不得再提供已删除的权限级别。

`open` 级在权限收敛那轮被删除（原本是「按概率直接回复」，语义并入 trusted）。
后端有别名把残留的 "open" 归一成 trusted，所以**后端不会报错** —— 这正是危险之处：
前端还列着这个选项的话，用户选了它界面也不报错，但语义已经不是他以为的那个。
真机就是踩在这个盲区上：`status.html` 与 `napcat.html` 清了，
`open_platform.html` 与 `script.js` 漏了。
"""

from __future__ import annotations

import pathlib

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]

#: 前端文件里**不允许**再出现的两种写法（都是"提供 open 级别"的历史形态）。
FORBIDDEN = ('value="open"', "'open', 'open'")

FRONTENDS = ("static/napcat.html", "static/open_platform.html", "static/status.html", "static/script.js")


def test_frontend_does_not_offer_the_removed_open_level():
    offenders: list[str] = []
    for rel in FRONTENDS:
        p = PLUGIN_DIR / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{rel}: {needle}")
    assert not offenders, "前端还在提供已删除的权限级别 open：\n" + "\n".join(offenders)


def test_backend_still_normalizes_legacy_open_level():
    """后端保留别名（老配置不用改），所以这条看门狗不能顺手把别名也删了。"""
    from plugin.plugins.qq_auto_reply.group_permission import GroupPermissionManager

    assert GroupPermissionManager._normalize_level("open") == "trusted"
    assert GroupPermissionManager._normalize_level("truth") == "trusted"


# ── 群默认档：名单里的群＝信任群 ──────────────────────────────────
#
# `normal` 的语义是「没被 @ 时就只按 low 概率转发给主人」，接近不说话。把它当
# 新增群聊的默认档，用户「加了群」却看不到她开口 —— 真机踩过这个静默默认。
# 显式选 `normal` 的入口必须留着，只是不能再是默认档。

def _select_snippet(text: str, select_id: str) -> str:
    start = text.index(f'id="{select_id}"')
    return text[start:text.index("</select>", start)]


def test_group_modal_defaults_to_trusted_in_napcat_panel():
    napcat = (PLUGIN_DIR / "static/napcat.html").read_text(encoding="utf-8")
    assert "showGroupModal('','trusted',null,null)" in napcat, "新增群聊的默认档还是普通群"
    assert "showGroupModal('','normal'" not in napcat
    assert "(!level||level==='trusted')" in napcat, "群弹窗的选中回退没跟着默认档走"
    assert 'value="normal"' in napcat, "显式选择普通群的入口不能删"


def test_group_modal_defaults_to_trusted_in_open_platform_panel():
    op = (PLUGIN_DIR / "static/open_platform.html").read_text(encoding="utf-8")
    snippet = _select_snippet(op, "mg-level")
    trusted_at = snippet.index('value="trusted"')
    normal_at = snippet.index('value="normal"')
    assert "selected" in snippet[trusted_at:normal_at], "开放平台的群弹窗默认还是普通群"
    assert "selected" not in snippet[normal_at:], "普通群又被写成默认档"


def test_other_group_entry_points_keep_trusted_first():
    """status.html / script.js 的群级别选项本来就是 trusted 打头，代码取第一项当默认。"""
    snippet = _select_snippet((PLUGIN_DIR / "static/status.html").read_text(encoding="utf-8"), "g-level")
    assert snippet.index('value="trusted"') < snippet.index('value="normal"')
    assert "selected" not in snippet, "selected 会盖掉「第一项即默认」这个约定"
    script = (PLUGIN_DIR / "static/script.js").read_text(encoding="utf-8")
    assert ": [['trusted', 'trusted'], ['normal', 'normal']]" in script, "群级别选项列表被改了"
    assert "levelSelect.value = String(item?.level || options[0][0])" in script, "新条目的默认档不再取第一项"
