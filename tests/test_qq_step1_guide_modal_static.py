import pathlib

# 同时支持独立插件仓库和宿主中的部署目录。
ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_step1_modal_markup_present():
    # The NapCat first-step onboarding moved from a standalone modal in index.html
    # (index.html is now just the napcat/open-platform mode picker) to the embedded
    # "配置引导" guide page in napcat.html.
    html = (ROOT / "static/napcat.html").read_text(encoding="utf-8")
    assert 'id="guide-detail-step-napcat"' in html
    assert 'data-i18n="ui.shared.card.guide"' in html
    assert 'https://github.com/NapNeko/NapCatQQ/releases' in html


def test_step1_state_persisted_in_config_and_backend():
    # 默认值的真相已归并到 settings_schema 的声明表里 —— config_store 不再逐键手写
    # （见 config_store.default_config 的 docstring），白名单也由那张表生成，
    # 所以 __init__.py 里不再有这个键的字面量。断言改指表与运行时白名单。
    from plugin.plugins.qq_auto_reply import settings_schema

    schema = (ROOT / "settings_schema.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "dashboard_service.py").read_text(encoding="utf-8")
    assert 'SettingSpec("guide_step_napcat_done", "bool", False' in schema
    # 端到端：它确实在保存白名单里（界面能存下来）
    assert "guide_step_napcat_done" in settings_schema.SAVEABLE_KEYS
    # Runtime status is built by the runtime service, and the napcat step's done-state
    # is derived from a live managed+running NapCat process in the dashboard service.
    assert 'runtime = self.plugin.runtime_service.build_runtime_status()' in dashboard
    assert 'runtime["napcat_managed"] and runtime["napcat_running"]' in dashboard


def test_step1_frontend_handlers_present():
    script = (ROOT / "static/script.js").read_text(encoding="utf-8")
    assert 'function openStep1GuideModal()' in script
    assert 'async function confirmStep1GuideModal()' in script
    assert "guide_step_napcat_done: true" in script
    assert "document.getElementById('guide-step-napcat').addEventListener('click', () => {" in script
