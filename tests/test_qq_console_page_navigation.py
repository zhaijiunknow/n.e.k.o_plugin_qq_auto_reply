# -*- coding: utf-8 -*-
"""看门狗：两个控制台页必须有「返回首页」入口，且缓存版本号各处一致。

为什么值得钉：

1. 用户明确要求这两个页面能回到 `index.html`（接入方式选择页）。它们是**被跳转进来的**，
   页内导航（侧栏）只切页内分节，没有回上层的出口 —— 只能靠浏览器后退。
2. `index.html` 里写着「改了这几个页面记得把 ?v= +1」（击穿强缓存，之前缓存 1 小时导致
   改了代码仍看到旧页）。版本号散落在 index.html 与两页互相跳转的链接里，**只提一处**
   就会让用户拿到「新页面 + 旧缓存」的混合版本，而且现象是"改了没生效"，很难查。
"""

from __future__ import annotations

import pathlib
import re

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
PAGES = ("static/napcat.html", "static/open_platform.html")
VERSION_SOURCES = ("static/index.html", "static/napcat.html", "static/open_platform.html")


def test_both_console_pages_have_a_back_to_index_control():
    for rel in PAGES:
        html = (PLUGIN_DIR / rel).read_text(encoding="utf-8")
        assert 'href="index.html"' in html, f"{rel} 没有返回 index.html 的链接"
        assert 'id="btn-back-index"' in html, f"{rel} 的返回入口缺少稳定 id（前端/测试都靠它定位）"
        assert "ui.shared.topbar.back_index" in html, f"{rel} 的返回入口没接 i18n"


def test_back_index_label_exists_in_both_bundles():
    import json

    for name in ("zh-CN.json", "en.json"):
        data = json.loads((PLUGIN_DIR / "i18n" / name).read_text(encoding="utf-8"))
        assert data.get("ui.shared.topbar.back_index"), f"{name} 缺 ui.shared.topbar.back_index"


def test_cache_buster_versions_agree_across_pages():
    """所有引用处对同一个页面必须用同一个 ?v= 版本号。"""
    seen: dict[str, set[str]] = {}
    for rel in VERSION_SOURCES:
        text = (PLUGIN_DIR / rel).read_text(encoding="utf-8")
        for target, version in re.findall(r"(napcat|open_platform)\.html\?v=(\d+)", text):
            seen.setdefault(target, set()).add(version)
    assert seen, "一个 ?v= 版本号都没扫到 —— 正则失效或版本号被去掉"
    for target, versions in seen.items():
        assert len(versions) == 1, (
            f"{target}.html 的 ?v= 不一致：{sorted(versions)}；"
            "只提一处会让用户拿到新页面配旧缓存的混合版本"
        )
