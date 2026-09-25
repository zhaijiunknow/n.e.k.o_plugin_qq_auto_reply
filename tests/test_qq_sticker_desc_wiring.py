"""表情包描述的接线看门狗：**三个页面**上，用户写的描述都必须真的传到后端。

**为什么要有这条**：这轮实测发现 `open_platform.html` 的表情包页有两个缺陷叠加，
结果是**那页注册的每一张表情包描述都只能是文件名**，而且全程不报错：

1. `skFilePicked()` 没有调用 `skBuildDescList()` —— 于是"点选文件"这条路**永远不会
   渲染出每个文件的描述框**（`skBuildDescList` 在那页里根本没有调用点，"拖入"那条
   是唯一会调到它的地方）；
2. 每个文件的输入框没有从总描述框**播种** `value`，也没有监听 —— 总描述框是死的。

后端 `_asset_upload_sticker` 的 `desc` 是必填的，前端有"用文件名兜底"这一步，
所以这两个缺陷**不会报错**，只会把 `cat-nod` 这种文件名当成描述写进 `sticker.json`。
猫娘之后按描述挑图 —— 描述错了，发出来的表情包就是错的。

同源的 `napcat.html` 没这两个问题（它的 `skFilePicked` 会调 `skBuildDescList`，
输入框也从总描述框播种），所以这是 `open_platform` 那份拷贝自己掉的队。

`.dsh-artifacts/verify-sticker-others.py` 会在浏览器里把 `window.call` 换成桩，
实测"点选 / 拖入"两条路交给后端的 desc 到底是什么 —— 这条静态看门狗负责盯住它别回退。
"""

from __future__ import annotations

import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parents[1]
STATIC = BASE / "static"
PAGES = ("napcat.html", "open_platform.html", "status.html")


def _code(name: str) -> str:
    """去掉块注释 —— 这条检查的是代码，注释里提到函数名不算。"""
    return re.sub(r"/\*.*?\*/", "", (STATIC / name).read_text(encoding="utf-8"), flags=re.S)


def _fn_body(text: str, name: str) -> str | None:
    """取 `function name(...){ ... }` 的函数体（按花括号配平，不靠下一行缩进）。

    **取最后一个定义**：JS 里同名函数声明重复时，后声明的覆盖先声明的。
    `napcat.html` 就真的有两份 `skFilePicked` —— 早的那份（老的单文件版本，
    用的是 `_skPickedFile` 单数）已经被后面那份覆盖，只看第一份会得出错误结论。
    """
    bodies: list[str] = []
    for m in re.finditer(rf"function\s+{name}\s*\([^)]*\)\s*\{{", text):
        depth = 0
        for i in range(m.end() - 1, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(text[m.end():i])
                    break
    return bodies[-1] if bodies else None


def test_the_body_extractor_actually_works():
    """先证明解析器在干活 —— 失配会让下面几条空过。"""
    text = "function foo(a){ if(a){ return 1 } return 2 } tail"
    assert _fn_body(text, "foo") == " if(a){ return 1 } return 2 "
    assert _fn_body(text, "nope") is None


def test_the_body_extractor_takes_the_last_definition():
    """JS 同名函数声明后者胜出 —— napcat 真的有两份 skFilePicked，取错就误判。"""
    text = "function foo(){ return 1 } function foo(){ return 2 }"
    assert _fn_body(text, "foo") == " return 2 "


def test_picking_a_file_rebuilds_the_description_inputs():
    """这条正是 open_platform 掉的那一环：不调 skBuildDescList 就没有描述框。"""
    for name in PAGES:
        body = _fn_body(_code(name), "skFilePicked")
        assert body is not None, f"{name}: 找不到 skFilePicked()"
        assert "skBuildDescList" in body, (
            f"{name}: skFilePicked() 没有调用 skBuildDescList() —— "
            "「点选文件」这条路不会出现任何描述框，描述只能永远是文件名"
        )


def test_the_description_inputs_are_seeded_from_the_shared_box():
    """没有播种/监听，页面上那个总描述框就是死的（用户打了字也不会生效）。"""
    for name in PAGES:
        body = _fn_body(_code(name), "skBuildDescList")
        assert body is not None, f"{name}: 找不到 skBuildDescList()"
        assert "sk-desc" in body, (
            f"{name}: skBuildDescList() 没有引用总描述框 #sk-desc —— "
            "用户在那个框里写的描述会被静默丢弃"
        )
        # 播种（value）或双向监听，二者至少要有其一，否则运行时必丢
        assert re.search(r"\.value\s*=", body), (
            f"{name}: 每个文件的描述框既没有从 #sk-desc 播种、也没有回写 —— 描述必丢"
        )


def test_dropping_files_rebuilds_the_description_inputs():
    """拖入那条路也必须重建描述框（三个页面都要）。"""
    for name in PAGES:
        text = _code(name)
        assert "skBuildDescList" in text, f"{name}: 找不到 skBuildDescList"
        # 拖入处理里（drop 监听附近）应当出现 skBuildDescList 或 skFilePicked
        m = re.search(r"addEventListener\('drop'.{0,1200}", text, re.S)
        assert m, f"{name}: 找不到 drop 监听"
        seg = m.group(0)
        assert ("skBuildDescList" in seg) or ("skFilePicked" in seg), (
            f"{name}: 拖入之后没有重建描述框（既没调 skBuildDescList 也没调 skFilePicked）"
        )


def test_desc_can_never_be_empty():
    """后端 desc 必填；前端必须有兜底，否则用户不填就整批失败。"""
    for name in PAGES:
        body = _fn_body(_code(name), "doUploadSticker") or _fn_body(_code(name), "uploadStickers")
        assert body is not None, f"{name}: 找不到上传函数"
        assert re.search(r"f\.name\.replace\(", body), (
            f"{name}: 上传时 desc 没有用文件名兜底 —— 不填描述会被后端拒收"
        )
