"""Beta-only guard: sync shared fixes without importing stable UI restrictions."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_beta_keeps_onebot_navigation_and_deployment_controls():
    home = (ROOT / "static/index.html").read_text(encoding="utf-8")
    for target in ("napcat.html", "open_platform.html", "status.html", "old.html"):
        assert f'data-nav="{target}"' in home
    status = (ROOT / "static/status.html").read_text(encoding="utf-8")
    for control in ("modes", "onebot-backend-section", "sec-napcat", "btn-qr"):
        assert f'id="{control}"' in status
    console = (ROOT / "static/open_platform.html").read_text(encoding="utf-8")
    assert 'data-nav="napcat.html"' in console
    assert 'data-nav="status.html"' in console
    assert "identity_link" in console


def test_beta_has_no_stable_only_routing_or_visibility_rules():
    assert not (ROOT / "static/onebot-legacy.js").exists()
    assert not (ROOT / "tests/test_qq_stable_no_onebot_ui.py").exists()
    for name in ("index", "status", "open_platform"):
        text = (ROOT / "static" / f"{name}.html").read_text(encoding="utf-8")
        for marker in ("OneBotLegacy", "onebot-legacy", "stable-no-onebot-ui", "keepOneBot"):
            assert marker not in text, (name, marker)
    for locale in ("en", "zh-CN"):
        bundle = json.loads((ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        assert not any(key.startswith("ui.stable.") for key in bundle)


@pytest.mark.parametrize("name", ["index", "status", "open_platform", "napcat"])
def test_beta_inline_scripts_parse(name, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for frontend syntax checks")
    html = (ROOT / "static" / f"{name}.html").read_text(encoding="utf-8")
    for index, script in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        path = tmp_path / f"{name}-{index}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
