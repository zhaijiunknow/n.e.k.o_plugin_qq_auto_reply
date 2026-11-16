# -*- coding: utf-8 -*-
"""控制台做减法（2026-11-16）：很多用户是学生，不该一进来面对八个入口。

只动布局、不删功能：侧栏只留日常三项，其余收进默认折叠的「高级」；名单页里排查工具和
认领码收进折叠区；没换过通道的人看不到「换通道认领」。
"""

from __future__ import annotations

import pathlib
import re

HTML = (pathlib.Path(__file__).resolve().parents[1] / "static" / "open_platform.html").read_text(encoding="utf-8")
NAV = HTML[HTML.index('<nav aria-label="控制台导航">'):HTML.index("</nav>")]


def _pages(fragment: str) -> list[str]:
    return [page + (":" + sub if sub else "") for page, sub in
            re.findall(r'class="nav-item[^"]*" data-page="([a-z]+)"(?: data-sub="([a-z]+)")?', fragment)]


def test_the_sidebar_shows_three_everyday_entries():
    everyday, advanced = NAV.split('<details class="nav-advanced"')
    assert _pages(everyday) == ["status", "config:accounts", "sticker"]
    assert _pages(advanced) == ["config", "plugintools", "logs", "guide"]
    assert 'data-nav="status.html"' in advanced, "重新扫码绑定的入口还在（收在高级里）"
    assert "<details" in NAV and " open" not in NAV.split("<summary")[0].split("<details")[1], "高级默认折叠"


def test_every_page_is_still_reachable():
    for page in ("status", "config", "sticker", "plugintools", "logs", "guide"):
        assert f'data-page="{page}"' in NAV


def test_the_roster_entry_opens_the_accounts_tab_and_highlights_alone():
    assert "switchPage(item.dataset.page);if(item.dataset.sub){switchSub(item.dataset.sub);adv.open=was}" in HTML, (
        "「名单」要直接落到用户子页，并且不因为途经「配置」的第一个子页而把「高级」展开"
    )
    assert re.search(r"^function markNav\(\)", HTML, re.M)
    switch_sub = re.search(r"^function switchSub\(.*$", HTML, re.M).group(0)
    assert switch_sub.rstrip().endswith("markNav()}"), "每次换页 / 换子页都要重算侧栏高亮"
    assert "adv.open=true" in HTML, "当前页在高级里时要自动展开"


def test_the_accounts_page_tucks_rarely_used_tools_away():
    page = HTML.split('<div class="page" id="page-config-accounts">')[1].split('<div class="page"')[0]
    probe = page.index('id="cfg-identity-probe"')
    assert page.rindex("<details", 0, probe) > page.rfind("</details>", 0, probe), "排查开关在折叠区里"
    card = page[page.index('id="identity-links-card"'):page.index('id="plugin-tools-card"')]
    assert card.index('id="seen-table"') < card.index("<details") < card.index('id="link-table"'), (
        "先是「这是谁」的列表，认领码和已匹配记录收在折叠区"
    )
    assert 'id="identity-links-card" style="display:none"' in page
    assert "getElementById('identity-links-card').style.display=" in HTML, "加载后即显示，纯开放平台用户也能关联"


def test_the_prompt_editor_is_gone_from_the_console_but_not_from_the_backend():
    """提示词编辑从界面上拔掉（使用者 2026-11-16）；后端入口和已保存的覆盖不动。"""
    assert "prompts" not in HTML and "PromptOverride" not in HTML and "loadPromptEditorState" not in HTML
    source = (pathlib.Path(__file__).resolve().parents[1] / "__init__.py").read_text(encoding="utf-8")
    assert 'if action == "prompt_editor":' in source and "resolve_prompt_override" in source


def test_the_owner_binding_card_is_onebot_only():
    """「私有插件上下文：主人绑定」只在 OneBot 控制台出现（使用者 2026-11-16）。

    自动背景在代码里只对 OneBot 生效，要填的还是 QQ 号 —— 开放平台用户手里只有 openid。
    开放平台并入本体后背景由本体统一提供，这张卡片在稳定版控制台上只是一张填了没用的表单。
    后端和已保存的绑定不动。
    """
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    root = pathlib.Path(__file__).resolve().parents[1]
    script = (root / "static" / "plugin-tools.js").read_text(encoding="utf-8")
    assert 'id="config-companion-context"' not in HTML, "开放平台控制台没有承载这张卡片的容器"
    assert 'id="config-companion-context"' in (root / "static" / "napcat.html").read_text(encoding="utf-8")

    harness = r"""
const assert = require('node:assert/strict');
const elements = {};
const host = {set innerHTML(markup) {
  elements['cc-card'] = {remove() {delete elements['cc-card'];}};
  for (const id of ['pt-zone-available', 'pt-zone-all', 'pt-zone-admin', 'pt-available', 'pt-all', 'pt-admin',
                    'plugin-tools-channel', 'pt-empty'])
    elements[id] = {addEventListener(){}, innerHTML:'', textContent:'', style:{}, classList:{toggle(){}, add(){}, remove(){}}};
}};
elements['page-plugintools'] = host;
if (onebot) elements['config-companion-context'] = {appendChild(card) {card.parent = this;}};
global.document = {readyState:'loading', addEventListener(){}, getElementById: id => elements[id] || null,
                   querySelectorAll() {return [];}};
global.window = {};
global.t = (key, fallback) => fallback; global.tf = (key, fallback) => fallback;
global.escapeHtml = String; global.escapeOnclick = String; global.toast = () => {};
const requests = [];
global.call = async (entry, args) => {
  requests.push(args.action);
  if (args.action === 'plugin_tools') return {candidates: [{plugin_id: 'forever_companion', name: 'x', running: true,
      entries: ['a'], context_capabilities: [{label: '角色整体情绪', available: true}]}], tiers: {}};
  return {settings: {companion_context: {}}};
};
"""
    checks = r"""
(async () => {
  window.ptMountPluginTools();
  assert.equal('cc-card' in elements, onebot);
  await window.loadPluginTools();
  const rendered = Object.values(elements).map(e => e.innerHTML || '').join('');
  assert.ok(rendered.includes('forever_companion') || rendered.includes('>x<') || rendered.length > 0);
  assert.equal(rendered.includes('自动背景'), onebot, '插件卡片上的「自动背景」标注只在 OneBot 控制台出现');
  assert.equal(requests.includes('dashboard'), onebot, '没有绑定卡片时不为它多查一次配置');
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    for onebot in (True, False):
        result = subprocess.run(
            [node, "-e", "const onebot=" + json.dumps(onebot) + ";" + harness + script + checks],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        assert result.returncode == 0, f"onebot={onebot}: " + result.stdout + result.stderr
