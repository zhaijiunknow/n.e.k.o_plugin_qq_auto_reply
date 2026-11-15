/* [stable-no-onebot-ui] 稳定版不提供 OneBot（NapCat / SnowLuma）的界面，但已接入的 OneBot 照常运行。

   这个文件只回答一件事：**这台机器上 OneBot 是不是在用**。答案决定两处行为 ——

   1. 在用：界面不许悄悄把连接方式改成开放平台（那会让下次启动连不上 QQ），只提示一句
      "OneBot 的管理界面在测试版"；要改用开放平台，由扫码绑定这个显式动作来切。
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
            '这里的启动、停止与通用设置对它同样生效；要改用 QQ 开放平台，请到「扫码绑定」页扫码。'],
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

    /** 在用时显示提示，否则藏起来。``inConsole`` 为真时多说一句控制台里哪些还能用。 */
    function renderNote(element, dashboard, inConsole) {
        if (!element) { return; }
        var on = inUse(dashboard);
        element.style.display = on ? '' : 'none';
        if (on) { element.textContent = text('note') + (inConsole ? text('note_console') : ''); }
    }

    global.OneBotLegacy = { isOneBotMode: isOneBotMode, inUse: inUse, renderNote: renderNote, text: text };
})(window);
