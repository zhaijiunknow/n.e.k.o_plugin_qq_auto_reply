"""表情包删除按钮：三个页面都必须在列表里给出删除入口，而且接的是同一个后端动作。

**为什么要有这条**：`asset` 入口的 schema 是 `additionalProperties: False` ——
前端把参数名写错（比如 `sticker_id` 而不是 `id`）、或者忘了带 `id`，**调用会被参数
校验直接拦掉**，界面上表现为"点了没反应"。所以这里把"发出去的参数名"也钉住。

后端那边删东西是真的删磁盘文件，`test_qq_sticker_delete.py` 负责行为；
这条只管"界面上有没有入口、接得对不对"。
"""

from __future__ import annotations

import re

from _ui_source import code_of, fn_body, read

PAGES = ("napcat.html", "open_platform.html", "status.html")


def test_code_of_does_not_swallow_markup():
    """先钉住工具本身：`accept="image/*"` 会被误当成注释开头。

    naive 的实现会把它和后面很远的 `*/` 配成一对、静默吞掉几 KB ——
    `status.html` 实测被吞 5.2 KB，其中正好有 `id="sticker-list"`。
    换个断言方向，这种吞法会让测试"看着通过、其实没查"。
    """
    text = code_of("status.html")
    assert 'id="sticker-list"' in text, "剥注释把 markup 吞掉了 —— 检查用的工具本身有 bug"
    assert 'id="sk-file"' in text, "剥注释把 markup 吞掉了"
    assert 'accept="image/*"' in text, "挡位符没有还原回去"
    # 注释确实被剥掉了（否则这条工具就没意义）
    assert "落区/输入框的样式与 napcat" not in text, "注释没有被剥掉"


def test_the_body_extractor_actually_works():
    """先证明提取器在干活 —— 失配会让下面几条空过。"""
    text = "function foo(a){ if(a){ return 1 } return 2 }"
    assert fn_body(text, "foo") == " if(a){ return 1 } return 2 "
    assert fn_body(text, "nope") is None


def test_every_sticker_list_has_a_delete_button():
    for name in PAGES:
        text = read(name)
        assert re.search(r'data-i18n="ui\.shared\.btn\.delete"|ui\.shared\.btn\.delete', text), (
            f"{name}: 表情包列表里没有「删除」按钮"
        )


def test_the_delete_button_is_wired_to_deleteSticker():
    for name in PAGES:
        text = code_of(name)
        assert re.search(r"deleteSticker\(", text), f"{name}: 没有 deleteSticker 调用点"
        assert fn_body(text, "deleteSticker") is not None, f"{name}: 没有定义 deleteSticker()"


def test_delete_sends_the_backend_contract():
    """后端要 `action=delete_sticker` 和 `id`；`additionalProperties:False` 会拦掉别的写法。

    注意这些文件是压缩成行的，`action:'delete_sticker'` 冒号后面**没有空格**，
    所以这里用容忍空白的正则（写死带空格的字面量会假红 —— 我第一版就栽在这）。
    """
    for name in PAGES:
        body = fn_body(code_of(name), "deleteSticker")
        assert body is not None, f"{name}: 定位不到 deleteSticker 的函数体"
        assert re.search(r"action\s*:\s*['\"]delete_sticker['\"]", body), (
            f"{name}: 没有传 action: 'delete_sticker'"
        )
        assert re.search(r"\bid\s*:", body), (
            f"{name}: 没有传 id —— schema 的 additionalProperties:False 会把这个调用判为非法"
        )


def test_delete_asks_for_confirmation_first():
    """后端连图片文件一起删、不可恢复 —— 本仓库其它破坏性操作（清群记忆、恢复默认
    提示词）都先 confirm，这里也一样。"""
    for name in PAGES:
        body = fn_body(code_of(name), "deleteSticker")
        assert body is not None, f"{name}: 定位不到 deleteSticker 的函数体"
        confirm_at = body.find("confirm(")
        call_at = body.find("call(")
        assert confirm_at != -1, f"{name}: 删除前没有 confirm，一点就真删了"
        assert call_at == -1 or confirm_at < call_at, (
            f"{name}: confirm 出现在调用之后 —— 等于没拦"
        )


def test_the_delete_button_refreshes_the_list_afterwards():
    """删完不刷新，被删的那行还挂在界面上，看着像没删掉。"""
    for name in PAGES:
        body = fn_body(code_of(name), "deleteSticker")
        assert "loadStickers(" in (body or ""), f"{name}: 删除后没有刷新列表"


def test_status_page_has_the_list_and_its_refresh_button():
    text = code_of("status.html")
    assert 'id="sticker-list"' in text, "status.html 没有已注册表情包的列表容器"
    assert 'id="btn-refresh-stickers"' in text, "status.html 没有刷新列表按钮"
    assert re.search(r"getElementById\('btn-refresh-stickers'\)\.addEventListener", text), (
        "status.html 的刷新列表按钮没有接线"
    )
    assert "sticker-table" not in text, (
        "status.html 不该去动别的页面才有的 #sticker-table"
    )
