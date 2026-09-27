# -*- coding: utf-8 -*-
"""**本分支专用**（`promo-no-napcat-ui`）：NapCat 前端入口与「一键部署」必须保持隐藏。

为什么要有这个文件：这条分支存在的唯一理由就是"录宣传视频时前端不出现 NapCat 与一键部署"，
而它的维护方式必然是 `git merge main`（main 每周都在长）。**合并会把隐藏掉的卡片原样带回来**
—— 那时不会报错、不会有人发现，直到视频里出现 NapCat 页为止。所以把"隐藏"钉成断言：
合并后一旦哪张卡回来了，这个文件立刻红。

三条边界（刻意如此，别扩大）：

1. **只隐藏入口，不删页面**：`napcat.html` 仍然随包发布，直接输 URL 照样能打开 ——
   使用者原话是「在前端隐藏napcat页的入口和一键部署即可」。
2. **只隐藏不删除 DOM**：`status.html` 的部署卡片用 `display:none` 藏起来，
   因为页内 JS 会无条件 `getElementById('btn-deploy'/'btn-apply')` 绑事件；
   把元素删掉会让整页开页即抛、一片空白（这条比"看不见"重要得多）。
3. **后端能力不动**：`deploy` 入口、NapCat 安装/配置逻辑全部保留 ——
   视频录制用的是已经配好的环境，不需要前端按钮。

main 上没有这个文件；把本分支合回 main 或从 main 合并更新时，先看 docs/PROMO-NO-NAPCAT.md。
"""

from __future__ import annotations

import pathlib
import re

PLUGIN = pathlib.Path(__file__).resolve().parents[1]

INDEX = (PLUGIN / "static" / "index.html").read_text(encoding="utf-8")
STATUS = (PLUGIN / "static" / "status.html").read_text(encoding="utf-8")
OPENPLAT = (PLUGIN / "static" / "open_platform.html").read_text(encoding="utf-8")


# ── 1. 首页：NapCat 与「一键部署」两张卡都不许出现 ────────────────────

def test_index_has_no_napcat_entry():
    assert 'data-nav="napcat.html"' not in INDEX, (
        "首页又出现了 NapCat 入口 —— main 合并时把那张卡带回来了（本分支要求隐藏）"
    )
    assert 'href="napcat.html"' not in INDEX, "首页的 NapCat 卡片链接回来了"


def test_index_has_no_one_click_deploy_entry():
    """首页那张卡直接写着「一键部署」（跳 status.html）—— 录视频不能出现这行字。"""
    assert 'data-nav="status.html"' not in INDEX, (
        "首页又出现了跳 status.html 的卡（它就叫「一键部署」）"
    )
    assert "ui.index.status.title" not in INDEX, "首页那行「一键部署」文案回来了"


def test_index_still_keeps_the_safe_entries():
    """别把入口删光了：QQ 开放平台与旧版控制中心必须还在（否则视频里没东西可点）。"""
    assert 'data-nav="open_platform.html"' in INDEX, "开放平台入口被一起删了"
    assert 'data-nav="old.html"' in INDEX, "旧版控制中心入口被一起删了"


# ── 2. 开放平台页顶部：不出现「切换到 NapCat」 ───────────────────────

def test_open_platform_has_no_napcat_switch():
    assert 'data-nav="napcat.html"' not in OPENPLAT, "开放平台页顶部又出现了「切换到 NapCat」"
    assert "ui.openplat.topbar.switch" not in OPENPLAT, "那句切换文案的 i18n 键又挂回去了"


# ── 3. status.html：「一键部署」卡片隐藏但**必须留在 DOM 里** ────────

def _deploy_card() -> str:
    m = re.search(r'<div class="card"([^>]*)>\s*<h2 data-i18n="ui\.status\.deploy"', STATUS)
    assert m, "找不到「一键部署」卡片 —— 元素被删了？见本文件开头第 2 条边界"
    return m.group(1)


def test_the_deploy_card_is_hidden():
    assert "display:none" in _deploy_card().replace(" ", ""), (
        "「一键部署」卡片没有 display:none —— 视频里会拍到它"
    )


def test_the_deploy_buttons_are_still_in_the_dom():
    """隐藏 ≠ 删除：JS 无条件绑这两个按钮，删了整页会开页即抛。"""
    assert 'id="btn-deploy"' in STATUS, "btn-deploy 被删了 —— status.html 会开页空白"
    assert 'id="btn-apply"' in STATUS, "btn-apply 被删了 —— status.html 会开页空白"
    # 绑定点仍在（数量：id 属性 1 处 + getElementById 1 处）
    assert STATUS.count("btn-deploy") >= 3, "btn-deploy 的绑定点少了"
    assert STATUS.count("btn-apply") >= 3, "btn-apply 的绑定点少了"


def test_status_page_itself_still_works():
    """这页剩下的东西（连接方式/开机自启/二维码/信任名单/表情包）不许被动过。"""
    for key in ("ui.status.mode", "ui.status.autostart", "ui.qrcode.title",
                "ui.status.lists", "ui.shared.card.sticker_upload"):
        assert key in STATUS, f"{key} 不见了 —— 隐藏一键部署时误删了别的东西"


# ── 4. 边界：页面本身仍然随包发布（只隐藏入口） ──────────────────────

def test_the_napcat_page_itself_still_ships():
    """使用者口径是「隐藏入口」：直接输 URL 仍应打得到这页（也留着以后恢复）。"""
    page = PLUGIN / "static" / "napcat.html"
    assert page.is_file(), "napcat.html 被删了 —— 本分支只隐藏入口，不删页面"
    assert page.stat().st_size > 1000, "napcat.html 变成空文件了"
