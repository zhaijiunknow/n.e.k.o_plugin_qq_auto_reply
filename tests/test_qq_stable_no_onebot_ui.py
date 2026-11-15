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
3. **已接入 OneBot 的老用户不被悄悄改掉连接方式** —— 界面只提示一句，后端照跑；
   要改用开放平台，由扫码绑定这个显式动作来切（先确认）。

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


# ── 1. 首页：只有开放平台两张卡 + 一句给老用户的话 ─────────────────────

def test_index_has_no_onebot_or_legacy_entry():
    for target in ("napcat.html", "old.html"):
        assert f'data-nav="{target}"' not in INDEX, f"首页又出现了通往 {target} 的入口（稳定版不提供 OneBot 界面）"
        assert f'href="{target}"' not in INDEX, f"首页还链着 {target}"


def test_index_still_reaches_both_open_platform_pages():
    """扫码绑定在 status.html 上：这张卡一丢，开放平台就没法绑了。"""
    for target in ("status.html", "open_platform.html"):
        assert f'data-nav="{target}"' in INDEX, f"首页通往 {target} 的入口不见了"


def test_index_copy_does_not_offer_onebot_deployment():
    note = re.search(r'<p[^>]*id="onebot-channel-note"[^>]*>(.*?)</p>', INDEX, re.S)
    assert note, "首页给 OneBot 老用户的那句提示不见了"
    assert "测试版" in note.group(1) and "继续" in note.group(1), "提示要说清两件事：界面在测试版、已接入的照常运行"

    rest = _visible(INDEX.replace(note.group(0), ""))
    for banned in ("NapCat", "SnowLuma", "OneBot", "一键部署", "自动下载"):
        assert banned not in rest, f"首页（提示那句之外）还写着「{banned}」"


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


@pytest.mark.parametrize("page", ["status.html", "open_platform.html"])
def test_pages_load_what_the_legacy_handling_needs(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    for name in ("onebot-legacy.js", "ui-confirm.js"):
        assert re.search(rf'<script[^>]+src="{re.escape(name)}', html), f"{page} 没有加载 {name}"


# ── 4. 文案键两个语言包都有 ───────────────────────────────────────────

def test_new_copy_exists_in_both_bundles():
    keys = set(re.findall(r"'(ui\.stable\.[a-z_]+)'", LEGACY_JS))
    keys |= {"ui.index.bind.title", "ui.index.bind.desc1", "ui.index.bind.desc2", "ui.index.onebot_note",
             "ui.status.flow_title"}
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
// 提示：在用才显示；控制台里多说一句。
const el = {style:{}, textContent:''};
OneBotLegacy.renderNote(el, FRESH);
assert.equal(el.style.display, 'none');
OneBotLegacy.renderNote(el, DEPLOYED);
assert.equal(el.style.display, '');
assert.match(el.textContent, /测试版/);
const short = el.textContent;
OneBotLegacy.renderNote(el, DEPLOYED, true);
assert.ok(el.textContent.length > short.length && el.textContent.startsWith(short));
OneBotLegacy.renderNote(null, DEPLOYED);   // 页面上没有这个元素时不该抛
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
    """上面那条用的是替身；真函数必须每次拿到面板数据都先重算 keepOneBot 并刷新提示。"""
    body = _one_line_function(OPENPLAT, "applyDashboard")
    head = body[:body.index("let s=")]
    assert "state.keepOneBot=OneBotLegacy.inUse(d)" in head
    assert "OneBotLegacy.renderNote(document.getElementById('onebot-legacy-note'),d,!0)" in head
    assert 'id="onebot-legacy-note"' in OPENPLAT and 'id="onebot-legacy-note"' in STATUS


# ── 6. 后端能力不动：OneBot 的入口还在 ────────────────────────────────

def test_backend_still_offers_the_onebot_actions():
    source = (PLUGIN / "__init__.py").read_text(encoding="utf-8")
    for action in ('"one_click"', '"apply_onebot"', '"login_poll"', '"qrcode_sync"'):
        assert action in source, f"deploy 入口少了 {action} —— 稳定版只藏界面，不删 OneBot 能力"
    assert "backend_start" in source and "backend_stop" in source
    for page in ("napcat.html", "old.html"):
        assert (STATIC / page).is_file(), f"{page} 仍应随包发布（测试版分支保留入口，文件两边一致）"
