# -*- coding: utf-8 -*-
"""控制台做减法（2026-11-16）：很多用户是学生，不该一进来面对八个入口。

侧栏只留日常三项，其余收进默认折叠的「高级」；开放平台名单页的排查工具和认领码
收进折叠区。两种连接均移除提示词页面，OneBot 保留连接、部署、登录和审阅能力。
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


def test_prompt_editing_is_retired_and_only_read_only_diagnostics_remain():
    """稳定版不暴露编辑器；后端只保留内置模板的只读诊断，不再读旧覆盖。"""
    assert "prompts" not in HTML and "PromptOverride" not in HTML and "loadPromptEditorState" not in HTML
    source = (pathlib.Path(__file__).resolve().parents[1] / "__init__.py").read_text(encoding="utf-8")
    assert 'if action == "prompt_editor":' in source and "resolve_prompt_override" not in source


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


def test_onebot_uses_the_same_small_sidebar_without_retired_pages():
    html = (pathlib.Path(__file__).resolve().parents[1] / "static" / "napcat.html").read_text(encoding="utf-8")
    nav = html.split('<nav aria-label="控制台导航">')[1].split('</nav>')[0]
    everyday, advanced = nav.split('<details class="nav-advanced"')
    assert _pages(everyday) == ["status", "config:accounts", "sticker"]
    assert _pages(advanced) == ["config", "plugintools", "review", "logs", "guide"]
    assert " open" not in advanced.split("<summary")[0]
    for target in ("status.html", "open_platform.html"):
        assert f'data-nav="{target}"' in advanced
    for removed in ("prompts", "prompt_editor", "loadPromptEditorState", "_promptState", "gotoConfig", "快捷配置"):
        assert removed not in html
    for element in ("cfg-backend", "config-backend-process-controls", "backend-process-controls", "qr-img",
                    "backend-webui-link", "cfg-url", "cfg-token", "user-table", "group-table"):
        assert f'id="{element}"' in html
    assert 'layout.css?v=3' in html


def test_onebot_navigation_runs_and_preserves_connection_and_review_pages():
    import json
    import shutil
    import subprocess

    import pytest
    from _ui_source import fn_body

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    html = (pathlib.Path(__file__).resolve().parents[1] / "static" / "napcat.html").read_text(encoding="utf-8")
    # Parse all inline JS, not just the navigation functions exercised below.
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)
    pages = re.findall(r'id="(page-[^"]+)"', html)
    nav = html.split('<nav aria-label="控制台导航">')[1].split('</nav>')[0]
    entries = _pages(nav)
    functions = "\n".join("function " + signature + "{" + fn_body(html, name) + "}" for name, signature in (
        ("switchPage", "switchPage(page,sub)"), ("switchSub", "switchSub(sub)"), ("markNav", "markNav()")))
    listener = next(line for line in html.splitlines() if line.startswith("document.querySelectorAll('#sidebar"))
    harness = r"""
const assert = require('node:assert/strict');
function element() {
  const classes = new Set();
  return {style:{}, innerHTML:'', scrollTop:100, dataset:{},
    classList:{add(x){classes.add(x)}, remove(x){classes.delete(x)},
      toggle(x,on){if(on)classes.add(x);else classes.delete(x)}, contains(x){return classes.has(x)}},
    querySelectorAll(){return []}, addEventListener(type,fn){this[type]=fn}};
}
const els = Object.fromEntries(pageIds.map(id=>[id,element()]));
els.content=element(); els['topbar-tabs']=element();
const navs = entries.map(entry=>{
  const el=element(), parts=entry.split(':');
  el.dataset={page:parts[0]}; if(parts[1])el.dataset.sub=parts[1]; return el;
});
const external=element(); // data-nav links must not get an undefined page click handler.
const adv={open:false, querySelector(){return navs.slice(3).find(el=>el.classList.contains('active'))||null}};
els['nav-advanced']=adv;
global.document={getElementById(id){return els[id]||null},
  querySelectorAll(sel){
    if(sel==='#sidebar .nav-item[data-page]')return navs;
    if(sel==='#sidebar .nav-item')return [...navs,external];
    if(sel==='.page')return pageIds.map(id=>els[id]);
    if(sel==='.page.active')return pageIds.map(id=>els[id]).filter(el=>el.classList.contains('active'));
    return [];
  },
  querySelector(sel){const page=sel.match(/data-page="([^"]+)"/)[1], sub=sel.match(/data-sub="([^"]*)"/)[1];
    return navs.find(el=>el.dataset.page===page&&el.dataset.sub===sub)||null;}
};
let currentPage='status',currentSub='overview',_settingsDirty=true;
const calls=[];
const t=(key,fallback)=>fallback;
function loadDashboard(){calls.push('dashboard')} function updateGuide(){}
function loadStickers(){calls.push('stickers')} function loadBacklog(){calls.push('backlog')}
function loadGroupMemory(){calls.push('memory')} function loadUserProfiles(){calls.push('profiles')}
function loadLogs(){} function startLogPoll(){calls.push('log-start')} function stopLogPoll(){calls.push('log-stop')}
function loadPluginTools(){} function ptScheduleRefresh(){} function ptStopRefresh(){} function ccLoad(){}
"""
    checks = r"""
function shown(id){assert.ok(els['page-'+id].classList.contains('active'));
  assert.equal(pageIds.filter(id=>els[id].classList.contains('active')).length,1)}
function active(entry){assert.deepEqual(navs.filter(el=>el.classList.contains('active')).map(el=>
  el.dataset.page+(el.dataset.sub?':'+el.dataset.sub:'')),[entry])}
navs[1].click(); shown('config-accounts'); active('config:accounts');
assert.equal(adv.open,false); assert.equal(_settingsDirty,false);
switchSub('groups'); shown('config-groups'); active('config:accounts'); assert.equal(adv.open,false);
navs[3].click(); shown('config-connection'); active('config'); assert.equal(adv.open,true);
switchSub('params'); shown('config-params'); active('config');
navs[1].click(); assert.equal(adv.open,true,'keep manually opened Advanced');
adv.open=false;
navs[0].click(); shown('status-overview'); active('status');
switchSub('traces'); shown('status-traces'); active('status'); assert.equal(adv.open,false);
switchPage('review'); shown('review-overview'); active('review'); assert.equal(adv.open,true);
switchSub('memory'); shown('review-memory'); assert.ok(calls.includes('memory'));
switchSub('profiles'); shown('review-profiles'); assert.ok(calls.includes('profiles'));
switchPage('logs'); assert.equal(calls.at(-1),'log-start');
switchPage('sticker'); shown('sticker'); active('sticker'); assert.equal(calls.at(-1),'log-stop');
switchPage('guide'); shown('guide'); active('guide');
assert.equal(external.click,undefined);
"""
    result = subprocess.run(
        [node], input="const pageIds=" + json.dumps(pages) + ";const entries=" + json.dumps(entries) + ";"
         + "for(const script of " + json.dumps(scripts) + ")new Function(script);"
         + harness + functions + "\n" + listener + "\n" + checks,
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
