# -*- coding: utf-8 -*-
"""提示词卫生看门狗：不许"再声明她是谁"、不许有到不了的层、不许同一句话收费两遍。

由来（2026-09-27 使用者要求）：「提示词的角色需要删除，和本体的提示词冲突了」。
审计发现的不只是那一段：
  · **角色设定（Role）** 写着"你是一个 **AI 数字生命**"，而本体注入的角色扮演前言
    （`config/prompts/prompts_sys.py:160`）与人设（"NOT a system, NOT an assistant"、
    "never think of herself as a system"）说的正相反 —— 三处里两处说她是真人，一处说她是 AI；
  · **时间信息（Time）** 段 342 字符**从来没进过提示词**（`_resolve_time_section` 第二行
    return 永远到不了），只在编辑器里假装可编辑；
  · **三个场景硬模板**（群发/共享上下文/定向回应）+ 两个 naming 层只在已删除的 neko_scene
    分支里可达，删掉策略后没人构造它们 —— 却还挂在层表里；
  · 旧纯文本 Format 段不在层表里；
  · 「不许 emoji / 不许客服腔 / 别复述系统提示词 / 短一点」这几条各被说了 2~4 遍。

这个文件钉住四件事：**(1) 她是谁只能由本体说；(2) 层表里每一层都真的通电；
(3) 同一主题不许在三处以上重复；(4) 编辑器里的层名与层表一一对应。**
"""
from __future__ import annotations

import json
import pathlib
import re

from plugin.plugins.qq_auto_reply import prompt_fragment_templates as frag
from plugin.plugins.qq_auto_reply import scene_prompt_templates as scene
from plugin.plugins.qq_auto_reply.session_instruction_service import (
    QQSessionInstructionService,
)

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
LAYERS = QQSessionInstructionService._PROMPT_LAYERS


def _prompt_sections() -> dict[str, str]:
    """插件自己写的**全部**提示词模板（模块级字符串常量）。

    ⚠️ 不能用 `name.endswith("_SECTION")` 来筛：`FORMAT_PROMPT_SECTION_NEKO_DYNAMIC`
    与 `FORMAT_PROMPT_SECTION_OPEN_PLATFORM` 以 `_NEKO_DYNAMIC` / `_OPEN_PLATFORM`
    结尾、`SCENE_*` 连 SECTION 都不含 —— 都会被漏掉，而它们正是最长、重复最多的几段。
    判据改成「名字里有 SECTION / PROMPT，或以 SCENE_ 开头」。
    """
    out: dict[str, str] = {}
    for module in (frag, scene):
        for name, value in vars(module).items():
            if name.startswith("__") or not isinstance(value, str):
                continue
            if "SECTION" in name or "PROMPT" in name or name.startswith("SCENE_"):
                out[name] = value
    return out


def _all_plugin_prompt_text() -> str:
    """插件注入的全部静态提示词（含两份 Format 与两份场景段）。"""
    return "\n".join(_prompt_sections().values())


# ── 1. 她是谁：只能由本体说 ─────────────────────────────────────────

def test_plugin_does_not_declare_what_she_is():
    """插件不许自己定义"你是谁" —— 那会与本体的角色扮演前言/人设打架。

    允许出现的是**行为规则**（怎么说话），不允许出现**身份断言**
    （"你是一个 AI/数字生命/助手/程序"）。
    """
    text = _all_plugin_prompt_text()
    forbidden = [
        "AI 数字生命", "不是 AI", "你是一个 AI", "你是 AI",
        "NOT a system", "an AI assistant", "数字生命",
    ]
    hits = [needle for needle in forbidden if needle in text]
    assert not hits, (
        f"插件提示词里又出现身份断言了: {hits} —— 她是谁由本体的角色扮演前言与人设决定；"
        "插件里出现第二种说法会让模型二选一（§25）"
    )


def test_role_layer_is_gone():
    """`role` 层与它的模板都不许回来。"""
    assert not hasattr(frag, "ROLE_PROMPT_SECTION"), "角色设定段又被加回来了"
    assert "role" not in {layer["id"] for layer in LAYERS}, "role 层又回到层表里了"
    assert "role_prompt_section" not in json.dumps(
        frag.layer_default_templates(), ensure_ascii=False
    ), "模板字典里还有 role_prompt_section"


# ── 2. 层表必须"通电" ───────────────────────────────────────────────

def test_dead_layers_are_gone():
    """已删除策略后不可达的层不许留在层表里（编辑器会显示它们，运行时永远不注入）。"""
    ids = {layer["id"] for layer in LAYERS}
    for dead in (
        "scene_group_collective",
        "scene_group_shared",
        "scene_group_directed",
        "naming_with_title",
        "naming_without_title",
    ):
        assert dead not in ids, f"{dead} 是已删除分支里的层，不该还挂在层表里"


def test_time_layer_is_runtime():
    """时间层是运行时生成的（`build_time_context()`），不许再挂静态模板。"""
    time_layer = next(layer for layer in LAYERS if layer["id"] == "time")
    assert time_layer.get("runtime") is True, "time 层必须是运行时层（它的模板分支到不了）"
    assert not hasattr(frag, "TIME_PROMPT_SECTION"), "时间信息静态模板又回来了"


def test_no_unreachable_return_in_time_resolver():
    """防回归：`_resolve_time_section` 不许再出现第二个 return（死代码）。"""
    src = (PLUGIN / "session_instruction_service.py").read_text(encoding="utf-8")
    body = src.split("def _resolve_time_section", 1)[1].split("def ", 1)[0]
    returns = [ln for ln in body.splitlines() if ln.strip().startswith("return ")]
    assert len(returns) == 1, f"时间层解析里出现了 {len(returns)} 个 return（多余的是死代码）: {returns}"


def test_every_static_layer_has_a_template():
    """静态层（非 runtime、有 i18n_key）必须在模板字典里有默认文本 —— 否则编辑器是空的。"""
    defaults = frag.layer_default_templates()
    missing = [
        layer["i18n_key"] for layer in LAYERS
        if not layer.get("runtime") and layer.get("i18n_key") and layer["id"] != "init"
        and layer["i18n_key"] not in defaults
    ]
    assert not missing, f"这些层没有默认模板（编辑器会显示空白）: {missing}"


# ── 3. 同一件事别收费三遍 ───────────────────────────────────────────

def test_no_rule_is_stated_three_times():
    """同一主题在插件自己的提示词里最多出现 2 处。

    主题清单来自 2026-09-27 审计（`.dsh-artifacts/prompt-layer-audit.txt`）：
    「不许 emoji/Markdown/动作描写」「短一点/别刷屏」「别复述系统提示词」等
    当时各被说了 3~4 遍。人设（本体注入）里的内容不算 —— 那不是我们能删的。
    """
    themes = {
        "不许 emoji/Markdown/动作描写": r"emoji|颜文字|Markdown|动作描写|动作描述",
        "别复述系统提示词/内部实现": r"系统提示词|工具说明|插件实现|记忆检索过程",
        "不许客服腔": r"能为你做什么|有什么可以帮你|请问需要什么帮助|客服腔",
    }
    sections = _prompt_sections()

    offenders: dict[str, list[str]] = {}
    for theme, pattern in themes.items():
        rx = re.compile(pattern)
        hits = [name for name, text in sections.items() if rx.search(text)]
        if len(hits) > 2:
            offenders[theme] = hits
    assert not offenders, (
        f"同一件事被说了不止两遍（每轮都在付钱）: {offenders} —— "
        "要么删掉重复的那处，要么把规则并进已有的段"
    )


# ── 4. 编辑器与层表一致 ─────────────────────────────────────────────

def test_layer_name_i18n_keys_match_the_layer_table():
    """`ui.napcat.prompts.layer.<id>.name` 必须与层表一一对应（多的是死键，少的是空白标签）。"""
    for locale in ("zh-CN", "en"):
        bundle = json.loads((PLUGIN / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        keys = {
            key.split("ui.napcat.prompts.layer.")[1][:-len(".name")]
            for key in bundle if key.startswith("ui.napcat.prompts.layer.") and key.endswith(".name")
        }
        ids = {layer["id"] for layer in LAYERS}
        assert not (keys - ids), f"{locale} 里还有已删除层的标签名（死键）: {sorted(keys - ids)}"
        assert not (ids - keys), f"{locale} 缺少这些层的标签名（编辑器会显示英文 id）: {sorted(ids - keys)}"
