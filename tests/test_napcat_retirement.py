"""Retired NapCat cannot be selected or launched; old data stays intact."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply.config_store import QQAutoReplyConfigStore
from plugin.plugins.qq_auto_reply.napcat_service import QQNapcatService
from plugin.plugins.qq_auto_reply.onebot_backend import OneBotBackendService
from plugin.plugins.qq_auto_reply.runtime_ops_service import QQRuntimeOpsService
from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService


@pytest.mark.parametrize("backend", [None, "napcat", "snowluma"])
@pytest.mark.parametrize("mode", ["napcat", "napcat_forward", "open_platform"])
async def test_migration_preserves_data_and_disables_only_retired_onebot_autostart(tmp_path, backend, mode):
    original = {
        "qq_connection_mode": mode, "auto_start_on_launch": True,
        "napcat_directory": "C:/old-napcat", "snowluma_directory": "C:/snowluma",
        "token": "existing-token", "trusted_users": [{"qq": "123", "level": "admin"}],
        "backend_deployments": {"napcat": "C:/old-napcat"},
    }
    if backend is not None:
        original["onebot_backend"] = backend
    store = QQAutoReplyConfigStore(tmp_path)
    store.path.write_text(json.dumps(original), encoding="utf-8")
    settings = await store.load()
    assert settings["onebot_backend"] == "snowluma"
    assert settings["auto_start_on_launch"] is (backend == "snowluma" or mode == "open_platform")
    for key in ("napcat_directory", "snowluma_directory", "token", "trusted_users", "backend_deployments"):
        assert settings[key] == original[key]
    if backend != "snowluma":
        assert store.migration_applied
    await store.save(settings)
    assert (await store.load())["auto_start_on_launch"] == settings["auto_start_on_launch"]


async def test_retired_backend_rejected_without_mutation_or_shutdown():
    plugin = SimpleNamespace(_qq_settings={"onebot_backend": "snowluma"}, _stop_auto_reply_runtime=AsyncMock())
    with pytest.raises(ValueError):
        await QQSettingsService(plugin).save_settings(onebot_backend="napcat")
    assert plugin._qq_settings == {"onebot_backend": "snowluma"}
    plugin._stop_auto_reply_runtime.assert_not_awaited()


async def test_direct_napcat_start_never_spawns_even_with_existing_launcher(tmp_path, monkeypatch):
    (tmp_path / "launcher.bat").write_text("@echo off")
    spawn = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    service = QQNapcatService(get_settings=lambda: {"napcat_directory": str(tmp_path)})
    with pytest.raises(RuntimeError, match="已移除"):
        await service.ensure_napcat_started()
    spawn.assert_not_awaited()


async def test_stale_runtime_request_cannot_start_napcat(tmp_path):
    settings = {"onebot_backend": "napcat", "qq_connection_mode": "napcat"}
    service = OneBotBackendService(get_settings=lambda: settings, data_dir=tmp_path)
    assert set(service.services) == {"snowluma"}
    service.services["snowluma"].ensure_napcat_started = AsyncMock()
    plugin = SimpleNamespace(_qq_settings=settings, napcat_service=service)
    assert (await QQRuntimeOpsService(plugin).control_backend(start=True, backend="napcat")).is_err()
    service.services["snowluma"].ensure_napcat_started.assert_not_awaited()


def test_ui_has_no_napcat_choice_or_download_tutorial():
    root = Path(__file__).resolve().parents[1]
    for name in ("index.html", "status.html", "napcat.html", "old.html", "onebot-backend.js"):
        source = (root / "static" / name).read_text(encoding="utf-8")
        assert "NapCat" not in source
        assert "NapNeko" not in source
        assert '<option value="napcat">' not in source
    for name in ("napcat_install.py", "napcat_integrity.py"):
        assert not (root / name).exists()
