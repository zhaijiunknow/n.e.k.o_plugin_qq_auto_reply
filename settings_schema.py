"""插件可调参数的**单一真相表**。

一个键要在六层里各写一遍（默认值 / 保存白名单 / 入口 JSON schema / dashboard 签名 /
dashboard 转发 / 钳制），全靠 ``tests/test_qq_config_save_keys.py`` 那几条看门狗盯着。
漏一层就是**静默失效** —— 界面上改得动、存得下、实际没人读。历史上踩过两次：
回复缓冲的两个开关（界面能点，值从没存进去过）和 ``retroactive_review_max_reply``
（能存能校验，逻辑里硬编码了"1-2 条"）。

本模块把每个键的**事实**集中在这里，并生成那六层里能量产的四层：

- ``default_config()`` 的默认值
- ``_CONFIG_SAVE_KEYS`` 白名单（仅 ``saveable``）
- 入口 ``input_schema`` 的 properties（仅 ``saveable``）
- dashboard 快照的逐键 cast 与默认值

**不生成** ``_save_settings_locked``：那里的键有一半带着不可归约的联动逻辑
（切连接模式要重置地址、两个概率键越界要抛异常而不是钳制、五个 consent 键要走
延迟发布、策略模式要联动强制开注意力），硬套通用循环只会把语义压平。所以那些键在
表里标 ``handler``，逻辑留在 ``settings_service``；``handler`` 为 ``None`` 的走通用路径。

新增一个键的标准动作：在 ``SETTINGS`` 里加一条，然后按 ``handler`` 决定要不要在
``settings_service`` 里写具名块，最后在业务代码里**真的读它** —— 后者由
``tests/test_qq_settings_schema.py`` 强制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UIInput:
    """UI 输入框的元数据。``id`` 是 ``static/napcat.html`` 里的元素 id。

    目前只被测试用来断言"前端的 min/max 与后端钳制一致" —— 这两处过去是手工镜像
    （``settings_service`` 里那句"与前端 max=10 对齐"就是证据），会静默漂移。
    """

    id: str
    kind: str = "number"          # "number" | "checkbox" | "text" | "textarea" | "select"
    min: float | None = None
    max: float | None = None
    step: float | None = None
    label: str = ""               # i18n 键
    hint: str = ""                # i18n 键（.hint）


@dataclass(frozen=True)
class SettingSpec:
    """一个可调键的全部事实。"""

    key: str
    kind: str                      # int | float | bool | str | list | dict
    #: 默认值。可变类型（list/dict）在生成时会 deepcopy，调用方拿到的是独立对象。
    default: Any
    #: 是否进保存白名单与参数页。False = 只认配置文件（如 fatigue_* 的细调项）。
    saveable: bool = False
    floor: float | None = None
    ceiling: float | None = None
    #: 上限取自另一个键的**当前值**（如发送线 ≤ 焦点线）。见 settings_service 的具名块。
    ceiling_key: str | None = None
    enum: tuple[str, ...] | None = None
    json_type: str | None = None   # 默认按 kind 推
    description: str = ""          # 入口 input_schema 的描述
    ui: UIInput | None = None
    #: 非 None = 在 settings_service 里有具名处理块（带联动/抛错/延迟发布），不走通用路径。
    handler: str | None = None
    #: 历史别名。保存时接受这些名字，落到 canonical key 上。
    aliases: tuple[str, ...] = ()
    #: 只读透传：既不归一也不改名（存量迁移源，见 config_store.load 的注释）。
    passthrough: bool = False
    #: 不写进 default_config()（历史遗留：靠 .get(key, "") 读）。
    not_in_defaults: bool = False


#: 情绪 → 注意力升/降速率倍率。原先硬编码在 ``attention_service._EMOTION_MULTIPLIER``。
#: 注意 ``attention_service`` 另有两个**按名字**匹配的集合（抢焦点 `arguing`/`proud`、
#: 让焦点 `sulking`/`embarrassed`），它们不随这张表配置化 —— 用户往表里加新情绪时，
#: 新情绪不会自动获得抢/让焦点的行为。
DEFAULT_EMOTION_MULTIPLIERS: dict[str, float] = {
    "arguing": 1.2,      # 上头死磕，涨得快跌得慢
    "proud": 0.8,        # 赢了要炫耀，猛拉注意力
    "annoyed": 0.5,      # 不爽，比正常更专注
    "playful": 0.3,      # 玩闹中，微微挂住
    "curious": 0.2,      # 被勾起兴趣
    "calm": 0.0,         # 正常
    "sad": -0.4,         # 难过，不太想说话
    "embarrassed": -0.6,  # 尴尬想溜
    "sulking": -0.9,     # 赌气——基本清零，主动让出焦点
}

#: 默认的关键词标签表。原先硬编码在 ``config_store.default_backlog_labels``。
DEFAULT_BACKLOG_LABELS: list[dict[str, Any]] = [
    {
        "id": "mention",
        "label": "点名",
        "keywords": [r"@全体成员"],
        "priority": 60,
    },
]

#: 注意力参数，按周期模型分组。
ATTENTION = (
    SettingSpec("group_attention_max_score", "float", 10.0, saveable=True,
                floor=1.0, ceiling=10.0, description="save：注意力分数上限",
                ui=UIInput("cfg-att-max", min=1, max=10, step=0.5,
                           label="ui.attention.max_score", hint="ui.attention.max_score.hint")),
    # 下调焦点线时会把发送线一起收紧（见 settings_service），避免焦点群夺冠后
    # 被门控第 5 步一直拒之门外。
    SettingSpec("group_attention_focus_threshold", "float", 4.0, saveable=True,
                floor=0.1, description="save：焦点线（赢得焦点的资格线）",
                handler="attention_focus_threshold",
                ui=UIInput("cfg-att-focus-threshold", min=1, max=10, step=0.5,
                           label="ui.attention.focus_threshold", hint="ui.attention.focus_threshold.hint")),
    # 焦点群的「保持线」：低于焦点线、高于最低线。焦点线是夺冠资格线；发送门控若也用
    # 焦点线，焦点群回一条就跌破线被门控（见 attention_gate_service 门控第 5 步）。
    SettingSpec("group_attention_focus_send_threshold", "float", 2.0, saveable=True,
                floor=0.0, ceiling_key="group_attention_focus_threshold",
                description="save：焦点发送门控线",
                handler="attention_focus_send_threshold",
                ui=UIInput("cfg-att-focus-send", min=0, max=10, step=0.5,
                           label="ui.attention.focus_send_threshold",
                           hint="ui.attention.focus_send_threshold.hint")),
    SettingSpec("group_attention_min_threshold", "float", 1.0, saveable=True,
                floor=0.0, description="save：最低阈值（全群低于此线 = 全局休眠）",
                ui=UIInput("cfg-att-min-threshold", min=0, max=5, step=0.5,
                           label="ui.attention.min_threshold", hint="ui.attention.min_threshold.hint")),
    SettingSpec("group_attention_message_gain", "float", 0.25, saveable=True,
                floor=0.0, description="save：批量消息计数时每条消息的注意力增益",
                ui=UIInput("cfg-att-msg-gain", min=0, max=3, step=0.05,
                           label="ui.attention.msg_gain", hint="ui.attention.msg_gain.hint")),
    SettingSpec("attention_base_rise_rate", "float", 0.02, saveable=True,
                floor=0.0, description="save：rise 相位每秒自然增长（0=禁用自然上升）",
                ui=UIInput("cfg-att-rise-rate", min=0, max=1, step=0.001,
                           label="ui.attention.rise_rate", hint="ui.attention.rise_rate.hint")),
    SettingSpec("attention_message_boost", "float", 0.15, saveable=True,
                floor=0.0, description="save：单条消息的注意力加成",
                ui=UIInput("cfg-att-msg-recovery", min=0, max=3, step=0.05,
                           label="ui.attention.msg_recovery", hint="ui.attention.msg_recovery.hint")),
    SettingSpec("attention_keyword_boost_ratio", "float", 1.8, saveable=True,
                floor=0.0, description="save：关键词命中时的额外加成倍率",
                ui=UIInput("cfg-att-kw-boost", min=0, max=10, step=0.1,
                           label="ui.attention.kw_boost", hint="ui.attention.kw_boost.hint")),
    SettingSpec("attention_honeymoon_seconds", "int", 60, saveable=True,
                floor=0, description="save：夺冠蜜月（秒）",
                ui=UIInput("cfg-att-focus-rise", min=0, max=600, step=10,
                           label="ui.attention.focus_rise", hint="ui.attention.focus_rise.hint")),
    SettingSpec("attention_fall_seconds", "int", 30, saveable=True,
                floor=0, description="save：fall 相位最短持续（秒）",
                ui=UIInput("cfg-att-focus-cooldown", min=0, max=3600, step=60,
                           label="ui.attention.focus_cooldown", hint="ui.attention.focus_cooldown.hint")),
    SettingSpec("attention_fall_rate", "float", 0.015, saveable=True,
                floor=0.0, description="save：fall 相位每秒回落量",
                ui=UIInput("cfg-att-decay", min=0, max=1, step=0.001,
                           label="ui.attention.decay", hint="ui.attention.decay.hint")),
    SettingSpec("attention_consume_ratio", "float", 0.10, saveable=True,
                floor=0.0, ceiling=1.0, description="save：每次回复消耗的注意力比例",
                ui=UIInput("cfg-att-reply-penalty", min=0, max=1, step=0.05,
                           label="ui.attention.reply_penalty", hint="ui.attention.reply_penalty.hint")),
)

#: 本轮从硬编码提上来的注意力项。
ATTENTION_NEW = (
    SettingSpec("attention_fall_boost_attenuation", "float", 0.3, saveable=True,
                floor=0.0, ceiling=1.0,
                description="save：fall 相位里消息加成的衰减系数（越小越难回血）",
                ui=UIInput("cfg-att-fall-attenuation", min=0, max=1, step=0.05,
                           label="ui.attention.fall_attenuation",
                           hint="ui.attention.fall_attenuation.hint")),
    SettingSpec("attention_at_bot_boost", "float", 3.0, saveable=True,
                floor=0.0, description="save：被 @ 时消息加成的倍率",
                ui=UIInput("cfg-att-at-bot-boost", min=0, max=20, step=0.5,
                           label="ui.attention.at_bot_boost",
                           hint="ui.attention.at_bot_boost.hint")),
    SettingSpec("attention_question_boost", "float", 1.5, saveable=True,
                floor=0.0, description="save：消息是提问时的加成倍率",
                ui=UIInput("cfg-att-question-boost", min=0, max=20, step=0.5,
                           label="ui.attention.question_boost",
                           hint="ui.attention.question_boost.hint")),
    SettingSpec("attention_wake_boost_ratio", "float", 0.75, saveable=True,
                floor=0.0, ceiling=1.0,
                description="save：唤醒时把分数垫到「焦点线 × 此比例」",
                ui=UIInput("cfg-att-wake-ratio", min=0, max=1, step=0.05,
                           label="ui.attention.wake_ratio",
                           hint="ui.attention.wake_ratio.hint")),
    SettingSpec("attention_decay_interval_seconds", "float", 5.0, saveable=True,
                floor=0.1, description="save：注意力衰减循环的 tick 间隔（秒）",
                ui=UIInput("cfg-att-decay-interval", min=1, max=120, step=0.5,
                           label="ui.attention.decay_interval",
                           hint="ui.attention.decay_interval.hint")),
    # 9 项情绪 → 倍率表。做成 JSON 文本框而不是 9 个数字框：后者要在六层里各写九遍。
    SettingSpec("attention_emotion_multipliers", "dict", DEFAULT_EMOTION_MULTIPLIERS, saveable=True,
                description="save：情绪 → 升/降速率倍率表（JSON 对象）",
                handler="emotion_multipliers",
                ui=UIInput("cfg-att-emotion-multipliers", kind="textarea",
                           label="ui.attention.emotion_multipliers",
                           hint="ui.attention.emotion_multipliers.hint")),
)

#: 回复节奏与频率闸。缓冲只暴露标量 —— "按条数分桶"的那张等待表保持硬编码。
#:
#: 发送延迟的出厂值刻意压得很低（群聊中心 1.0s）：**生成本身已经花掉 2–3 秒，那就是
#: 拟人的停顿了**，再叠 6–10 秒等于重复计费，用户感觉是「猫娘反应很慢」。原先那套
#: 6±3 秒是按「生成很快、需要补一个停顿」设计的，前提不成立。
#:
#: 注意 ``reply_buffer_service`` 里还有一份同名类常量（DEFAULT_WAIT_SECONDS=6.0 等），
#: 那是**传了 settings 为空的调用方**（主要是测试）的最后兜底，生产路径永远走本表；
#: 两处不一致是有意的 —— 改类常量会碰掉 test_group_waits_longer_than_private 钉的
#: 「群聊明显等得比私聊久」那条设计不变量。
PACING = (
    SettingSpec("reply_burst_window_seconds", "int", 60, saveable=True,
                floor=1, description="save：回复频率窗口（秒）",
                ui=UIInput("cfg-burst-window", min=1, max=3600, step=1,
                           label="ui.pacing.burst_window", hint="ui.pacing.burst_window.hint")),
    SettingSpec("reply_burst_max_replies", "int", 3, saveable=True,
                floor=1, description="save：窗口内最多回复几条（第 N+1 条强制静默）",
                ui=UIInput("cfg-burst-max", min=1, max=100, step=1,
                           label="ui.pacing.burst_max", hint="ui.pacing.burst_max.hint")),
    # 发送停顿：**生成完成之后**、真正发出去之前的一小块停顿，只为了让回复别
    # 看起来是秒回的。总时长不靠它 —— 那由下面的收集窗口把控（见
    # reply_buffer_service._send_at 的下限语义），所以它常常"不参与决定"。
    #
    # 原本这里有四个旋钮（均值 / 标准差 / 下限 / 上限，按正态分布取样并夹尾巴）。
    # 改成下限语义后总时长归收集窗口管，正态尾巴与区间夹取都没有意义，四个并成一个。
    #
    # **键名里的 mean 是历史遗留**（原先它是正态分布的中心），现在就是那个停顿本身。
    # 刻意不改名：老配置里已经存了用户调好的值，改名等于把它们静默重置成出厂值。
    SettingSpec("buffer_delay_mean_seconds", "float", 1.0, saveable=True,
                floor=0.0, description="save：群聊发送停顿（秒）—— 生成完成后、发出前的小停顿",
                ui=UIInput("cfg-buffer-mean", min=0, max=60, step=0.5,
                           label="ui.pacing.buffer_mean", hint="ui.pacing.buffer_mean.hint")),
    SettingSpec("buffer_delay_private_seconds", "float", 0.6, saveable=True,
                floor=0.0, description="save：私聊发送停顿（秒）",
                ui=UIInput("cfg-buffer-mean-private", min=0, max=60, step=0.5,
                           label="ui.pacing.buffer_mean_private",
                           hint="ui.pacing.buffer_mean_private.hint")),
    # 收集窗口：从**消息到达**算起至少等这么久才发，是"后续消息并进同一批"的机会窗口。
    # 与上面那组 buffer_delay_* 的区别：那组是**生成完成之后**的发送停顿，这组锚在
    # 到达时刻、是下限（见 reply_buffer_service._send_at）。调小它 = 更难合并。
    SettingSpec("buffer_collect_window_seconds", "float", 3.0, saveable=True,
                floor=0.0,
                description="save：收集窗口（秒）—— 从消息到达算起至少等这么久，给后续消息合并的机会",
                ui=UIInput("cfg-buffer-collect", min=0, max=60, step=0.5,
                           label="ui.pacing.buffer_collect",
                           hint="ui.pacing.buffer_collect.hint")),
    SettingSpec("buffer_collect_window_private_seconds", "float", 1.0, saveable=True,
                floor=0.0, description="save：私聊收集窗口（秒）",
                ui=UIInput("cfg-buffer-collect-private", min=0, max=60, step=0.5,
                           label="ui.pacing.buffer_collect_private",
                           hint="ui.pacing.buffer_collect_private.hint")),
    SettingSpec("buffer_max_count", "int", 17, saveable=True,
                floor=1, description="save：缓冲多少条就强制总结（同时是「我在听」的上界）",
                ui=UIInput("cfg-buffer-max-count", min=1, max=200, step=1,
                           label="ui.pacing.buffer_max_count",
                           hint="ui.pacing.buffer_max_count.hint")),
)

#: 疲劳系统参数（KiraAI-style 动态行为约束）。
#: ``fatigue_enabled`` 此前是死键（默认值里有、保存/校验/界面都有、运行时没人读）。
FATIGUE = (
    SettingSpec("fatigue_enabled", "bool", True, saveable=True,
                description="save：疲劳系统总开关（关掉后不再影响注意力涨跌与破冰）",
                ui=UIInput("cfg-fatigue-enabled", kind="checkbox",
                           label="ui.fatigue.enable")),
    SettingSpec("fatigue_circadian_peak_hour", "int", 15, floor=0, ceiling=23,
                description="昼夜节律峰值时间（24 小时制）"),
    SettingSpec("fatigue_circadian_low_hour", "int", 3, floor=0, ceiling=23,
                description="昼夜节律低谷时间"),
    SettingSpec("fatigue_session_per_reply", "float", 5.0, floor=0.0,
                description="每条回复增加的会话疲劳"),
    SettingSpec("fatigue_awake_idle_timeout", "float", 10.0, floor=0.0,
                description="已废弃（疲劳没有睡眠状态机，从未被读取）"),
)

#: 其余全部键。``saveable`` 决定是否进白名单/参数页；``handler`` 决定是否走通用路径。
MISC = (
    # ── 连接 ──
    SettingSpec("qq_connection_mode", "str", "napcat", saveable=True,
                enum=("napcat", "napcat_forward", "open_platform"),
                description="save：连接方式", handler="qq_connection_mode"),
    SettingSpec("onebot_url", "str", "ws://0.0.0.0:6199", saveable=True,
                description="save：反向 WS 监听地址 / 正向 WS 拨号目标",
                handler="onebot_url"),
    SettingSpec("token", "str", "", saveable=True, description="save：OneBot 鉴权 token",
                handler="token"),
    SettingSpec("napcat_directory", "str", "", saveable=True,
                description="save：NapCat 安装目录", handler="napcat_directory"),
    # 默认**后台**启动：自动化（一键部署 / 开机自启）不该弹一个控制台出来打断用户。
    # 藏了窗口就没有控制台了，所以启动隐藏窗口时会顺手打开 napcat.json 的 fileLog ——
    # 否则 NapCat 的日志哪儿都不会留，见 napcat_onebot_config.ensure_file_log。
    SettingSpec("show_napcat_window", "bool", False, saveable=True,
                description="save：启动 NapCat 时显示控制台窗口",
                ui=UIInput("cfg-show-napcat", kind="checkbox")),
    SettingSpec("local_stt_url", "str", "", saveable=True,
                description="save：本地 STT 地址", not_in_defaults=True),
    # ── QQ 开放平台 ──
    SettingSpec("qq_open_app_id", "str", "", saveable=True,
                description="save：开放平台 AppID", handler="qq_open_app_id"),
    SettingSpec("qq_open_client_secret", "str", "", saveable=True,
                description="save：开放平台密钥", handler="qq_open_client_secret"),
    SettingSpec("qq_open_sandbox_enabled", "bool", False, saveable=True,
                description="save：开放平台走沙箱域名", handler="qq_open_sandbox_enabled"),
    # 采集授权：打开后每条事件都把标识符落进**持久**日志，所以走延迟发布
    # （写盘成功后才对运行时可见），与记忆开关同族。
    SettingSpec("qq_open_identity_probe_enabled", "bool", False, saveable=True,
                description="save：身份作用域取证（往持久日志写标识符）",
                handler="consent_opt_in"),
    # ── 名单与账本 ──
    SettingSpec("trusted_users", "list", [], description="可信用户名单",
                ui=UIInput("", kind="text")),
    SettingSpec("trusted_groups", "list", [], description="可信群名单"),
    SettingSpec("speaker_trust_profiles", "dict", {}, passthrough=True,
                description="存量 per-QQ 信赖度账本（只读迁移源，归一/截断等于改写它）"),
    # ── 转发与概率 ──
    # 越界抛 ValueError（不是钳制）—— 有测试钉着。
    SettingSpec("normal_relay_probability", "float", 0.1, saveable=True,
                floor=0.0, ceiling=1.0, description="save：转发给管理员的概率",
                handler="probability",
                ui=UIInput("cfg-normal-prob", min=0, max=1, step=0.05)),
    # canonical 是 open_reply_probability；UI/白名单历史上用别名 truth_reply_probability，
    # 保存时两个键一起写（见 settings_service），读取处先读 canonical。
    SettingSpec("open_reply_probability", "float", 0.1, saveable=True,
                floor=0.0, ceiling=1.0, description="save：开放群回复概率 0~1",
                handler="probability", aliases=("truth_reply_probability",),
                ui=UIInput("cfg-truth-prob", min=0, max=1, step=0.05)),
    # ── 引导 ──
    SettingSpec("show_onboarding", "bool", True, saveable=True,
                description="save：是否显示引导"),
    SettingSpec("guide_step_napcat_done", "bool", False, saveable=True,
                description="save：NapCat 步骤是否已完成"),
    SettingSpec("guide_step_config_done", "bool", False, saveable=True,
                description="init / save：配置步骤是否已完成"),
    SettingSpec("guide_step_runtime_done", "bool", False, saveable=True,
                description="save：运行时步骤是否已完成"),
    # ── 运行时 ──
    SettingSpec("max_concurrent_messages", "int", 3, floor=1,
                description="同时在处理的消息数上限"),
    SettingSpec("ai_connect_timeout_seconds", "float", 10.0, floor=0.0),
    SettingSpec("ai_turn_timeout_seconds", "float", 60.0, floor=0.0),
    SettingSpec("handler_shutdown_timeout_seconds", "float", 10.0, floor=0.0),
    SettingSpec("reply_mode", "str", "text", saveable=True,
                enum=("text", "voice", "both"), description="save：回复模式",
                handler="reply_mode", ui=UIInput("", kind="select")),
    # ── 积压 ──
    SettingSpec("backlog_retention_limit", "int", 200, floor=20,
                description="每个会话保留多少条积压消息"),
    SettingSpec("backlog_summary_threshold", "int", 10, floor=1,
                description="已废弃（解析了但从未被使用）"),
    SettingSpec("backlog_notify_cooldown_seconds", "int", 900, floor=60,
                description="积压通知冷却（秒）"),
    SettingSpec("backlog_issue_notify_threshold", "int", 1, floor=1,
                description="多少条非 chat 标签的消息触发通知"),
    SettingSpec("backlog_labels", "list", DEFAULT_BACKLOG_LABELS, saveable=True,
                description="save：关键词/优先级标签表", handler="backlog_labels"),
    # ── 策略 ──
    # "neko_dynamic" | "neko_scene" —— 主策略 / 退级策略。
    SettingSpec("strategy_mode", "str", "neko_dynamic", saveable=True,
                enum=("neko_dynamic", "neko_scene"), description="save：策略模式",
                handler="strategy_mode"),
    SettingSpec("neko_dynamic_idle_timeout_seconds", "float", 10.0, floor=0.0,
                description="已废弃（注意力系统下不再使用）"),
    SettingSpec("neko_dynamic_waking_users", "list", [],
                description="已废弃（改用 attention + backlog_labels）"),
    SettingSpec("neko_dynamic_waking_keywords", "list", [],
                description="已废弃（改用 backlog_labels 的 keywords）"),
    # ── 回溯补回 ──
    SettingSpec("retroactive_review_max_messages", "int", 30, saveable=True,
                floor=1, description="save：回溯最多取多少条被忽略消息",
                ui=UIInput("cfg-retro-max-msgs", min=1, max=500, step=1,
                           label="ui.napcat.config.retro_review_max_msgs")),
    # 此前是死键：提示词里硬编码了"1-2 条 / 不要超过两条"。
    SettingSpec("retroactive_review_max_reply", "int", 5, saveable=True,
                floor=1, description="save：回溯最多补回多少条",
                ui=UIInput("cfg-retro-max-reply", min=1, max=50, step=1,
                           label="ui.napcat.config.retro_review_max_reply")),
    SettingSpec("icebreaker_cold_threshold", "int", 3, saveable=True,
                floor=0, description="save：连续冷场多少次触发破冰（0=禁用）",
                ui=UIInput("cfg-icebreaker-threshold", min=0, max=20, step=1,
                           label="ui.attention.icebreaker", hint="ui.attention.icebreaker.hint")),
    # ── 回复缓冲开关 ──
    # 群聊与私聊**各自独立**开关，默认都开（与历史行为一致）。
    # 关掉的那一类不再排队等待，每条消息各自判定并立即投递。
    SettingSpec("group_buffer_enabled", "bool", True, saveable=True,
                description="save：群聊回复缓冲",
                ui=UIInput("cfg-buffer-group", kind="checkbox",
                           label="ui.napcat.config.buffer_group")),
    SettingSpec("private_buffer_enabled", "bool", True, saveable=True,
                description="save：私聊回复缓冲",
                ui=UIInput("cfg-buffer-private", kind="checkbox",
                           label="ui.napcat.config.buffer_private")),
    # 默认**关** —— NapCat 会为注入拉起 QQ（必要时杀掉正在运行的那个），
    # 不该由插件替用户决定。
    SettingSpec("auto_start_on_launch", "bool", False, saveable=True,
                description="save：自启（每次插件启动都拉起 NapCat 并接上自动回复）"),
    # ── 记忆（五个采集授权键走延迟发布）──
    # 群聊长期记忆显式 opt-in。成员记忆会增加按成员分桶的提取调用，
    # 因此独立开关且默认关闭。
    SettingSpec("group_memory_enabled", "bool", False, saveable=True,
                description="save：群聊长期记忆（显式 opt-in）", handler="consent_opt_in"),
    SettingSpec("group_member_memory_enabled", "bool", False, saveable=True,
                description="save：按成员分桶的群记忆（父开关关着时授权无效）",
                handler="consent_opt_in"),
    # 非管理员私聊的 participant 记忆：以对方为主体单独建档（participant scope），
    # 绝不进管理员的 legacy 私聊语料。同为显式 opt-in，默认关闭。
    SettingSpec("private_participant_memory_enabled", "bool", False, saveable=True,
                description="save：非管理员私聊的 participant 记忆",
                handler="consent_opt_in"),
    # 跨群实时话题不是长期记忆的一部分，默认严格隔离。
    SettingSpec("allow_cross_group_context", "bool", False, saveable=True,
                description="save：允许跨群实时话题上下文", handler="consent_opt_in"),
    # ── 提示词覆盖 ──
    # 提示词编辑器覆盖值（locale → layer_id → text）
    SettingSpec("prompt_overrides", "dict", {},
                description="提示词编辑器覆盖值（locale → layer_id → text）"),
    SettingSpec("group_prompts", "dict", {}, description="按群自定义提示词"),
)

SETTINGS: tuple[SettingSpec, ...] = ATTENTION + ATTENTION_NEW + PACING + FATIGUE + MISC

BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS}

#: 白名单：``__init__.py`` 的 ``_CONFIG_SAVE_KEYS`` 由此生成（含别名）。
SAVEABLE_KEYS: frozenset[str] = frozenset(
    name for spec in SETTINGS if spec.saveable for name in (spec.key, *spec.aliases)
)

#: 有具名处理块的键 → 处理块名。用于看门狗断言"标了 handler 就真的有人管"。
HANDLED_KEYS: frozenset[str] = frozenset(
    name for spec in SETTINGS if spec.handler for name in (spec.key, *spec.aliases)
)


def _json_type(spec: SettingSpec) -> str:
    if spec.json_type:
        return spec.json_type
    return {
        "int": "integer", "float": "number", "bool": "boolean",
        "str": "string", "list": "array", "dict": "object",
    }[spec.kind]


def input_schema_properties() -> dict[str, dict[str, Any]]:
    """入口 ``config`` 的 JSON schema properties（仅 saveable，含历史别名）。

    **别名必须一起生成**：入口 schema 带 ``additionalProperties: False``，而前端发的
    是别名（``truth_reply_probability``）。漏掉别名不是"少一条文档"，是那次保存被整个拒掉。
    """
    props: dict[str, dict[str, Any]] = {}
    for spec in SETTINGS:
        if not spec.saveable:
            continue
        entry: dict[str, Any] = {"type": _json_type(spec)}
        if spec.enum:
            entry["enum"] = list(spec.enum)
        if spec.description:
            entry["description"] = spec.description
        props[spec.key] = entry
        for alias in spec.aliases:
            alias_entry = dict(entry)
            if spec.description:
                alias_entry["description"] = f"{spec.description}（历史别名，落到 {spec.key}）"
            props[alias] = alias_entry
    return props


def defaults() -> dict[str, Any]:
    """``default_config()`` 的默认值部分。可变类型每调用一次给一份新的。

    ``not_in_defaults`` 的键被排除：它们历史上就不在 ``default_config()`` 里
    （靠 ``.get(key, "")`` 读），凭空补一条会改动默认配置的形状。表里仍留着它们的
    声明，好让"表覆盖全部键"这条断言成立。
    """
    import copy

    return {
        spec.key: copy.deepcopy(spec.default)
        for spec in SETTINGS
        if not spec.not_in_defaults
    }


def snapshot_value(spec: SettingSpec, settings: dict[str, Any]) -> Any:
    """dashboard 快照里一个键的取值：按 kind 选 cast，缺值时退回默认。

    缺值路径**返回副本**：`attention_emotion_multipliers` 之类的默认值是模块级可变
    对象，按引用交出去等于把表本身暴露给调用方改。
    """
    import copy

    raw = settings.get(spec.key)
    if raw is None:
        fallback = spec.default
        if fallback is None:
            # 可变类型没写默认值 —— 交给调用方原样透传。
            return raw
        raw = copy.deepcopy(fallback)
    if spec.kind == "bool":
        return bool(raw)
    if spec.kind == "int":
        return int(raw)
    if spec.kind == "float":
        return float(raw)
    return raw


__all__ = [
    "ATTENTION",
    "ATTENTION_NEW",
    "BY_KEY",
    "DEFAULT_EMOTION_MULTIPLIERS",
    "FATIGUE",
    "HANDLED_KEYS",
    "MISC",
    "PACING",
    "SAVEABLE_KEYS",
    "SETTINGS",
    "SettingSpec",
    "UIInput",
    "defaults",
    "input_schema_properties",
    "snapshot_value",
]
