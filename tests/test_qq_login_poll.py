"""Only a live OneBot login confirms SnowLuma login; files never do."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply.deploy_service import QQDeployService


@pytest.mark.parametrize("status,uin,expected", [("online", "123", "completed"), ("offline", "123", "pending"), ("online", "", "pending")])
async def test_poll_uses_live_login(status, uin, expected, tmp_path):
    (tmp_path / "onebot11_123.json").write_text("{}")
    plugin = SimpleNamespace(_qq_settings={"onebot_backend": "snowluma"},
        runtime_service=SimpleNamespace(fetch_login_status_payload=AsyncMock(return_value={"status": status, "self_id": uin})))
    assert (await QQDeployService(plugin).poll_login())["status"] == expected
