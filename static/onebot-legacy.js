/* [stable-no-onebot-ui] 稳定版不提供 OneBot（NapCat / SnowLuma）的界面，但已接入的 OneBot 照常运行。

   这个文件只回答一件事：**这台机器上 OneBot 是不是在用**。答案决定两处行为 ——

   1. 在用：界面不许悄悄把连接方式改成开放平台（那会让下次启动连不上 QQ），只提示一句
      "OneBot 的管理界面在测试版"；要改用开放平台，只有两个显式动作会切 —— 扫码绑定，
      或提示条上的「改用 QQ 开放平台」按钮（手填 AppID / 密钥的人没有扫码这一步）。
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
        note_console: ['ui.stable.onebot_note_console',
            '这里的启动、停止与通用设置对它同样生效。'],
        manual_switch: ['ui.stable.manual_switch', '改用 QQ 开放平台'],
        manual_switch_confirm: ['ui.stable.manual_switch_confirm',
            '当前正在使用 OneBot（NapCat / SnowLuma）。切换后会停止 OneBot 连接和插件拉起的后端进程，改用 QQ 开放平台；本版本的界面里没有切回 OneBot 的入口。继续吗？'],
        manual_switch_ok: ['ui.stable.manual_switch_ok', '切换'],
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

    /** 在用时显示提示，否则藏起来。``inConsole`` 为真时多说一句控制台里哪些还能用。

        文案写进提示条的第一个子元素（页面上是一个 span，后面跟着「改用 QQ 开放平台」按钮）；
        没有子元素时写进提示条自己。不能整条用 textContent 重写 —— 面板每 5 秒刷新一次，
        那样会把按钮一起抹掉。 */
    function renderNote(element, dashboard, inConsole) {
        if (!element) { return; }
        var on = inUse(dashboard);
        element.style.display = on ? '' : 'none';
        if (!on) { return; }
        var label = element.firstElementChild || element;
        label.textContent = text('note') + (inConsole ? text('note_console') : '');
        var button = element.querySelector ? element.querySelector('button') : null;
        if (button) { button.textContent = text('manual_switch'); }
    }

    /** 老用户主动改用开放平台（提示条上的按钮）。除了扫码绑定，这是唯一会改连接方式的地方。

        先确认；再尽力关掉插件拉起的 OneBot 后端（不是插件管理的会报错，忽略）；然后切连接方式。
        原来在跑的话把自动回复按开放平台重新拉起来 —— 没填凭据时拉不起来，如实说。
        返回 ``{switched, message}``；``message`` 是给人看的一句结果。 */
    async function switchToOpenPlatform(call) {
        var confirmer = global.UIConfirm;
        if (!confirmer || !await confirmer.ask(text('manual_switch_confirm'),
                { danger: false, okText: text('manual_switch_ok') })) {
            return { switched: false, message: '' };
        }
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

    global.OneBotLegacy = { isOneBotMode: isOneBotMode, inUse: inUse, renderNote: renderNote,
        switchToOpenPlatform: switchToOpenPlatform, text: text };
})(window);
