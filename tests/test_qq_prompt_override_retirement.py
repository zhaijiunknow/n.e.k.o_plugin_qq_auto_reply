"""Retired prompt edits must not return through tool calls, old configs or runtime fallback."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin, settings_schema
from plugin.plugins.qq_auto_reply import config_store as store_module
from plugin.plugins.qq_auto_reply import session_instruction_service as instructions
from plugin.plugins.qq_auto_reply.config_store import QQAutoReplyConfigStore
from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService
from plugin.sdk.shared.core.decorators import EVENT_META_ATTR

ROOT = Path(__file__).resolve().parents[1]
RETIRED_ACTIONS = ("prompt_save", "prompt_reset", "group_prompt_save", "group_prompt_delete")
RETIRED_KEYS = ("prompt_overrides", "group_prompts")
SENTINEL = "RETIRED_HIDDEN_INSTRUCTION"


def _legacy_settings():
    return {
        "prompt_overrides": {
            locale: {"init": SENTINEL + " {name}", "": SENTINEL + " {name}",
                     "attention_prompt_section": SENTINEL, "group_attention_prompt_section": SENTINEL}
            for locale in ("zh", "zh-CN", "zh-TW", "en")
        },
        "group_prompts": {"g1": SENTINEL, "old-group": SENTINEL},
        "trusted_users": [{"qq": "owner", "level": "admin"}],
        "trusted_groups": [{"group_id": "g1", "level": "trusted"}],
        "group_memory_enabled": True,
        "unrelated_saved_data": {"keep": [1, 2]},
    }


def _plugin(tmp_path):
    return SimpleNamespace(
        config_store=QQAutoReplyConfigStore(tmp_path), _qq_settings={},
        data_path=lambda name: tmp_path / name,
        permission_mgr=None, group_permission_mgr=None, backlog_store=None,
        logger=logging.getLogger("prompt-retirement"), _emit_log=Mock(),
        _create_backlog_store_from_settings=lambda settings: None,
    )


@pytest.mark.parametrize("action", RETIRED_ACTIONS)
async def test_old_config_actions_are_rejected_even_when_called_directly(action):
    plugin = SimpleNamespace(_kick_deferred_startup_tasks=Mock())
    result = await QQAutoReplyPlugin._config_dispatch(plugin, action, {
        "locale": "zh-CN", "layer_id": "init", "text": SENTINEL, "group_id": "g1",
    })
    assert result.is_err() and "BAD_ACTION" in str(result.error)
    assert not hasattr(QQAutoReplyPlugin, "_config_" + action)


def test_agent_schema_and_generic_save_do_not_accept_retired_controls():
    meta = getattr(QQAutoReplyPlugin.config_entry, EVENT_META_ATTR)
    properties = meta.input_schema["properties"]
    assert set(RETIRED_ACTIONS).isdisjoint(properties["action"]["enum"])
    assert {"layer_id", "text", *RETIRED_KEYS}.isdisjoint(properties)
    assert set(RETIRED_KEYS).isdisjoint(settings_schema.BY_KEY)
    assert set(RETIRED_KEYS).isdisjoint(QQAutoReplyPlugin._CONFIG_SAVE_KEYS)
    query_meta = getattr(QQAutoReplyPlugin.query, EVENT_META_ATTR)
    assert "group_prompts" not in query_meta.input_schema["properties"]["action"]["enum"]


async def test_generic_save_drops_retired_keys_and_keeps_normal_settings():
    save = AsyncMock(return_value="saved")
    plugin = SimpleNamespace(
        _CONFIG_SAVE_KEYS=QQAutoReplyPlugin._CONFIG_SAVE_KEYS,
        dashboard_service=SimpleNamespace(save_settings=save),
        _emit_log=Mock(), logger=logging.getLogger("prompt-retirement"),
    )
    retired = {key: _legacy_settings()[key] for key in RETIRED_KEYS}
    result = await QQAutoReplyPlugin._config_save(plugin, retired)
    assert result.is_err() and "INVALID_INPUT" in str(result.error)
    save.assert_not_awaited()
    result = await QQAutoReplyPlugin._config_save(plugin, {**retired, "reply_mode": "text"})
    assert result == "saved"
    save.assert_awaited_once_with(reply_mode="text")
    assert plugin._emit_log.called


@pytest.mark.parametrize("overrides", [_legacy_settings(), {"prompt_overrides": [], "group_prompts": "broken"},
                                      {"prompt_overrides": {}, "group_prompts": {}}])
async def test_startup_cleans_old_keys_on_disk_once_without_touching_other_data(tmp_path, overrides):
    plugin = _plugin(tmp_path)
    plugin.config_store.path.write_text(json.dumps(overrides), encoding="utf-8")
    service = QQSettingsService(plugin)
    await service.load_business_config()
    saved = json.loads(plugin.config_store.path.read_text(encoding="utf-8"))
    assert set(RETIRED_KEYS).isdisjoint(saved)
    assert set(RETIRED_KEYS).isdisjoint(plugin._qq_settings)
    for key, value in overrides.items():
        if key not in RETIRED_KEYS:
            assert saved[key] == value
    assert plugin.config_store.migration_applied is True
    plugin.config_store.save = AsyncMock(wraps=plugin.config_store.save)
    await service.load_business_config()
    assert plugin.config_store.migration_applied is False
    plugin.config_store.save.assert_not_awaited()


async def test_cleanup_write_failure_stays_disabled_and_retries_next_load(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    original = json.dumps(_legacy_settings())
    plugin.config_store.path.write_text(original, encoding="utf-8")
    service = QQSettingsService(plugin)
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "atomic_write_json_async", AsyncMock(side_effect=OSError("read-only disk")))
        await service.load_business_config()
    assert plugin.config_store.path.read_text(encoding="utf-8") == original
    assert set(RETIRED_KEYS).isdisjoint(plugin._qq_settings)
    logs = plugin._emit_log.call_args_list
    assert any(call.args[0] == "WARN" for call in logs)
    assert not any("已落盘" in call.args[1] for call in logs)
    assert plugin._qq_settings["trusted_users"] == _legacy_settings()["trusted_users"]
    await service.load_business_config()
    assert set(RETIRED_KEYS).isdisjoint(json.loads(plugin.config_store.path.read_text(encoding="utf-8")))


async def test_saving_an_old_backup_cannot_restore_overrides(tmp_path):
    store = QQAutoReplyConfigStore(tmp_path)
    saved = await store.save(_legacy_settings())
    assert set(RETIRED_KEYS).isdisjoint(saved)
    assert set(RETIRED_KEYS).isdisjoint(await store.load())
    assert saved["unrelated_saved_data"] == {"keep": [1, 2]}


@pytest.mark.parametrize("locale", ["zh-CN", "zh-TW", "en"])
@pytest.mark.parametrize("is_group", [False, True])
@pytest.mark.parametrize("needs_attention", [False, True])
async def test_actual_prompt_bundle_ignores_legacy_overrides_but_preserves_character(
    tmp_path, monkeypatch, locale, is_group, needs_attention,
):
    monkeypatch.setattr(instructions, "get_global_language_full", lambda: locale)
    service = instructions.QQSessionInstructionService(SimpleNamespace(
        _qq_settings=_legacy_settings(),
        data_path=lambda name: tmp_path / name,
        i18n=SimpleNamespace(t=lambda key, default=None, **kw: default or key),
        qq_client=SimpleNamespace(needs_attention=needs_attention),
        prompt_builder=SimpleNamespace(should_use_memory_context=lambda **kw: False),
        identity_links=SimpleNamespace(group=lambda value: "old-group"),
        logger=logging.getLogger("prompt-retirement"),
    ))
    # Keep the real template resolvers, group orchestration, role-card injection and compose path.
    for name in ("_load_emoji_catalog", "_load_sticker_catalog", "_build_sessions_section",
                 "_build_accounts_section", "_resolve_time_section", "_build_chat_environment_section",
                 "_build_pending_commitments_section", "_build_scene_section", "_append_cross_group_section"):
        setattr(service, name, Mock(return_value=""))
    service._sessions_section_discloses_others = Mock(return_value=False)
    service._build_core_memory_section = AsyncMock(return_value="")
    service._append_user_profile_section = AsyncMock()
    service._append_blacklist_section = Mock()
    service._append_attention_context_section = Mock()
    bundle = await service.build_session_instructions(
        her_name="Neko", master_name="Owner", character_prompt="KEEP_CHARACTER_PERSONA",
        character_card_fields={"hobby": "KEEP_CHARACTER_CARD"}, permission_level="trusted",
        sender_id="u1", user_title="Friend", is_group=is_group, group_id="g1" if is_group else None,
    )
    assert SENTINEL not in bundle.system_prompt
    assert "Group-Specific Instructions" not in bundle.system_prompt
    assert "KEEP_CHARACTER_PERSONA" in bundle.system_prompt
    assert "KEEP_CHARACTER_CARD" in bundle.system_prompt
    assert service._resolve_init_template(locale).format(name="Neko") in bundle.system_prompt


async def test_read_only_template_query_cannot_display_or_reactivate_old_overrides(tmp_path):
    plugin = _plugin(tmp_path)
    plugin._qq_settings = _legacy_settings()
    plugin.i18n = SimpleNamespace(t=lambda key, default=None, **kw: default or key)
    plugin.session_instruction_service = instructions.QQSessionInstructionService(plugin)
    result = await QQAutoReplyPlugin._query_prompt_editor(plugin, {"mode": "open_platform", "locale": "zh-TW"})
    assert result.is_ok()
    payload = result.value
    assert SENTINEL not in json.dumps(payload)
    assert all(layer["read_only"] for layer in payload["layers"])
    init = next(layer for layer in payload["layers"] if layer["id"] == "init")
    assert init["effective_text"] == plugin.session_instruction_service._resolve_init_template("zh-TW")


def test_console_contains_no_retired_prompt_writes_and_scripts_parse(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for frontend syntax checks")
    for name in ("napcat", "open_platform"):
        html = (ROOT / "static" / f"{name}.html").read_text(encoding="utf-8")
        for action in RETIRED_ACTIONS:
            assert action not in html
        assert "groupprompts" not in html
        assert "savePromptOverride" not in html
        for index, script in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
            path = tmp_path / f"{name}-{index}.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
