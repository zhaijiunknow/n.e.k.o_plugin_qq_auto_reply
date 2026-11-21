"""Deployment validates the QQ account before installation side effects."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply.deploy_service import QQDeployService


@pytest.mark.parametrize("uin", ["", "   ", "0", "012345", "abc", "123/456", "１２３４５", "123\n456"])
async def test_deploy_rejects_invalid_account_before_any_side_effect(uin):
    # No services are supplied: validation must precede accessing installation/settings/runtime.
    svc = QQDeployService(SimpleNamespace())
    with pytest.raises(RuntimeError, match="请先填写有效的机器人 QQ 号"):
        await svc.deploy(uin=uin)
