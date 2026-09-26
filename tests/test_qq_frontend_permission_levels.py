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
