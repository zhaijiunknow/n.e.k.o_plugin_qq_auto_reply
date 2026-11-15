/* [stable-no-onebot-ui] 稳定版不提供 OneBot（NapCat / SnowLuma）的界面，但已接入的 OneBot 照常运行。

   这个文件只回答一件事：**这台机器上 OneBot 是不是在用**。答案决定两处行为 ——

   1. 在用：界面不许悄悄把连接方式改成开放平台（那会让下次启动连不上 QQ）。插件每次启动后
      第一次打开首页时弹一次窗（announceOnce）：说明 OneBot 的管理界面在测试版，并给一个
      「改用 QQ 开放平台」的选择。要改用开放平台，只有两个显式动作会切 —— 这个弹窗，或扫码绑定。
   2. 没在用（全新安装）：按开放平台的流程走，不提 OneBot。

   为什么不能只看已保存的连接方式：它的默认值就是 OneBot，全新安装也是。只看它的话，每个
   新用户都会被当成 OneBot 老用户。所以要有证据：部署过 / 连着 / 后端进程在 / 自动回复在跑 /
   开了开机自启，任一成立才算在用。

   本文件只在稳定版（main）上存在；保留 OneBot 界面的测试版分支没有它。 */
(function (global) {
    'use strict';

    var TEXT = {
        note: ['ui.stable.onebot_note',
            '检测到已接入的 OneBot（NapCat / SnowLuma）：它会继续按原配置运行。本版本不提供 OneBot 的部署、连接配置与登录界面，需要时请安装测试版。'],
        popup_switch_hint: ['ui.stable.popup_switch_hint',
            '开放平台控制台里的启动、停止与通用设置对它同样生效。\n\n也可以现在改用 QQ 开放平台：会停止 OneBot 连接和插件拉起的后端进程，且本版本的界面里没有切回 OneBot 的入口。'],
        popup_keep: ['ui.stable.popup_keep', '继续使用 OneBot'],
        popup_go_bind: ['ui.stable.popup_go_bind', '去扫码绑定'],
        popup_close: ['ui.stable.popup_close', '关闭'],
        manual_switch: ['ui.stable.manual_switch', '改用 QQ 开放平台'],
        manual_switch_done: ['ui.stable.manual_switch_done', '已改用 QQ 开放平台'],
        manual_switch_started: ['ui.stable.manual_switch_started', '，自动回复已按开放平台重新启动'],
        manual_switch_not_started: ['ui.stable.manual_switch_not_started',
            '，但自动回复没有启动：请先扫码绑定或填写 AppID 与密钥，再点启动'],
        manual_switch_failed: ['ui.stable.manual_switch_failed', '切换失败：'],
        switch_confirm: ['ui.stable.switch_confirm',
            '当前正在使用 OneBot（NapCat / SnowLuma）。扫码绑定完成后会改用 QQ 开放平台，并停止 OneBot 连接。继续吗？'],
        switch_ok: ['ui.stable.switch_ok', '继续扫码'],
        switch_cancelled: ['ui.stable.switch_cancelled', '已取消，OneBot 连接保持不变'],
        state_unknown: ['ui.stable.state_unknown', '读取当前连接状态失败，请刷新后重试'],
    };

    var SEEN_KEY = 'qq_onebot_notice_boot';

    function text(name) {
        var pair = TEXT[name];
        var i18n = global.I18n;
        return (i18n && typeof i18n.t === 'function') ? i18n.t(pair[0], pair[1]) : pair[1];
    }

    /** 已保存的连接方式是不是 OneBot（缺省值也是 OneBot）。 */
    function isOneBotMode(dashboard) {
        var settings = (dashboard || {}).settings || {};
        return String(settings.qq_connection_mode || 'napcat') !== 'open_platform';
    }

    /** OneBot 是不是在用：连接方式是 OneBot，并且有它真的被用着的证据。 */
    function inUse(dashboard) {
        if (!isOneBotMode(dashboard)) { return false; }
        var settings = (dashboard || {}).settings || {};
        var runtime = (dashboard || {}).runtime || {};
        return runtime.deployment_complete === true
            || runtime.onebot_connected === true
            || runtime.napcat_running === true
            || runtime.auto_reply_running === true
            || settings.auto_start_on_launch === true;
    }

    /** 改用开放平台。除了扫码绑定，这是唯一会改连接方式的地方；调用方负责先让人确认。

        先尽力关掉插件拉起的 OneBot 后端（不是插件管理的会报错，忽略），再切连接方式。
        原来在跑的话把自动回复按开放平台重新拉起来 —— 没填凭据时拉不起来，如实说。
        返回 ``{switched, message}``；``message`` 是给人看的一句结果。 */
    async function switchToOpenPlatform(call) {
        try {
            var before = await call('query', { action: 'dashboard' });
            var wasRunning = ((before || {}).runtime || {}).auto_reply_running === true;
            var backend = ((before || {}).settings || {}).onebot_backend || 'napcat';
            try { await call('runtime', { action: 'backend_stop', backend: backend }); } catch (ignored) { /* 手动 / 远程启动的后端不归插件管 */ }
            var result = await call('runtime', { action: 'set_mode', mode: 'open_platform' });
            var started = !!(result && result.restarted === true);
            if (wasRunning && !started) {
                try { await call('runtime', { action: 'start' }); started = true; } catch (ignored) { started = false; }
            }
            return { switched: true, message: text('manual_switch_done')
                + (started ? text('manual_switch_started') : (wasRunning ? text('manual_switch_not_started') : '')) };
        } catch (error) {
            return { switched: false, message: text('manual_switch_failed') + ((error && error.message) || error) };
        }
    }

    /** 首页用：插件每次启动后第一次打开首页，OneBot 在用就弹一次窗。

        "一次"按插件进程算：面板数据里的 runtime.boot_id 每次启动都不同，弹过的那个记在
        localStorage 里。同一次启动里再回首页不重复弹；插件重启后再弹。
        返回值只给测试看：not-in-use / already-shown / kept / switched / switch-failed / unavailable。 */
    async function announceOnce(call, storage, navigate) {
        var confirmer = global.UIConfirm;
        var dashboard;
        try { dashboard = await call('query', { action: 'dashboard' }); } catch (error) { return 'unavailable'; }
        if (!inUse(dashboard)) { return 'not-in-use'; }
        if (!confirmer) { return 'unavailable'; }
        var boot = String(((dashboard || {}).runtime || {}).boot_id || '');
        try { if (boot && storage.getItem(SEEN_KEY) === boot) { return 'already-shown'; } } catch (ignored) { /* 存储不可用就每次都弹 */ }
        try { if (boot) { storage.setItem(SEEN_KEY, boot); } } catch (ignored) { /* 同上 */ }
        var wantsSwitch = await confirmer.ask(text('note') + text('popup_switch_hint'),
            { danger: false, okText: text('manual_switch'), cancelText: text('popup_keep') });
        if (!wantsSwitch) { return 'kept'; }
        var result = await switchToOpenPlatform(call);
        var goBind = await confirmer.ask(result.message,
            { danger: false, okText: text('popup_go_bind'), cancelText: text('popup_close') });
        if (result.switched && goBind) { navigate('status.html'); }
        return result.switched ? 'switched' : 'switch-failed';
    }

    /** 首页没有别的页那套 call()（它不加载 SSE）：这里给一个只靠轮询的最小版本。 */
    async function run(entry, args) {
        var response = await fetch('/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ plugin_id: 'qq_auto_reply', entry_id: entry, args: args || {} }) });
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        var created = await response.json();
        var id = created.run_id || created.id;
        if (!id) { throw new Error('no run_id'); }
        var status = '';
        for (var attempt = 0; attempt < 240 && !status; attempt++) {   // 最多约 60 秒
            await new Promise(function (resolve) { setTimeout(resolve, 250); });
            var poll = await fetch('/runs/' + id);
            if (!poll.ok) { continue; }
            var record = await poll.json();
            if (['succeeded', 'failed', 'canceled', 'timeout'].indexOf(record.status) >= 0) { status = record.status; }
        }
        if (status !== 'succeeded') { throw new Error(status || 'timeout'); }
        var exported = await fetch('/runs/' + id + '/export');
        if (!exported.ok) { return {}; }
        var items = (await exported.json()).items || [];
        var item = items.find(function (i) { return i.type === 'json' && i.json; }) || items[0];
        if (!item) { return {}; }
        var raw = item.json || {};
        while (raw && raw.data && typeof raw.data === 'object' && ('success' in raw.data || 'error' in raw.data)) { raw = raw.data; }
        if (raw && raw.success && raw.data && typeof raw.data === 'object') { raw = raw.data; }
        return raw;
    }

    global.OneBotLegacy = { isOneBotMode: isOneBotMode, inUse: inUse, announceOnce: announceOnce,
        switchToOpenPlatform: switchToOpenPlatform, run: run, text: text };
})(window);
