"""界面提交的设置键必须全在 `_CONFIG_SAVE_KEYS` 白名单里。

这是"点了保存没效果"的成因那一族：`save` 那条链是**三层逐个具名透传**
（入口白名单 → dashboard_service 具名参数 → settings_service 逐个 kwargs.get），
**任何一层没点名，那个键就被静默丢掉** —— 界面照常弹「设置已保存」，
但值刷新后弹回原值，看起来就是"保存没生效"。

`enable_group_attention` 就这么丢过：界面一直在发，三层都没接。

===== 归并到 settings_schema 之后的形状 =====

白名单现在由 ``settings_schema.SAVEABLE_KEYS`` 生成，所以"白名单"和"表"不可能对不上；
第 3 条守卫因此换了标的：它现在盯的是**表里标了 handler 的键，settings_service 里
是不是真有代码管它** —— 那才是"声明了却没人实现"这类静默失效。
"""
from __future__ import annotations

import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parents[1]


def _allowlist() -> set[str]:
    """真实白名单。**从源码正则抠字面量已经不可靠了**（它现在是生成式），直接导入。"""
    from plugin.plugins.qq_auto_reply import settings_schema

    return set(settings_schema.SAVEABLE_KEYS)


def _ui_save_keys(html_name: str) -> set[str]:
    """从 `let args={...}` 里抠出顶层键名。"""
    html = (BASE / "static" / html_name).read_text(encoding="utf-8")
    m = re.search(r"let args=\{(.*?)\};", html, re.S)
    assert m, f"{html_name}: 找不到 args 字面量（界面改写法了？）"
    return set(re.findall(r"(?:^|,)\s*([a-z_][a-z_0-9]*)\s*:", m.group(1)))


def test_napcat_save_form_sends_only_allowed_keys():
    """napcat.html 的「保存设置」提交的键 —— 一个都不能落在白名单外。"""
    outside = sorted(_ui_save_keys("napcat.html") - _allowlist())
    assert not outside, (
        f"这些键会被静默丢弃（三层具名透传没接）：{outside}。"
        f"补 `settings_schema` 里对应 spec 的 saveable=True + 服务层处理。"
    )


def test_allowlist_is_generated_from_the_schema_table():
    """白名单必须**恰好**是表里 saveable 的键（含别名）—— 防手工改回去。"""
    from plugin.plugins.qq_auto_reply import settings_schema

    expected = {
        name
        for spec in settings_schema.SETTINGS
        if spec.saveable
        for name in (spec.key, *spec.aliases)
    }
    assert _allowlist() == expected


def test_the_attention_switch_is_wired_end_to_end():
    """这条是通用守卫的**具体案例** —— 它曾经真的断过。"""
    assert "enable_group_attention" in _allowlist()
    assert "enable_group_attention" in _ui_save_keys("napcat.html")

    import inspect

    from plugin.plugins.qq_auto_reply.dashboard_service import QQDashboardService
    from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService

    assert "enable_group_attention" in inspect.signature(
        QQDashboardService.save_settings).parameters
    # 最后一层：真的写进设置
    assert '"enable_group_attention"' in inspect.getsource(
        QQSettingsService._save_settings_locked)


def test_every_declared_handler_is_actually_implemented():
    """**第三层守卫**：表里声明了 handler，`settings_service` 里就得真有代码管它。

    归并之后"白名单里的键没被写"已经结构上不可能（通用路径覆盖所有
    ``handler is None`` 的键），剩下能断的是这一种：表里写了个 handler 名字，
    实际没人实现 —— 那个键就会静静地走通用路径，联动/抛错/延迟发布全丢。
    """
    import inspect

    from plugin.plugins.qq_auto_reply import settings_schema
    from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService

    src = inspect.getsource(QQSettingsService._save_settings_locked)
    declared = {spec.key: spec.handler for spec in settings_schema.SETTINGS if spec.handler}

    # 有 handler 的键：它的具名块必须在 _save_settings_locked 里（读或写都算）。
    missing = sorted(k for k in declared if f'"{k}"' not in src)
    assert not missing, (
        f"表里标了 handler，但 settings_service 里没有对应代码：{missing}。"
        f"这些键的联动逻辑会被静默跳过。"
    )


def test_plain_keys_are_covered_by_the_generic_path():
    """``handler is None`` 的 saveable 键由通用路径写 —— 它的 kind 必须受支持。"""
    from plugin.plugins.qq_auto_reply import settings_schema

    supported = {"int", "float", "bool", "str", "list", "dict"}
    plain = [s for s in settings_schema.SETTINGS if s.saveable and not s.handler]
    unsupported = sorted(s.key for s in plain if s.kind not in supported)
    assert not unsupported, f"通用路径不认识这些 kind：{unsupported}"
    assert plain, "通用路径没有任何键可管 —— 表结构变了？"
