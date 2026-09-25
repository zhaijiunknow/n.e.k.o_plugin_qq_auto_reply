"""status.html 的刷新必须在**任何滚动位置**都够得着。

**为什么要有这条**：`status.html` 有 6 张卡片，而两个刷新按钮原本只在顶部的
`<header>` 里。滑到底部想刷新，得先一路滚回顶部 —— 使用者原话是"很反人类"。

修法是加一个 `position:fixed` 的浮动刷新按钮，滚过顶部那排按钮之后才出现
（顶部的按钮本来就看得见，不该同屏出现两个）。

这里钉住的是**结构**，不是像素：一旦有人把浮动按钮删掉、把它挪进滚动内容里、
或者忘了给 `.wrap` 留底部空间（那样它会盖住最后一张卡片），这条就会红。
真正的交互行为（滚动后出现、点击真的调 load）由 `.dsh-artifacts/verify-fab.py`
用 Playwright 验，那个需要浏览器，不适合放进单测。
"""

from __future__ import annotations

import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parents[1]
STATUS = (BASE / "static" / "status.html").read_text(encoding="utf-8")

#: 浮动按钮的样式块。取 `#btn-refresh-fab {` 到它自己的 `}` 之间。
_FAB_RULE = re.search(r"#btn-refresh-fab\s*\{(.*?)\}", STATUS, re.S)
_SHOW_RULE = re.search(r"#btn-refresh-fab\.show\s*\{(.*?)\}", STATUS, re.S)
_FAB_TAG = re.search(r"<button id=\"btn-refresh-fab\"[^>]*>(.*?)</button>", STATUS, re.S)


def test_the_floating_button_exists():
    assert _FAB_TAG, "status.html 里找不到 <button id=\"btn-refresh-fab\">"
    assert _FAB_RULE, "找不到 #btn-refresh-fab 的样式规则"


def test_it_is_fixed_so_scrolling_never_hides_it():
    """`fixed` 是关键：换成 `absolute` 它会跟着内容滚走，问题原样存在。"""
    body = _FAB_RULE.group(1)
    assert re.search(r"position\s*:\s*fixed", body), (
        "浮动刷新按钮必须是 position:fixed，否则滚动时会跟着内容跑掉"
    )


def test_it_starts_hidden_and_is_revealed_by_a_show_class():
    """页顶已经有刷新按钮了，两个同时出现会让人不知道该点哪个。"""
    assert re.search(r"display\s*:\s*none", _FAB_RULE.group(1)), (
        "默认应当 display:none（顶部那个按钮本来就看得见）"
    )
    assert _SHOW_RULE, "找不到 #btn-refresh-fab.show 规则 —— 没有它按钮永远不会出现"
    assert re.search(r"display\s*:\s*(inline-)?flex", _SHOW_RULE.group(1)), (
        ".show 应当把按钮显示出来"
    )


def test_the_reveal_is_driven_by_scroll():
    """必须有 scroll 监听，而且用的是同一个 load()，不能是另一套刷新逻辑。"""
    assert re.search(r"addEventListener\(\s*'scroll'", STATUS), (
        "没有 scroll 监听 —— 按钮不会随滚动出现"
    )
    m = re.search(r"getElementById\('btn-refresh-fab'\)", STATUS)
    assert m, "脚本里没有取 #btn-refresh-fab"
    # 按钮的 click 必须接同一个 load
    assert re.search(
        r"btn-refresh-fab'\);?\s*\n?\s*if \(!fab\) return;\s*\n\s*fab\.addEventListener\('click',\s*load\)",
        STATUS,
    ), "浮动按钮的 click 没有接到 load —— 会变成一个死按钮"


def test_it_does_not_cover_the_last_card():
    """`.wrap` 要留出底部空间，否则浮动按钮会压在最后一张卡片的操作区上。"""
    m = re.search(r"\.wrap\s*\{([^}]*)\}", STATUS)
    assert m, "找不到 .wrap 规则"
    pad = re.search(r"padding-bottom\s*:\s*(\d+)px", m.group(1))
    assert pad, ".wrap 没有 padding-bottom —— 浮动按钮会盖住最后一张卡片"
    assert int(pad.group(1)) >= 60, (
        f".wrap 的 padding-bottom 只有 {pad.group(1)}px，"
        "浮动按钮（约 37px 高 + 20px 边距）会压住内容"
    )


def test_it_reuses_the_existing_i18n_key():
    """不新增 i18n 条目：文字直接复用顶部的 ui.status.refresh。"""
    inner = _FAB_TAG.group(1)
    assert 'data-i18n="ui.status.refresh"' in inner, (
        "浮动按钮的文字应当复用已有的 ui.status.refresh 键，"
        "新增键会让 i18n 覆盖率看门狗（test_qq_ui_i18n_coverage）要求补全所有语言"
    )


def test_the_icon_is_inline_not_an_external_file():
    """插件要离线可用，图标用内联 SVG，不引外部资源。"""
    inner = _FAB_TAG.group(1)
    assert "<svg" in inner, "浮动按钮应当带一个内联 SVG 图标"
    assert "src=" not in inner, "不要给图标引外部文件（插件要离线可用）"
