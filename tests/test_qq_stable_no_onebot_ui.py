# -*- coding: utf-8 -*-
"""**稳定版专用**（`main`）：界面不提供 OneBot（NapCat / SnowLuma），已接入的 OneBot 照常运行。

背景（2026-11-15）：NapCat / SnowLuma 的 release 已经不能稳定拉取，一键部署对新用户
不可靠；已经下载好的用户还能继续用一段时间。于是分成两个版本：

* **稳定版**（`main`）：界面只有 QQ 开放平台；
* **测试版**（`beta-onebot-ui` 分支）：保留 OneBot 的全部界面。

这个文件钉的是稳定版这一半，三件事缺一不可：

1. **OneBot 的入口不出现** —— 首页的 OneBot 卡与旧版控制中心、开放平台页侧栏的
   「OneBot 控制台」、接入页的连接方式按钮 / 后端选择 / 一键部署 / 登录二维码；
2. **开放平台那一整套仍可达** —— 扫码绑定就在接入页上，藏过头会把它一起关在门外；
3. **已接入 OneBot 的老用户不被悄悄改掉连接方式** —— 后端照跑；插件每次启动后第一次打开
   首页时弹一次窗说明情况，并可在弹窗里选择改用开放平台。会改连接方式的只有这个弹窗和
   扫码绑定两个显式动作。

两条刻意的边界：

* **只藏不删**：接入页那些 OneBot 元素留在 DOM 里，页内脚本会无条件读写它们，删掉会让
  整页开页即抛；`napcat.html` / `old.html` 仍随包发布，只是没有入口。
* **后端不动**：`deploy` / `runtime` 里 OneBot 的能力一行没少。

测试版分支没有这个文件（它断言的恰好是测试版要保留的东西）；两条分支怎么同步见 `AGENTS.md`。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
STATIC = PLUGIN / "static"

INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
STATUS = (STATIC / "status.html").read_text(encoding="utf-8")
OPENPLAT = (STATIC / "open_platform.html").read_text(encoding="utf-8")
LEGACY_JS = (STATIC / "onebot-legacy.js").read_text(encoding="utf-8")


def _visible(html: str) -> str:
    """去掉 HTML 注释与 <script>/<style>：剩下的才是会画到屏幕上的。"""
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    return re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)


# ── 1. 首页：只有开放平台两张卡，居中 ─────────────────────────────────

def test_index_has_no_onebot_or_legacy_entry():
    for target in ("napcat.html", "old.html"):
        assert f'data-nav="{target}"' not in INDEX, f"首页又出现了通往 {target} 的入口（稳定版不提供 OneBot 界面）"
        assert f'href="{target}"' not in INDEX, f"首页还链着 {target}"


def test_index_still_reaches_both_open_platform_pages():
    """扫码绑定在 status.html 上：这张卡一丢，开放平台就没法绑了。"""
    for target in ("status.html", "open_platform.html"):
        assert f'data-nav="{target}"' in INDEX, f"首页通往 {target} 的入口不见了"


def test_index_copy_does_not_mention_onebot_at_all():
    """首页不提 OneBot（使用者 2026-11-15：那句"管理界面在测试版"省略）。

    给老用户的提示只在接入页和控制台里、并且只在 OneBot 真的在用时才出现。
    """
    body = _visible(INDEX)
    for banned in ("NapCat", "SnowLuma", "OneBot", "一键部署", "自动下载", "测试版", "接入方式"):
        assert banned not in body, f"首页还写着「{banned}」"
    assert "onebot-channel-note" not in INDEX


def test_index_title_is_not_a_mode_picker_any_more():
    """只剩一种接入方式，标题不能还叫「QQ 接入方式」；语言包里的值会盖掉页面上的字，要一起改。"""
    assert re.search(r'<h1 data-i18n="ui\.index\.title">QQ 集成</h1>', INDEX)
    for name, expected in (("zh-CN.json", "QQ 集成"), ("en.json", "QQ Integration")):
        bundle = json.loads((PLUGIN / "i18n" / name).read_text(encoding="utf-8"))
        assert bundle["ui.index.title"] == expected, f"{name} 的首页标题还是旧的"


def test_index_cards_are_laid_out_as_two_centered_columns():
    """layout.css 给首页排三列，两张卡会靠左空出一列 —— 首页自己改成两列并居中。"""
    style = re.sub(r"/\*.*?\*/", "", "\n".join(re.findall(r"<style>(.*?)</style>", INDEX, re.S)), flags=re.S)
    rule = re.search(r"body\.home-layout \.wrap\s*\{([^}]*)\}", style)
    assert rule, "首页没有覆写 .wrap 的列数"
    declarations = rule.group(1).replace(" ", "")
    assert "grid-template-columns:repeat(2," in declarations and "justify-content:center" in declarations
    assert len(re.findall(r'<a class="card"', INDEX)) == 2, "卡片数和两列的排法对不上了"


# ── 2. 开放平台控制台：没有回 OneBot 的入口 ───────────────────────────

def test_open_platform_console_has_no_onebot_entry():
    body = _visible(OPENPLAT)
    assert 'data-nav="napcat.html"' not in body, "开放平台页侧栏又出现了「OneBot 控制台」"
    assert "一键部署" not in body, "开放平台页里还有指向「一键部署」的文案"
    assert 'data-nav="status.html"' in body, "开放平台页通往扫码绑定页的入口不见了"


# ── 3. 接入页：OneBot 那套藏起来（留在 DOM），开放平台那套始终显示 ─────

_STYLE = "\n".join(re.findall(r"<style>(.*?)</style>", STATUS, re.S))


def _rule_for(selector: str) -> str:
    """返回选择器列表里含 ``selector`` 的那条规则的声明块。"""
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", _STYLE, flags=re.S)):
        if selector in [item.strip() for item in selectors.split(",")]:
            if "!important" in body:
                return body.replace(" ", "")
    return ""


@pytest.mark.parametrize("selector", ["#modes", "#onebot-backend-section", "#mode-hint-napcat", "#sec-napcat", "#btn-qr"])
def test_status_hides_the_onebot_controls(selector):
    assert "display:none!important" in _rule_for(selector), (
        f"{selector} 没有被 display:none !important 藏住 —— 脚本会按连接方式把它显示回来"
    )


@pytest.mark.parametrize("selector", ["#sec-openplat", "#mode-hint-openplat"])
def test_status_always_shows_the_open_platform_flow(selector):
    """全新安装的缺省连接方式是 OneBot，脚本会把开放平台这两块藏起来 —— 稳定版必须强制显示。"""
    rule = _rule_for(selector)
    assert "!important" in rule and "display:none" not in rule, f"{selector} 没有被强制显示"


@pytest.mark.parametrize("marker", [
    'id="sec-napcat"', 'id="btn-deploy"', 'id="btn-apply"', 'id="deploy-log"', 'id="uin"',
    'id="onebot-backend"', 'id="backend-login-card"', 'id="qr"', 'id="btn-qr"', 'id="autostart"',
    'data-mode="napcat"', 'id="mode-hint-napcat"',
])
def test_status_keeps_the_hidden_elements_in_the_dom(marker):
    assert marker in STATUS, f"{marker} 被删掉了 —— 页内脚本无条件读写它，删掉会让整页开页即抛"


def test_status_binding_section_is_intact():
    section = STATUS[STATUS.index('<div id="sec-openplat"'):]
    for marker in ('id="btn-bind"', 'id="bind-status"', 'id="bind-qr"', 'id="bind-qr-img"'):
        assert marker in section, f"开放平台扫码绑定那块缺了 {marker}"
    assert "getElementById('btn-bind').addEventListener('click', startBind)" in STATUS


def test_status_title_never_says_one_click_deploy():
    assert "<title>QQ 开放平台接入</title>" in STATUS
    assert "QQ 一键部署" not in re.sub(r"<!--.*?-->|/\*.*?\*/", "", STATUS, flags=re.S), (
        "接入页的标题还会变成「QQ 一键部署」"
    )


def test_trust_list_and_sticker_upload_wait_for_the_open_platform_connection():
    """信任名单与表情包上传：扫完码、开放平台连上之后才显示。

    脚本原有的判据还认"OneBot 部署完成"，于是已接入 OneBot 的老用户一打开这页就看到这两张卡。
    """
    style = re.sub(r"/\*.*?\*/", "", _STYLE, flags=re.S).replace(" ", "")
    for card in ("#trust-list-card", "#sticker-upload-card"):
        assert f'#sec-openplat:not([data-connected="true"])~{card}' in style, f"{card} 没有挂在「已连上」这个标记上"
    assert ("document.getElementById('sec-openplat').dataset.connected = "
            "String(!OneBotLegacy.isOneBotMode(d) && deploymentReady(d));") in STATUS
    # 兄弟选择器要成立：两张卡必须排在 #sec-openplat 之后、且是同一层。
    order = [STATUS.index(marker) for marker in ('<div id="sec-openplat"', 'id="trust-list-card"', 'id="sticker-upload-card"')]
    assert order == sorted(order)


@pytest.mark.parametrize("page", ["index.html", "status.html", "open_platform.html"])
def test_pages_load_what_the_legacy_handling_needs(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    for name in ("onebot-legacy.js", "ui-confirm.js"):
        assert re.search(rf'<script[^>]+src="{re.escape(name)}', html), f"{page} 没有加载 {name}"


# ── 4. 文案键两个语言包都有 ───────────────────────────────────────────

def test_new_copy_exists_in_both_bundles():
    keys = set(re.findall(r"'(ui\.stable\.[a-z_]+)'", LEGACY_JS))
    keys |= {"ui.index.bind.title", "ui.index.bind.desc1", "ui.index.bind.desc2", "ui.status.flow_title"}
    assert len(keys) >= 10, "没从 onebot-legacy.js 里认出文案键 —— 正则跟文件对不上了"
    for name in ("zh-CN.json", "en.json"):
        bundle = json.loads((PLUGIN / "i18n" / name).read_text(encoding="utf-8"))
        missing = sorted(key for key in keys if not bundle.get(key))
        assert not missing, f"{name} 缺这些键: {missing}"


# ── 5. 行为：老用户不被改连接方式，新用户照开放平台走 ─────────────────

def _node(code: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to execute frontend behavior tests")
    result = subprocess.run([node, "-"], input="globalThis.window = globalThis;\n" + LEGACY_JS + code,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def _one_line_function(html: str, name: str) -> str:
    """开放平台页的函数都压在一行里：取 ``function name(`` 所在的那一整行。"""
    match = re.search(rf"^(?:async )?function {re.escape(name)}\(.*$", html, re.M)
    assert match, f"open_platform.html 里找不到 {name}()"
    return match.group(0)


def _block_function(html: str, name: str) -> str:
    start = html.index(f"async function {name}(")
    return html[start:html.index("\n}", start) + 2]


_IN_USE_PAYLOADS = """
const FRESH = {settings:{}, runtime:{}};
const OPEN = {settings:{qq_connection_mode:'open_platform', auto_start_on_launch:true},
              runtime:{onebot_connected:true, auto_reply_running:true}};
const DEPLOYED = {settings:{qq_connection_mode:'napcat'}, runtime:{deployment_complete:true}};
"""


def test_in_use_means_onebot_mode_plus_evidence():
    _node(_IN_USE_PAYLOADS + r"""
const assert = require('node:assert/strict');
// 全新安装：连接方式缺省就是 OneBot，但没有任何"在用"的证据。
assert.equal(OneBotLegacy.isOneBotMode(FRESH), true);
assert.equal(OneBotLegacy.inUse(FRESH), false);
assert.equal(OneBotLegacy.inUse({}), false);
assert.equal(OneBotLegacy.inUse(undefined), false);
// 开放平台：这些信号再亮也不算 OneBot 在用（onebot_connected 两个通道共用）。
assert.equal(OneBotLegacy.inUse(OPEN), false);
// OneBot + 任一证据。
for (const [where, key] of [['runtime','deployment_complete'], ['runtime','onebot_connected'],
    ['runtime','napcat_running'], ['runtime','auto_reply_running'], ['settings','auto_start_on_launch']]) {
  for (const mode of ['napcat', 'napcat_forward', undefined]) {
    const d = {settings:{qq_connection_mode:mode}, runtime:{}};
    d[where][key] = true;
    assert.equal(OneBotLegacy.inUse(d), true, key + ' / ' + mode);
    d[where][key] = 'yes';   // 只认布尔真：别的值不是证据
    assert.equal(OneBotLegacy.inUse(d), false, key + ' as string');
  }
}
""")


def test_binding_asks_before_replacing_a_running_onebot_setup():
    _node(_IN_USE_PAYLOADS + r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {style:{}, textContent:'', src:''});
  return elements.get(id);
}};
let dashboard, answer, asked, calls, failDashboard;
const UIConfirm = {ask: async () => { asked += 1; return answer; }};
async function call(entry, args) {
  calls.push(args.action);
  if (args.action === 'dashboard') { if (failDashboard) throw Error('offline'); return dashboard; }
  return {qrcode_url:'/q.png', interval:2};
}
let bindTimer = null;
function stopBindPoll() {}
function bindStatus(msg) { document.getElementById('bind-status').textContent = msg || ''; }
globalThis.setInterval = () => 1;
function reset(d, yes) { dashboard = d; answer = yes; asked = 0; calls = []; failDashboard = false; }
""" + _block_function(STATUS, "startBind") + r"""
(async () => {
  // 老用户点了取消：不申请绑定任务，OneBot 原样。
  reset(DEPLOYED, false);
  await startBind();
  assert.equal(asked, 1);
  assert.deepEqual(calls, ['dashboard']);
  assert.match(document.getElementById('bind-status').textContent, /保持不变/);
  // 老用户确认：才开始绑定。
  reset(DEPLOYED, true);
  await startBind();
  assert.equal(asked, 1);
  assert.deepEqual(calls, ['dashboard', 'bind_start']);
  // 全新安装 / 已经是开放平台：不问，直接绑。
  for (const d of [FRESH, OPEN]) {
    reset(d, false);
    await startBind();
    assert.equal(asked, 0);
    assert.deepEqual(calls, ['dashboard', 'bind_start']);
  }
  // 状态读不到：不在没确认的情况下往下走。
  reset(DEPLOYED, true);
  failDashboard = true;
  await startBind();
  assert.deepEqual(calls, ['dashboard']);
  assert.match(document.getElementById('bind-status').textContent, /重试/);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_the_console_never_flips_a_running_onebot_setup_to_the_open_platform():
    """进控制台、点启动、点保存设置 —— 老用户哪一步都不该带上 qq_connection_mode。"""
    functions = "\n".join(_one_line_function(OPENPLAT, name) for name in ("modeArgs", "doSave", "doStart", "bootstrap"))
    _node(_IN_USE_PAYLOADS + r"""
const assert = require('node:assert/strict');
const MODE = 'open_platform';
let state = {};
const document = {getElementById() { return {value:'', checked:false, style:{}}; }};
function t(key, fallback) { return fallback; }
function toast() {}
let dashboard, calls;
async function call(entry, args) { calls.push({entry, ...args}); return entry === 'query' ? dashboard : {}; }
// 只取真实 applyDashboard 里与本测试有关的那一句（整个函数要一整页 DOM）。
function applyDashboard(d) { state.keepOneBot = OneBotLegacy.inUse(d); }
async function loadDashboard() { applyDashboard(dashboard); }
const modes = () => calls.filter(c => 'qq_connection_mode' in c).map(c => c.entry + ':' + c.action);
""" + functions + r"""
(async () => {
  // 老用户：三个动作都不带连接方式；启动 / 保存本身照常发出去。
  dashboard = DEPLOYED; calls = []; state = {};
  await bootstrap();
  assert.deepEqual(calls.map(c => c.action), ['dashboard']);
  await doStart();
  await doSave();
  assert.deepEqual(modes(), []);
  assert.ok(calls.some(c => c.entry === 'runtime' && c.action === 'start'));
  assert.ok(calls.some(c => c.entry === 'config' && c.action === 'save' && 'qq_open_app_id' in c));

  // 全新安装：和以前一样，进页就把连接方式定成开放平台。
  dashboard = FRESH; calls = []; state = {};
  await bootstrap();
  assert.deepEqual(calls.map(c => c.action), ['dashboard', 'save', 'dashboard']);
  assert.equal(calls[1].qq_connection_mode, 'open_platform');
  calls = [];
  await doStart();
  await doSave();
  assert.deepEqual(modes(), ['config:save', 'config:save']);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_the_real_apply_dashboard_refreshes_the_legacy_state_first():
    """上面那条用的是替身；真函数必须每次拿到面板数据都先重算 keepOneBot。"""
    body = _one_line_function(OPENPLAT, "applyDashboard")
    assert "state.keepOneBot=OneBotLegacy.inUse(d)" in body[:body.index("let s=")]


def test_the_notice_lives_only_in_the_index_popup():
    """给老用户的提示是首页的一次性弹窗，接入页和控制台里不再有常驻提示条。"""
    for name, html in (("status.html", STATUS), ("open_platform.html", OPENPLAT)):
        for leftover in ("onebot-legacy-note", "btn-switch-openplat", "renderNote", "doSwitchToOpenPlatform"):
            assert leftover not in html, f"{name} 里还留着提示条的 {leftover}"
    assert "OneBotLegacy.announceOnce(OneBotLegacy.run, window.localStorage, Nav.go)" in INDEX
    assert "'i18n-ready'" in INDEX, "弹窗要等语言包就绪再弹"
    runtime = (PLUGIN / "runtime_service.py").read_text(encoding="utf-8")
    assert "BOOT_ID = uuid.uuid4().hex" in runtime and '"boot_id": BOOT_ID' in runtime, (
        "面板数据里没有 boot_id —— 弹窗分不清这次启动之后弹过没有"
    )


_ANNOUNCE_HARNESS = r"""
const assert = require('node:assert/strict');
let answers, asked, calls, dashboard, behaviour, went;
globalThis.UIConfirm = {ask: async (message, opts) => { asked.push({message, opts}); return answers.shift(); }};
const store = new Map();
const storage = {getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v))};
async function call(entry, args) {
  calls.push(entry + ':' + args.action + (args.mode ? '=' + args.mode : '') + (args.backend ? '=' + args.backend : ''));
  if (args.action === 'dashboard') { if (behaviour.dashboard instanceof Error) throw behaviour.dashboard; return dashboard; }
  if (behaviour[args.action] instanceof Error) throw behaviour[args.action];
  return behaviour[args.action] || {};
}
const navigate = target => went.push(target);
const boot = (id, extra) => ({settings:{qq_connection_mode:'napcat', onebot_backend:'snowluma', ...(extra || {}).settings},
                             runtime:{boot_id:id, auto_reply_running:true, ...(extra || {}).runtime}});
// 插件市场桥的替身：默认"没有可更新的测试版"。
let found, installResult, installs;
const bridge = {findBeta: async () => found, install: async f => { installs.push(f); return installResult; }};
function reset(d, replies, b) {
  dashboard = d; answers = replies.slice(); asked = []; calls = []; behaviour = b || {}; went = [];
  found = {status:'none'}; installResult = {ok:true, error:''}; installs = [];
}
const announce = () => OneBotLegacy.announceOnce(call, storage, navigate, bridge);
"""


def test_the_popup_shows_once_per_plugin_start_and_only_for_onebot_users():
    _node(_ANNOUNCE_HARNESS + r"""
(async () => {
  // 全新安装 / 已是开放平台：不弹。
  reset({settings:{}, runtime:{boot_id:'b0'}}, []);
  assert.equal(await announce(), 'not-in-use');
  reset({settings:{qq_connection_mode:'open_platform'}, runtime:{boot_id:'b0', onebot_connected:true}}, []);
  assert.equal(await announce(), 'not-in-use');
  assert.equal(asked.length, 0);

  // 老用户，这次启动第一次打开首页：弹；选「继续使用 OneBot」不动连接（接着那个弹窗是测试版的事）。
  reset(boot('b1'), [false]);
  assert.equal(await announce(), 'kept');
  assert.equal(asked.length, 2);
  assert.match(asked[0].message, /测试版/);
  assert.equal(asked[0].opts.okText, OneBotLegacy.text('manual_switch'));
  assert.equal(asked[0].opts.cancelText, OneBotLegacy.text('popup_keep'));
  assert.equal(asked[0].opts.danger, false);
  assert.deepEqual(calls, ['query:dashboard']);

  // 同一次启动里再回首页：不重复弹。
  reset(boot('b1'), [true]);
  assert.equal(await announce(), 'already-shown');
  assert.equal(asked.length, 0);

  // 插件重启（boot_id 变了）：再弹一次。
  reset(boot('b2'), [false]);
  assert.equal(await announce(), 'kept');
  assert.equal(asked.length, 2);

  // 面板读不到：不弹、也不记"弹过"，下次打开还有机会。
  reset(boot('b3'), [false], {dashboard: Error('offline')});
  assert.equal(await announce(), 'unavailable');
  assert.equal(asked.length, 0);
  reset(boot('b3'), [false]);
  assert.equal(await announce(), 'kept');

  // 旧后端不带 boot_id：分不清就每次都弹，不能因此一次都不弹。
  reset(boot(undefined), [false]);
  assert.equal(await announce(), 'kept');
  reset(boot(undefined), [false]);
  assert.equal(await announce(), 'kept');

  // 存储不可用（隐私模式）：照弹。
  const broken = {getItem() { throw Error('denied'); }, setItem() { throw Error('denied'); }};
  reset(boot('b9'), [false]);
  assert.equal(await OneBotLegacy.announceOnce(call, broken, navigate, bridge), 'kept');
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_keeping_onebot_points_at_the_beta_and_can_update_to_it():
    """选「继续使用 OneBot」之后要指到测试版，能直接更新就直接更新（使用者 2026-11-16）。"""
    _node(_ANNOUNCE_HARNESS + r"""
const RELEASE = {version:'0.16.0-beta.1', channel:'beta', package_url:'https://x/p', package_sha256:'s'};
(async () => {
  // 市场上有测试版：问一句；确认 → 走升级 → 结束后刷新首页。
  reset(boot('k1'), [false, true, true]);
  found = {status:'ok', release:RELEASE, token:'T', marketId:'52'};
  assert.equal(await announce(), 'beta-updated');
  assert.match(asked[1].message, /测试版 0\.16\.0-beta\.1/);
  assert.equal(asked[1].opts.okText, OneBotLegacy.text('beta_update'));
  assert.equal(asked[1].opts.cancelText, OneBotLegacy.text('beta_later'));
  assert.equal(installs.length, 1);
  assert.equal(installs[0].release, RELEASE);
  assert.match(asked[2].message, /已更新到测试版 0\.16\.0-beta\.1/);
  assert.deepEqual(went, ['index.html']);
  assert.deepEqual(calls, ['query:dashboard'], '更新到测试版不该碰连接方式');

  // 选「暂不更新」：什么都不装。
  reset(boot('k2'), [false, false]);
  found = {status:'ok', release:RELEASE, token:'T', marketId:'52'};
  assert.equal(await announce(), 'kept');
  assert.deepEqual([installs.length, went], [0, []]);

  // 更新失败：如实报原因，不刷新、不说成功。
  reset(boot('k3'), [false, true, false]);
  found = {status:'ok', release:RELEASE, token:'T', marketId:'52'};
  installResult = {ok:false, error:'sha256 mismatch'};
  assert.equal(await announce(), 'beta-failed');
  assert.match(asked[2].message, /更新到测试版失败.*sha256 mismatch/);
  assert.deepEqual(went, []);

  // 没有可直接更新的测试版（还没发布 / 不是从市场装的 / 市场连不上）：说清楚并指到插件市场。
  for (const status of ['none', 'unavailable']) {
    reset(boot('k4-' + status), [false, true]);
    found = {status};
    assert.equal(await announce(), 'kept');
    assert.match(asked[1].message, /测试版/);
    assert.match(asked[1].message, /插件市场/);
    assert.equal(installs.length, 0);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_the_beta_to_update_to_is_never_older_than_what_is_installed():
    """市场的版本列表里有很久以前的测试版（0.15.2-beta.1 等）：不能把 0.16.0 "更新"到它们上面。"""
    _node(r"""
const assert = require('node:assert/strict');
const row = (version, extra) => ({channel:'beta', version, package_url:'u', package_sha256:'s', ...extra});
const rows = [row('0.15.2-beta.1'), row('0.14.11-beta.1'), row('0.16.0-beta.1'), row('0.16.0-beta.2', {yanked_at:'2026-11-16'}),
              row('0.16.0-beta.3', {package_sha256:''}), {channel:'stable', version:'0.17.0', package_url:'u', package_sha256:'s'},
              row('nonsense')];
const pick = (list, installed) => (OneBotLegacy.pickBeta(list, installed) || {}).version;
assert.equal(pick(rows, '0.16.0'), '0.16.0-beta.1');           // 同号的测试版可以；下架的、缺校验和的不行
assert.equal(pick(rows, '0.15.6'), '0.16.0-beta.1');
assert.equal(pick(rows.slice(0, 2), '0.16.0'), undefined);       // 只有旧测试版：不挑
assert.equal(pick(rows, '0.16.1'), undefined);                    // 已装的更新：不往回装
assert.equal(pick([row('0.16.0-beta.1'), row('0.16.0-beta.10'), row('0.16.0-beta.9')], '0.16.0'), '0.16.0-beta.10');
assert.equal(pick([row('0.16.0-beta.5'), row('0.17.0-beta.1')], '0.16.0'), '0.17.0-beta.1');
assert.equal(pick(rows, 'weird'), undefined);                     // 已装版本认不出来：宁可不更新
assert.equal(pick(undefined, '0.16.0'), undefined);
""")


def test_the_market_bridge_upgrade_request_carries_what_the_host_checks():
    """走的是本体的 /market/install：mode=upgrade、测试通道、包地址与校验和都要带上。"""
    _node(r"""
const assert = require('node:assert/strict');
const seen = [];
let taskPolls = 0;
globalThis.setTimeout = fn => { fn(); return 0; };
globalThis.fetch = async (url, options) => {
  seen.push({url, options});
  const body = url.startsWith('/market/bridge-token') ? {bridge_token:'TOK EN'}
    : url.startsWith('/market/installed') ? {installed:[
        {plugin_id:'other', market_installed:true, latest_install_source:{plugin_market_id:'1'}},
        {plugin_id:'qq_auto_reply', market_installed:true, effective_version:'0.16.0', latest_install_source:{plugin_market_id:'52'}}]}
    : url.startsWith('/market/catalog') ? [
        {channel:'beta', version:'0.16.0-beta.1', package_url:'https://x/p.neko-plugin', package_sha256:'abc', payload_hash:'ph', created_at:'2026-11-16T00:00:00Z'}]
    : url.startsWith('/market/install') ? {task_id:'t 1', status:'pending'}
    : url.startsWith('/market/tasks/') ? {status: ++taskPolls < 3 ? 'installing' : 'completed'}
    : null;
  return {ok: body !== null, status: body === null ? 404 : 200, json: async () => body};
};
(async () => {
  const found = await OneBotLegacy.market.findBeta();
  assert.equal(found.status, 'ok');
  assert.deepEqual([found.marketId, found.release.version], ['52', '0.16.0-beta.1']);
  assert.ok(seen.some(c => c.url === '/market/catalog/api/v1/plugins/52/versions'));
  assert.ok(seen.some(c => c.url === '/market/installed?token=TOK%20EN'));

  const result = await OneBotLegacy.market.install(found);
  assert.deepEqual(result, {ok:true, error:''});
  const post = seen.find(c => c.url.startsWith('/market/install?'));
  assert.equal(post.url, '/market/install?token=TOK%20EN');
  assert.equal(post.options.method, 'POST');
  assert.deepEqual(JSON.parse(post.options.body), {
    package_url:'https://x/p.neko-plugin', package_sha256:'abc', payload_hash:'ph', plugin_id:'52',
    version:'0.16.0-beta.1', channel:'beta', published_at:'2026-11-16T00:00:00Z',
    mode:'upgrade', expected_plugin_toml_id:'qq_auto_reply'});
  assert.ok(seen.some(c => c.url === '/market/tasks/t%201?token=TOK%20EN'));
  assert.equal(taskPolls, 3);

  // 不是从市场装的：不去查版本，直接说不可用。
  seen.length = 0;
  const original = globalThis.fetch;
  globalThis.fetch = async url => url.startsWith('/market/installed')
    ? {ok:true, json: async () => ({installed:[{plugin_id:'qq_auto_reply', market_installed:false}]})} : original(url);
  assert.deepEqual(await OneBotLegacy.market.findBeta(), {status:'unavailable'});
  assert.ok(!seen.some(c => c.url.startsWith('/market/catalog')));

  // 市场桥连不上：不抛，说不可用。
  globalThis.fetch = async () => { throw Error('offline'); };
  assert.deepEqual(await OneBotLegacy.market.findBeta(), {status:'unavailable'});
  // 升级任务失败：把原因带回来。
  globalThis.fetch = async url => ({ok:true, json: async () => url.startsWith('/market/tasks/')
    ? {status:'failed', error:'package sha256 mismatch'} : {task_id:'t2'}});
  assert.deepEqual(await OneBotLegacy.market.install(found), {ok:false, error:'package sha256 mismatch'});
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_switching_from_the_popup_stops_onebot_and_reports_honestly():
    """弹窗里选「改用 QQ 开放平台」：关后端 → 切连接方式；原来在跑就按开放平台重新拉起来，
    拉不起来要如实说（真机反馈 2026-11-15 23:06：填了凭据点启动，连上的还是 OneBot）。"""
    _node(_ANNOUNCE_HARNESS + r"""
(async () => {
  // 在跑 + 凭据可用：服务端顺手重连成功；第二个弹窗选「去扫码绑定」就跳过去。
  reset(boot('s1'), [true, true], {set_mode: {status:'reconnected', restarted:true}});
  assert.equal(await announce(), 'switched');
  assert.deepEqual(calls, ['query:dashboard', 'query:dashboard', 'runtime:backend_stop=snowluma', 'runtime:set_mode=open_platform']);
  assert.match(asked[1].message, /重新启动/);
  assert.deepEqual(went, ['status.html']);

  // 后端不归插件管（手动 / 远程启动）：backend_stop 报错不拦切换；选「关闭」不跳转。
  reset(boot('s2'), [true, false], {backend_stop: Error('NOT_MANAGED'), set_mode: {restarted:true}});
  assert.equal(await announce(), 'switched');
  assert.ok(calls.includes('runtime:set_mode=open_platform'));
  assert.deepEqual(went, []);

  // 在跑 + 没填凭据：重连失败，再试一次启动也失败 —— 连接方式已经切了，但要说清没启动。
  reset(boot('s3'), [true, false], {set_mode: {status:'reconnect_failed', restarted:false}, start: Error('no credentials')});
  assert.equal(await announce(), 'switched');
  assert.deepEqual(calls.slice(-2), ['runtime:set_mode=open_platform', 'runtime:start']);
  assert.match(asked[1].message, /没有启动/);

  // 没在跑：只切连接方式，不顺手把自动回复拉起来。
  reset(boot('s4', {runtime:{auto_reply_running:false, deployment_complete:true}}), [true, false], {set_mode: {status:'saved', restarted:false}});
  assert.equal(await announce(), 'switched');
  assert.ok(!calls.includes('runtime:start'));
  assert.equal(asked[1].message, OneBotLegacy.text('manual_switch_done'));

  // 切换本身失败：如实报，不说成功，也不往扫码页跳。
  reset(boot('s5'), [true, true], {set_mode: Error('DEPLOY_BUSY')});
  assert.equal(await announce(), 'switch-failed');
  assert.match(asked[1].message, /切换失败.*DEPLOY_BUSY/);
  assert.deepEqual(went, []);

  // 没有确认框可用（脚本没加载）：宁可不动。
  delete globalThis.UIConfirm;
  reset(boot('s6'), [true]);
  assert.equal(await announce(), 'unavailable');
  assert.deepEqual(calls, ['query:dashboard']);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


# ── 6. 后端能力不动：OneBot 的入口还在 ────────────────────────────────

def test_backend_still_offers_the_onebot_actions():
    source = (PLUGIN / "__init__.py").read_text(encoding="utf-8")
    for action in ('"one_click"', '"apply_onebot"', '"login_poll"', '"qrcode_sync"'):
        assert action in source, f"deploy 入口少了 {action} —— 稳定版只藏界面，不删 OneBot 能力"
    assert "backend_start" in source and "backend_stop" in source
    for page in ("napcat.html", "old.html"):
        assert (STATIC / page).is_file(), f"{page} 仍应随包发布（测试版分支保留入口，文件两边一致）"
