# 群聊环境处理：第二轮外部调研 + 与我们逐维度对比

> 第一轮见 `GROUP-CHAT-RESPONSE-MECHANISMS.md`（AstrBot 9 阶段责任链、LangBot 11 阶段、
> MaiBot `reply_necessity` 常数、学术量化依据、QQ 官方通道限制）。**本轮换角度**：
> 不看"框架抽象"，而是看**别人在群聊环境里具体装了什么旋钮、默认值多少**，
> 然后逐条对照我们插件现在有什么（对照表的"我们"一列来自 2026-09-27 的代码/线上配置盘点）。

## 0. 取证方式与限制（先读）

- 本轮**我亲自拉到的原文**：AstrBot 官方配置文件文档、AstrBot 生态两个成熟群聊插件
  （`astrbot_plugin_group_chat_plus` 的 `CONFIG_REFERENCE.md`、Yunzai 系 `bl-chat-plugin`
  的 `README.md`/`message.yaml` 说明）、腾讯 QQ 机器人官方文档（消息收发概述）、
  OpenClaw 文档（bot loop protection）。
- 本机 DNS 策略**屏蔽 `github.com` 与 `raw.githubusercontent.com`**（解析到非公网 IP），
  所以 GitHub 源码一律**改用镜像**读：`cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/<path>`
  （可用，本轮两个插件的文档就是这么读的）。这条对以后的调研同样有效。
- 证据等级标注：`【原文】`＝我直接读到的官方文档/仓库文件；`【子代理】`＝并行子代理读到并
  交出处的（本轮用于 MaiBot 源码级、框架层、协议端风控）；`【二手】`＝博客/搜索摘要。
- **凡是没读到的一律写"未验证"**，不从别人的默认值外推我们的行为。

## 1. 本轮读到的一手材料（摘要）

### 1.1 AstrBot 核心：`platform_settings` 与 `provider_ltm_settings`【原文】

来源：[AstrBot 配置文件](https://docs.astrbot.app/dev/astrbot-config.html)

| 键 | 默认 | 说明 |
|---|---|---|
| `rate_limit.time / count / strategy` | `60 / 30 / "stall"` | 消息速率超限时：`stall` 等待、`discard` 丢弃 |
| `unique_session` | `false` | 「隔离对话」= 每位群成员**独立上下文** |
| `forward_threshold` | `1500` | 回复超长自动折叠成 QQ「转发消息」防刷屏（仅 QQ） |
| `enable_id_white_list` / `id_whitelist` | `true` / `[]` | **ID 白名单**（空列表=不过滤）；`wl_ignore_admin_on_group=true` 管理员无视白名单 |
| `reply_with_mention` / `reply_with_quote` | `false` | 回复时是否 @ / 引用 |
| `segmented_reply` | `enable:false, interval_method:"random", interval:"1.5,3.5", words_count_threshold:150, regex:".*?[。？！~…]+\|.+$"` | **分段回复**：按句切分、**随机间隔 1.5~3.5 秒**、超 150 字才分 |
| `ignore_bot_self_message` / `ignore_at_all` | `false` | 忽略自身消息 / 忽略 @全体 |
| `empty_mention_waiting` | `true` | 空的 @ 消息 → 等待而不是忽略 |
| `provider_ltm_settings.group_icl_enable` | `false` | 群聊长期记忆开关；`group_message_max_cnt: 1000` |
| `provider_ltm_settings.active_reply` | `enable:false, method:"possibility_reply", possibility_reply:0.1, whitelist:[]` | **主动回复**：概率 0.1 + 白名单 |
| `content_safety.internal_keywords` / `baidu_aip` | `enable:true, []` / 关 | 内容安全：内置词表 + 百度审核 API |

### 1.2 `astrbot_plugin_group_chat_plus`：把群聊环境拆成 ~40 个模块【原文】

来源：[CONFIG_REFERENCE.md（jsDelivr 读到的仓库文件）](https://cdn.jsdelivr.net/gh/Him666233/astrbot_plugin_group_chat_plus@main/docs/CONFIG_REFERENCE.md)

这是**目前看到的最完整的"群聊环境处理"配置面**（100+ 项，含 Web 面板）。与我们最相关的：

| 模块 | 关键默认值 | 备注 |
|---|---|---|
| 基础概率 | `initial_probability 0.02`（每条 2% 过第一层筛选）、`after_reply_probability 0.8`、`probability_duration 120` | 回复后临时提概率，促进连续对话 |
| 概率硬限制 | `min 0.05 / max 0.8` | 多重衰减后仍不越界 |
| **读空气 AI** | `decision_ai_timeout 30`，**超时=不回复**；可开推理（先推理块再单独一行 yes/no） | 用一个小模型判"这条要不要回" |
| **注意力机制（默认关）** | **用户级** 0~1 连续值：高注意力回复概率 `0.8` / 低 `0.08`、持续 `120s`、最多追踪 `10` 人；指数衰减**半衰期 300s**；回复该用户 `+0.35`；**读空气判不回 → −0.2**（>0.3 才生效）；情绪 ±0.1/0.15（情绪半衰期 600s）；**注意力溢出**（高注意力用户 spilling 到同群其他人 30%、半衰期 90s、触发下限 0.4）；**两段式冷却**：待冷却（观察该用户后续 1 条 / 最长 60s / 保底概率 0.18）→ 正式冷却（阈值 0.3、最长 600s、可自动解冻） | 与我们"群级注意力"是**两个正交维度**：他们挑"人"，我们挑"群" |
| **回复密度限制** | `enable true`、窗口 `300s`、**硬上限 4 条**、软限比例 `0.6`（≈2 条开始"提示 AI 少回"）、`reply_density_ai_hint true` | 硬闸 + 软提示两层 |
| **对话疲劳（默认关）** | 沉默 `300s` 清零；三档阈值 `3/5/8` 条 → 概率衰减 `0.1/0.2/0.35`；**中度以上 30% 概率发"结束语"**（如"我先忙了"） | 我们用 necessity 的"存在感惩罚"覆盖了同类目的 |
| **拟人模式（默认关）** | 连续 `3` 条未回复 → 进入沉默（最长 `600s`，沉默中收 `8` 条自动醒来）；动态阈值 `1~3`；**兴趣关键词概率 +0.25**；决策历史进 prompt 保持一致性 | 沉默/唤醒状态机 |
| 并发 | `concurrent_mode legacy\|smart`、`smart_concurrent_merge_wait 30s`、`smart_concurrent_max_batch_size 20`、`smart_concurrent_claim_delay 0.3s`、`concurrent_wait_max_loops×interval = 10×1s` | smart = 按到达顺序合并批次后一起交给 AI |
| 上下文格式 | `include_timestamp`（`[2026-03-13 周四 14:30:00]`）、`include_sender_info`（`Name(ID:12345)`）、`max_context_messages -1` | 「谁在什么时候说的」显式进 prompt |
| 单独 @ 的强化 | 消息数窗口 `8` + 时间窗口 `180s` **双满足** | 只 @AI 没内容时，回复阶段补一段中性上下文提醒 |
| 消息缓存 | `custom_storage_max_messages 500`、`pending_cache_max_count 10`、`pending_cache_ttl_seconds 1800` | **未过概率筛选的消息不丢**，暂存待下次回复当上下文 |
| 冷群缓存转正 | `enable_idle_cache_flush false` + `idle_cache_flush_delay_seconds 600` | 静默 10 分钟把待处理池转正入库，避免上下文断裂 |
| 主动对话 | 沉默 `1800s`、概率 `0.2`、检查间隔 `120s`、要求近 `300s` 内 ≥3 条用户消息、连续被无视 `3` 次→冷却 `2400s`、安静时段 `23:00-07:00` + `30` 分钟平滑过渡 | 比我们的破冰更细 |
| 自适应互动评分 | 成功 +15 / 被无视 −10 / 快速回复 +5 / 多人参与 +10 / 连续成功 +5 / 低分复活 +20，每日衰减 2，范围 10~100 | 主动对话策略随反馈调整 |
| 吐槽系统 | 连续被无视 2 次触发，最大累积 15 | 拟人化"被晾着会抱怨" |
| 频率调整器 | 用 AI 判"正常/过于频繁/过少"，`0.85` / `1.1` 系数，间隔 `180s`，持续 `360s` | 群体节奏自适应 |
| 打字模拟 | `enable false`，速度 `15 字符/秒`，最大延迟 `3s` | 与我们 buffer 随机延迟同类 |
| 打字错误 | 概率 `0.02`（每 50 字约 1 个错字） | 拟人化细节 |
| 重复过滤 | 最近 `5` 条、`1800s` 窗口内不重复 | 防复读刷屏 |
| 记忆注入 | `memory_insertion_timing: post_decision`（决策后注入，只影响内容不影响"是否回"）、`livingmemory_top_k 5` | 记忆时机可配 |
| 内容过滤 | 输出过滤与保存过滤**两套独立规则**（Range/Head/Tail 三种标记语法） | 清 thinking 块/第三方注入块 |
| 私聊 | 独立 30+ 项，仍标注"开发中，勿开" | — |

### 1.3 `bl-chat-plugin`（Yunzai + OneBot v11）：Timing Gate 与焦点状态机【原文】

来源：[README.md（jsDelivr）](https://cdn.jsdelivr.net/gh/Cat-bl/bl-chat-plugin@main/README.md)

| 机制 | 默认/细节 | 为什么值得对照 |
|---|---|---|
| **Timing Gate** | 子代理小模型输出 `continue / no_action / wait` 三选一，15s 超时 | 把"什么时候说"交给一个便宜的判断模型（MaiBot 同思路） |
| **talkValue 时段阈值** | `ceil(1/talkValue)`：白天 `0.07`→15 条触发一次、深夜 `0.034`→30 条 | 阈值按时段变，白天更话痨、深夜更安静 |
| 本地预筛 | `skipWhenAddressedOther`（@的是别人→直接跳过，不烧 LLM）、`skipWhenEmptyText` | 先做零成本过滤再花 LLM |
| Gate prompt 信号 | 群 5 分钟 ≥ `30` 条 → 提示"群里热闹，倾向沉默"；bot 10 分钟已回 ≥ `5` 条 → 强烈提示"避免刷屏" | 把"群多忙/我多话"写进判断 prompt |
| **焦点状态机** | `focus / fading / cold`；`focusMaxReplies`、`focusMaxNoAction`、`fadingForceGate` | 与我们"焦点 + 拿不拿得住"同构 |
| 冷群 deferred 唤醒 | 最短 `120s`、最长 `900s` 后再跑一轮 Gate | 冷群里"想想要不要补一句" |
| 打断与让步 | `inFlight` 锁（已有任务在跑则让步并记 `queuedWhileInFlight`）、`maxConsecutiveInterrupts 3`（同群连续被打断上限，超过强制走完） | **我们没有"被打断"这个概念** |
| 复读跟读 | 窗口 5 条、≥`3` 个不同用户文本完全相同、概率 `0.6`、冷却 `180s`、文本 ≤30 字、绕过 LLM 直接发原文 | 我们也有复读（阈值不同） |
| 禁言保护 | bot 被全员/单独禁言 → 所有自动回复链路阻断，兼容 ICQQ/OneBot/NapCat 字段 | 我们也有 `is_group_muted` |
| 群白名单 + 并发 | `enableGroupWhitelist true`、`concurrentLimit 3` | 并发默认 3，与我们一致 |
| 记忆 | 用户记忆 ≤`100` 条/人、群共识记忆 ≤`50` 条/群（类别：topic/rule/meme/event/member）、注入 `8`/`6` 条、总字符 ≤`1200`、**防抖批量提取**（用户静默 45s 或累计 6 条；群最小间隔 10 分钟或 12 条） | 群共识记忆的**分类**比我们细 |
| 表达学习 | 学群友说话风格（每群独立） | 我们只有人格/提示词，没有"学群友口癖" |
| 情感系统 | 每群独立，每小时向中性衰减 `0.02`，被夸 +0.1 / 被骂 −0.15 / 被@ +0.05 | 我们情绪是 9 档**倍率表**（离散），它是连续值 |
| 拟人化发送 | 分段发送（按标点/换行，即使关闭也按 `\n` 拆）、`waitTool`（LLM 主动"稍后再说一句"，1~60s）、错字 | 我们没有分段与 waitTool |
| 工具终态 | `this.terminal(...)`：工具自己已发完（图/语音/表情包）→ 跳过最终文本回复 | 我们有类似收敛（`_confirm_platform_result`） |
| 风控规避 | `textImageTool` 把可能触发敏感词/风控的长文本**转成图片**发送 | 我们没有 |

### 1.4 腾讯 QQ 机器人官方文档：群聊的硬规则【原文】

来源：[消息收发概述（bot.q.qq.com）](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)（页面更新 2026-07-21）

- **事件**：群聊有 `GROUP_AT_MESSAGE_CREATE` **和** `GROUP_MESSAGE_CREATE` 两种 ——
  后者就是"全量群消息"（印证第一轮文档 §6 的路线判断，事件名一致）。
- **被动消息时效/次数**：群聊 **5 分钟有效、每条消息可回复 5 次**；单聊 60 分钟 / 4 次。
- **主动消息频控**：群 **60/qpm**（未认证 30/qpm）、单关系 20/qpm、**每日上限 1000 条/群**；
  用户可在客户端关闭"允许主动发送"，关了以后主动消息**一律失败**。
- **去重与幂等**：同一 `msg_id` 可能多次推送，要结合 `msg_seq` 去重；相同
  `msg_id + msg_seq` 重复发送会失败，多次回复同一消息要**递增 `msg_seq`**。
- **富媒体**：`msg_type=7` + 先上传拿 `file_info`；官方文档明确"**分片上传（推荐）**"
  （与我们 2026-09-27 真机结论一致：旧式直传已失效、分片成功）；`file_info` 有 `ttl`。
- **撤回**：自己发的消息 **2 分钟内**可撤回。

### 1.5 OpenClaw：多 bot 同群的防死循环【原文】

来源：[Bot loop protection](https://docs.openclaw.ai/channels/bot-loop-protection)

- 按**机器人对（pair，双向算同一对）**做滑动窗口限速，默认
  `maxEventsPerWindow 20 / windowSeconds 60 / cooldownSeconds 60`，超预算则冷却期内抑制该对。
- bot 消息可以**只作为上下文可见、但不触发回合**（`allowBots` 与 admission 分离）。
- 内部多 agent 群轮次另有预算：`maxRounds 4`、`maxTurns 32`。

### 1.6 协议端与登录态：NapCat / LLBot / Lagrange / go-cqhttp【子代理·官方文档】

- **全是"全量"**：NapCat / LLBot / Lagrange / go-cqhttp 的文档化配置里**都没有"仅接收 @
  消息"的开关**，也就是说私有协议端默认就能拿到群里每条消息（我们的 NapCat 路径正是如此）。
  只有"是否上报自身消息"这类过滤：NapCat `reportSelfMessage: false`、
  Lagrange `Message.IgnoreSelf: true`、go-cqhttp `report-self-message: false`（默认都是不发自己的）。
- **禁言感知**：`group_ban` 事件（`sub_type=ban|lift_ban` + `duration`）、群成员对象带
  `shut_up_timestamp`、NapCat 另有 `get_group_shut_list` → 我们能可靠实现"被禁言就闭嘴"。
  反面：Lagrange.Core v1 的事件表里**没有禁言事件**（其 OneBot 层是否补齐未验证）。
- **ASR（语音转写）协议端一律不提供**，必须自建 —— 对应我们已有的本地 STT。
  但官方开放平台的语音附件里**带 `asr_refer_text`（ASR 参考结果）**，那条通道可以省掉自建。
- **语音收发转码需要 FFmpeg**（NapCat v4.4.11 起多数平台免配置，macOS 沙盒不支持）。
- **NapCat 没有消息历史库**：消息 ID 是哈希、约 5000 条后 LRU 过期、撤回后不可再取 ——
  所以引用链/回溯补回**必须依赖我们自己的 backlog 存储**（我们正是这么做的）。
- 登录/稳定性：NapCat 扫码（`loginRate 3/分钟`）**无自动重连项**；Lagrange `AutoReconnect: true`
  + 签名服务；go-cqhttp 官方 README 已写"无力继续维护"、Lagrange v1 标注 sunset。
- 正/反向 WS 都是双工；反向 WS = 协议端当客户端主动连我们（利于内网），NapCat 官方推荐优先 WS。

### 1.7 框架层：会话 / 权限 / 限流都是"容器"，不是"策略"【子代理·源码+官方文档】

| 框架 | 会话容器 | 权限 | 冷却/限流 | 去重 | 拟人化 |
|---|---|---|---|---|---|
| **NoneBot2** | 临时响应器（`temp`）续接，`expire_time` 默认 **2 分钟**；`T_State` 只在单次事件流程内有效；**没有**上下文条数/历史 | 只有 `SUPERUSER`；**无内置群主/群管层级**（自己读事件 `role`） | 核心**无** cd（生态插件做） | 无框架级（插件 `nospam` 做群内重复/相似检测并撤回） | 核心无 |
| **Koishi** | `session.prompt(timeout?)`，`delay.prompt` 默认 **60s**；历史条数无内置 | authority **0~5** + 指令/选项级；**assignee（同频道哪个 bot 应答）**；filters（按平台/用户/群 include-exclude）；实验性 permissions 支持 `inherit`/`depend` 与**按平台群管角色授权的访问器权限**（如 `telegram:admin`） | 官方插件 `rate-limit`：`maxUsage` / `minInterval` / `usageName`（多指令共享额度） | 插件 `repeater`：统计连续相同消息（`times/users/repeated`），按 `minTimes`+概率**复读或打断** | **核心有**：`session.sendQueued(msg, delay)`、`delay.character`（按前一条字数）、`delay.message`（默认 100ms）、`cancelQueued`；广播默认 500ms 间隔 |
| **Yunzai (Miao)** | `setContext/getContext`，key=`插件名.群/用户`，**默认 120s** 超时清理 | 规则级 `master/owner/admin/all`（**框架内置群主/群管**） | **框架内置两级 CD**：`groupGlobalCD`（整群）+ `singleCD`（个人，默认 **1000ms**），内存字典 + `setTimeout` | **框架内置** `msgThrottle`：key=`user_id:raw_message`，**200ms** 内同文本直接丢 | 未内置分段/随机延迟；**回复支持引用与定时撤回（0~120s）** |
| **ZeroBot (Go)** | `State` + `FutureEvent`（`Next` 取一次 / `Repeat` 持续并给 cancel），**无超时清空** | Rule 工厂：`SuperUser/Admin/Owner/UserOrGrpAdmin/GroupHigherPermission/CheckUser/CheckGroup/OnlyGroup/OnlyToMe` | **框架内置** `extension/rate`（令牌桶 `NewLimiter(interval, burst)` + `LimiterManager[K]` 按键分桶）与 `extension/single`（同 key **反并发**中间件） | 插件 `breakrepeat` 打断复读 | 无内置；只有 CLI `-l latency` 全局延时 |

**框架层的一致结论**（子代理逐条核过源码/文档）：**四家都不判定"这条群消息要不要回"**。
它们只提供容器与阻断原语（NoneBot 的 priority/block、Koishi 的中间件链、Yunzai 的
"命中即 break，一个事件只被一个 rule 处理"、ZeroBot 的 `Block`），策略全在插件里。
另外三家**都没有"同一用户短时间多条合并"**，只有 Yunzai 有 200ms 同文本去重、
Koishi 有"复读检测"。

对我们的启示（可抄的原语）：

1. **Yunzai 的 `msgThrottle`（200ms 同文本）**：极便宜，专治"同一事件被推送两次/自己重复回复"。
2. **Koishi 的 `delay.character`（按上一条字数算延迟）**：比"固定随机延迟"更像打字 ——
   我们的 buffer 延迟是固定区间，可以改成随回复长度变化。
3. **Yunzai 的"命中即 break"**：多插件/多策略竞争时**只有一个**处理这条消息 —— 我们的
   门控链已经是顺序短路（等价），但值得明确写成不变式（我们有测试守着顺序）。
4. **Koishi 的 assignee**：同频道"哪个 bot 应答"是框架级概念 —— 我们的多 bot 场景目前没有对策。

### 1.8 MaiBot 官方配置（`bot_config.toml`）：我们抄的那套的**出处**【原文】

来源：[Bot 配置（docs.mai-mai.org，更新 2026-09-14）](https://docs.mai-mai.org/manual/configuration/bot-config)

**这段最要紧**：我们的 `IdleBackoff` 与 necessity 阈值就是从 MaiBot 抄的，现在拿到了官方默认值，
可以对账；而且它**也有跨聊天的焦点机制**（见下），我们不是唯一一家。

`[chat.reply_timing]`（发言时机）：

| 键 | 默认 | 与我们的关系 |
|---|---|---|
| `talk_value` / `private_talk_value` | `1`（0~1，越小越安静；0.3~0.5 明显话少） | 我们**没有**这个旋钮（我们用 necessity 阈值 40 表达"多安静"） |
| `inevitable_at_reply` | `true` | ✓ 我们的 @ 旁路 |
| `mentioned_bot_reply` | `false` | 我们**没有**"提到名字更容易回"（`neko_dynamic_waking_keywords` 是唤醒词，语义不同） |
| **`reply_trigger_mode`** | `"frequency"` / `"reply_necessity"` | **两种触发模式可切**；我们直接并成了 necessity 一条（这是有意的简化） |
| `planner_interrupt_max_consecutive_count` | `0`（不限） | 我们**没有**"思考中来了新消息要不要重新想" |
| `max_consecutive_wait_count` | `3` | 我们**没有**（wait 是它的动作之一） |
| **`no_action_backoff_base_seconds`** | **`15`** | ✓✓ **我们 `IdleBackoff` 的 `15` 就是从这里来的** |
| **`no_action_backoff_cap_seconds`** | **`300`** | ✓✓ 我们的上限 `300` |
| **`no_action_backoff_start_count`** | **`2`** | ✓✓ 我们的 `START_COUNT = 2` |
| **`no_action_backoff_bypass_pending_count`** | **`6`** | ✓✓ 我们的 `BYPASS_PENDING = 6` |
| `talk_value_rules`（平台/群/时段，支持跨夜 `23:00-02:00`） | 默认两条全局规则 | 我们**没有**时段化频率 |

`[chat]`：`max_context_size 40`（群聊参考最近 40 条）/ `max_private_context_size 60` /
`enable_context_optimization true` / `mid_term_memory true` + `mid_term_memory_lenth 10`（**中期记忆
"聊天回想"**）—— 我们靠 Memory Server 的群记忆 + idle finalize，没有"最近 40 条"这种显式窗口。

`[experimental]`（**关键更正**）：

- **`focus_mode`（默认关）＝"同一时间只专注一个聊天流"**，`focus_on_private`、
  `focus_chat_whitelist`、**`focus_groups`（同组共享 Focus，不同组互不抢占）**、
  **`focus_cool_time = 120`（当前聊天多久没继续后允许被其他聊天唤醒）**。
  → **这就是跨聊天/跨群的焦点竞争**，只是实现形态不同：MaiBot 是「焦点 + 120s 冷却后允许被抢 +
  可分组」；我们是「分数 0~10 + 焦点线 4.0 / 保持线 2.0 + 被 @@ 上锁 90s + 蜜月 60s」。
  **我原先"跨群竞争只有我们做"的说法不成立，本文件 §2.2 已更正。**
- `attention_drift`（**话题级**注意力漂移：更容易被新话题/梗/反差点吸引；档位
  `subtle/active/scattered/wild`；`anchor_policy` 回钩策略；`reaction_style`）——
  这是"对话题的注意力"，与"对群/对人的注意力"又是另一个维度，我们完全没有。
- `enable_behavior_learning`（学"什么时候该怎么回应"的经验）、`enable_rich_reply`、
  `emotion_trait`。

`[message_receive]`：`image_parse_threshold 5`（单条图超 5 张就不识图）、`ban_words`、
**`ban_msgs_regex`（正则黑名单，启动时校验，写错直接启动失败）** —— 我们只有关键词表，
没有正则黑名单。

`[response_post_process]`：总开关 `enable_response_post_process true` 同时管**错别字生成与
"回复分割"**，另有 **`typing_speed`（0 最快 / 1 默认 / 2 更慢）** —— 即 MaiBot 也有打字速度模拟
与分段回复（与 AstrBot 的 `segmented_reply` / `typing_simulator` 同类）。

`[expression]` / `[jargon]`：**表达学习 + 黑话学习**（含 `vector_intent` 向量意图召回、
候选池上限 50、`learning_list` 按平台/群/私聊分别控制 use/learn）。

## 2. 逐维度对比（外部 vs 我们）

> "我们"一列＝当前代码/线上配置（86 个配置键、7 入口 50 动作、64 模块）。

### 2.1 「该不该回」

| 做法 | 外部 | 我们 |
|---|---|---|
| 概率门槛 | AstrBot 插件：首层 `0.02`，回复后临时抬到 `0.8`（120s）；`talkValue` 时段化 | 无概率门槛；normal 群 relay 概率 `0.1`（转达，不是回复） |
| 便宜模型判"要不要回" | AstrBot 插件读空气 AI（超时=不回）；bl-chat Timing Gate（continue/no_action/wait） | **没有**；我们的 8.5 是**确定性打分**（necessity，阈值 40），不烧额外 LLM |
| 打分/因子 | MaiBot 的 necessity 打分（我们抄的） | ✓ 已落地：@100 / 引用80 / 焦点40 / 提问15… + 积压压力 + 存在感惩罚 + 频率因子 |
| 时段化 | bl-chat `talkValue` 按时段、群聊插件的"动态时段概率" | ✗ 没有时段概念（疲劳/作息是我们主动删掉的） |
| 群多忙/我多话作为信号 | bl-chat 把它写进 Gate prompt（群 5min≥30 条→倾向沉默；我 10min 回了≥5 条→提示别刷屏） | ✓ 我们把它做成了**数**（积压压力 + 存在感惩罚），但没有"回了几条"的显式上限提示 |

**结论**：我们在"确定性打分"这条路上走得比他们远（他们是"概率 + 小模型判断"），
代价是没有"读空气"的语义理解（例如"群里在吵架，别插嘴"这种判断我们做不到）。
两者可以并存：**先打分，边界区间再交给小模型**——这是我们目前缺失的一档。

### 2.2 多群与焦点

| 外部 | 我们 |
|---|---|
| **MaiBot 有跨聊天焦点**（`experimental.focus_mode`）：同一时间只专注一个聊天流、`focus_cool_time 120`（多久没继续才允许被别的聊天唤醒）、`focus_groups`（同组共享焦点、不同组互不抢占）、`focus_chat_whitelist`；另有 `attention_drift`（**话题级**漂移） | **群级注意力竞争**：0~10 分、焦点线 4.0 / 保持线 2.0、被 @ 上锁 90s、蜜月 60s、频次倍率 0.15~1.8、情绪 9 档倍率 |
| AstrBot 插件的「注意力机制」是**用户级**：每个用户 0~1 连续值（高注意力回复概率 `0.8` / 低 `0.08`、半衰期 `300s`、回复该用户 `+0.35`、**读空气判不回 −0.2**、情绪 ±0.1/0.15），外加**溢出**（高注意力用户 30% 溢到同群其他人）与**两段式冷却**（待冷却观察 1 条 / 60s / 保底 0.18 → 正式冷却阈值 0.3 / 最长 600s） | 我们**没有用户级注意力**（靠 necessity 的"自己发言占比"惩罚 + @/引用 显式信号） |
| bl-chat：**per-group 焦点状态机**（`focus / fading / cold` + `focusMaxReplies` / `focusMaxNoAction`），群与群之间完全独立 | 我们是**全局竞争**（同一时刻只有一个焦点群） |

**结论（更正）**：三个"注意力维度"是**正交**的 —— **群级**（我们 + MaiBot `focus_mode`）、
**用户级**（AstrBot 插件）、**话题级**（MaiBot `attention_drift`、第一轮文档里的 Heartflow）。
我们只做了**群级**这一维，且形态与 MaiBot 不同：

- MaiBot：布尔焦点 + **120s 冷却后允许被抢** + 可分组（简单、可解释）。
- 我们：连续分数 + 焦点线/保持线 + 锁 90s + 蜜月 + 频次/情绪倍率（表达力强、参数多）。

**值得抄的两点**：① **`focus_groups`（共享组）** —— 让几个"其实是一个场景"的群共享焦点，
避免它们在彼此之间来回抢（我们完全没有这个概念）；② **`focus_cool_time` 的语义** ——
"刚聊过的群有冷却保护"，我们用 `lock + 蜜月` 表达了近似语义但更隐晦。

### 2.3 消息缓存与合并

| 外部 | 我们 |
|---|---|
| AstrBot：`rate_limit` stall/discard；插件 `pending_cache_max_count 10` + TTL 1800s + **冷群 10 分钟自动转正**；smart 模式按到达顺序合批（等待 30s、最多 20 条） | 缓冲合并：群 5s 窗口 / 私聊 1s、随机延迟 0.2~2.5s、**上限 17 条**；idle 10s finalize + digest |
| bl-chat：`concurrentLimit 3`，inFlight 锁让步 | 并发闸 3（一致） |

**结论**：我们的"合并缓冲"与他们的"pending 池"目的相同（都是为了不逐条烧 LLM），
但**他们没有"没通过筛选的消息也要留着当上下文"这一步之外的"转正"语义**；
我们缺的是他们的 **TTL + 冷群转正**（我们靠 idle finalize 推 digest，等价但时机不同）。

### 2.4 会话与上下文构建

| 外部 | 我们 |
|---|---|
| 时间戳 + 发言人 ID 显式进 prompt（`[2026-03-13 周四 14:30:00]`、`Name(ID:12345)`）；单独 @ 的双窗口强化（8 条 / 180s）；`unique_session` 可按人隔离上下文 | 有时间上下文模块、`sender_nickname`、引用链/转发/语音/文件递归展开、跨群上下文（`allow_cross_group_context=true`） |
| 长回复折叠成"转发消息"（1500 字） | ✗ 没有折叠 |
| 内容过滤（清 thinking/第三方注入块）两套规则 | ✗ 没有（我们靠提示词约束 + `_sanitize_message_text`） |

### 2.5 记忆

| 外部 | 我们 |
|---|---|
| bl-chat：用户记忆 100 条/人 + **群共识记忆 50 条/群**（topic/rule/meme/event/member）+ 注入 8/6 条、≤1200 字 + **防抖批量提取**（用户静默 45s / 6 条；群 10 分钟 / 12 条） | 群记忆 + 群成员记忆 + 私聊参与者记忆（都开）；idle finalize + digest 推 Memory Server；群成员画像 |
| AstrBot：群 ICL 开关 + `group_message_max_cnt 1000`；插件记忆注入时机 `pre_decision` / `post_decision` 可选 | 记忆只影响"回复内容"，不参与"要不要回"（等价于 post_decision）✓ |
| 语义召回（embedding）topK（bl-chat 默认关） | 记忆在 Memory Server 侧（宿主），插件只管推送 |

**结论**：**群共识记忆的分类**（群规/梗/事件/成员共识）值得抄一版进我们的提示词层；
防抖批量提取的阈值（45s / 6 条 / 10 分钟 / 12 条）也可以作为我们 idle finalize 的对照。

### 2.6 拟人化

| 外部 | 我们 |
|---|---|
| 分段回复（按句/换行，随机 1.5~3.5s 间隔，>150 字才分）；打字模拟（15 字符/秒、上限 3s）；**错字生成 2%**；`waitTool`「稍后再说一句」（1~60s）；复读跟读 | 只在**投递前**有 0.2~2.5s 随机延迟；没有分段、没有打字速率模拟、没有错字、没有 waitTool；有复读跟随（>5 人 + 焦点群） |
| **沉默状态机**（拟人模式）：连续 `3` 条未回复 → 进入沉默（最长 `600s`，沉默中收 `8` 条自动醒来）；动态阈值 `1~3`；兴趣关键词概率 `+0.25` | 没有"她主动安静一段"的状态（我们的退避是**不接话**，不是"状态上安静"）；也没有兴趣关键词加权 |
| **对话疲劳 + 结束语**：连续对话 `3/5/8` 条 → 概率衰减 `0.1/0.2/0.35`；**中度以上 30% 概率发"我先忙了"**（拟人收尾） | 疲劳被我们按使用者要求删掉（只留情绪）；"收尾语"没有 |
| 情绪：连续值（每小时衰减 0.02，被夸 +0.1 / 被骂 −0.15） | 9 档离散倍率 + `emotion_display` |

**结论**：这是**差距最大的一块**。分段 + 打字节奏 + 收尾语是"像真人"最直接的杠杆，
而我们只有"整条回复延迟一点"。优先级建议：① **分段（按 `\n`/标点）+ 每段随机间隔**
（提示词里她已经会换行分段，落地成本低）；② **收尾语**（聊久了主动说一句"我先忙了"
比突然消失更像人）。

### 2.7 频率与防刷屏

| 外部 | 我们 |
|---|---|
| AstrBot 插件：窗口 `300s`、**硬上限 4 条**，软限 `0.6`（≈2 条时**在提示词里告诉 AI 少回**）；重复过滤（最近 5 条 / 1800s） | 回复频率闸：**60s 内 3 条**强制静默（硬闸）；表情包冷却 5 条；没有重复过滤、没有软提示 |
| AstrBot 核心：消息速率 `60s/30 条`（stall 或 discard）；长回复折叠成"转发消息"（1500 字） | 并发闸 3（处理侧）；没有长回复折叠 |
| bl-chat：Gate prompt 里显式告诉模型"群 5min≥30 条→倾向沉默、我已回≥5 条→别刷屏" | 我们是纯硬闸，不"提醒模型" |

**结论**：我们的硬闸更严（口径也不同：他们限**收到的消息速率**，我们限**自己的回复条数**）。
缺两件小事但都值得做：**重复过滤**（防同一句卡带）与**软提示**（到阈值前先在提示词里收敛，
而不是到点直接静默——静默在连续对话里很突兀）。

### 2.8 权限与名单

| 外部 | 我们 |
|---|---|
| AstrBot：ID 白名单（默认开、空列表=不过滤）+ 管理员无视白名单 | 名单式：用户 admin/trusted/normal/**blacklist**（今天刚加）、群 trusted/normal/none；私聊一律回 |
| bl-chat：群白名单 + 仅主人管理 + 群主/管理员可清群记忆 | 群级别 + 信任名单 + admin 层 |
| 内容安全：AstrBot 内置词表 + 百度审核 | 黑名单词表（负优先级标签）+ 用户黑名单；**没有外部审核 API** |
| 禁言保护 | 双方都有 ✓ |

### 2.9 主动发言

| 外部 | 我们 |
|---|---|
| 沉默阈值（1800s）/ 概率（0.2）/ 检查间隔（120s）/ 要求近期有人活跃 / **连续被无视 3 次 → 冷却 2400s** / 安静时段 23:00-07:00 + 平滑过渡 / 自适应互动评分（成功 +15、被无视 −10）/ 吐槽系统 / 冷群 deferred 唤醒（120~900s） | 冷场破冰（同一焦点群连续 5 次无人说话）+ 主动私聊（静默 300s）+ backlog 通知；**没有失败冷却、没有安静时段、没有"被无视"反馈** |

**结论**：我们缺"**主动发言的失败保护**"（被无视后该收敛）与"安静时段"。
这是很容易被使用者感知的体验差异（深夜自言自语最招人烦）。

### 2.10 风控与合规

| 外部 | 我们 |
|---|---|
| **官方硬限**（可核实）：群被动回复 5 分钟 / **5 次**；单聊 60 分钟 / 4 次；群主动 **60/qpm**（未认证 30/qpm）、单关系 20/qpm、**1000 条/群/日**；频道每子频道每秒 5 条；WS 错误码 **4008**「发送 payload 过快」，`session_start_limit.total=1000 / reset_after=86400000ms`；`msg_id+msg_seq` 幂等；撤回限 2 分钟 | 我们**没有**：主动消息频控计数（破冰/主动私聊没有"每天多少条"上限）、**`msg_seq` 幂等**、撤回能力 |
| 第三方协议端的"安全频率" | **未验证**：NapCat 官方安全页只给定性建议（别与常用号同 IP/设备、换号换设备、SOCKS5），**没有任何 QPS/QPM 数字**；网上流传的"风控阈值"多来自 AI 生成的 SEO 站，与官方文档矛盾且无出处 → 拒绝采信 |
| 多 bot 同群防死循环（OpenClaw pair 限速 + bot 消息不触发回合） | ✗ **完全没有**：同群若有另一个 bot，我们可能与它互相触发（而且 NapCat 默认不上报自己的消息，我们**看不见自己**，更容易误判） |
| 敏感内容规避（把长文本转图发） | ✗ 没有 |
| 内容安全 API（AstrBot 接百度审核） | ✗ 没有（只有本地词表黑名单） |

**结论**：官方通道那套数字是**可以照抄的硬约束**（尤其"每天 1000 条/群"和 msg_seq 幂等）；
第三方协议端的风控**没有可信数字**，只能靠保守行为（我们的回复频率闸 60s/3 条 + 并发闸 3
本身就是一种保守）——这一点上"抄不到数字"要如实记下来。

## 3. 差距清单与建议（按性价比排序）

| # | 建议 | 依据（外部原文） | 成本 | 收益 |
|---|---|---|---|---|
| 1 | **分段回复 + 每段随机间隔**（她已经在换行分段，只是被当一条发出去） | AstrBot `segmented_reply`：按句切、随机 1.5~3.5s、>150 字才分；bl-chat 分段默认开 | 小 | 拟人化最直观 |
| 2 | **`msg_seq` 幂等去重 + 主动消息频控计数**（群 60/qpm、1000 条/群/日） | 腾讯官方文档：`msg_id` 会重复推送、同 `msg_id+msg_seq` 重复发送会失败 | 小 | 避免重复回复/被限流（官方通道尤其） |
| 3 | **重复过滤**（最近 5 条 / 30 分钟内不重发同一句）与**长回复折叠**（超 ~1500 字转"转发消息"） | AstrBot 插件 `duplicate_filter`；AstrBot 核心 `forward_threshold 1500` | 小 | 防"卡带"与刷屏 |
| 4 | **频率软提示**（到硬闸 60% 时先在提示词里让她收敛） | AstrBot 插件 `reply_density_soft_limit_ratio 0.6` + `reply_density_ai_hint` | 小 | 比"到点直接静默"更自然 |
| 5 | **主动发言失败保护 + 安静时段**（被无视 N 次进冷却；深夜不主动开口） | AstrBot 插件：连续被无视 3 次 → 冷却 2400s；安静时段 23:00-07:00 + 30 分钟过渡 | 小 | 减少打扰，最容易被使用者感知 |
| 6 | **收尾语**（聊久了以一定概率主动结束："我先忙了"） | AstrBot 插件疲劳模块 `fatigue_closing_probability 0.3` | 小 | 比突然消失更像人 |
| 7 | **同群多 bot 防死循环**（识别 bot 消息 / pair 滑动窗口限速 + 冷却） | OpenClaw `botLoopProtection`：20 事件/60s、冷却 60s；bot 消息可"只当上下文不触发回合" | 中 | 与其它 bot 共存时的保命机制（NapCat 默认不上报自己的消息，更容易误判） |
| 8 | **边界区间交给小模型"读空气"** | AstrBot 插件读空气 AI（超时=不回、可开推理）；bl-chat Timing Gate（continue/no_action/wait、15s 超时） | 中 | 补上我们缺的语义判断（吵架/玩梗/阴阳） |
| 9 | **用户级注意力**（在群里跟谁聊得热）+ 溢出 + 两段式冷却 | AstrBot 插件注意力机制（0~1、半衰期 300s、溢出 30%、待冷却保底 0.18） | 中 | 与我们群级注意力正交，能补"熟人感" |
| 10 | **群共识记忆分类**（群规/梗/事件/成员共识）与防抖批量提取 | bl-chat 群记忆五类 + 注入上限（8/6 条、1200 字）+ 用户 45s/6 条、群 10 分钟/12 条 | 中 | 群氛围一致性 |
| 11 | **内容安全**（外部审核 API，或把易触发的长文本转图发） | AstrBot `content_safety`（内置词表 + 百度审核）；bl-chat `textImageTool` | 中 | 账号安全 |
| 12 | **官方通道的全量模式 + 平台 ASR** | 官方文档确认 `GROUP_MESSAGE_CREATE`（开启"接收所有消息"后每条都推）；语音附件带 `asr_refer_text` | 中 | 官方通道也能做群聊感知，且省掉自建 STT |
| 13 | **`focus_groups`：把"其实是一个场景"的群共享焦点** | MaiBot `experimental.focus_groups`（同组共享、不同组互不抢占） | 小 | 避免姊妹群互相抢焦点（我们现在是全局平铺竞争） |
| 14 | **`talk_value` 式的"整体安静度"总旋钮**（按时段/按群） | MaiBot `talk_value` + `talk_value_rules`（支持跨夜时段）；bl-chat `talkValue` | 小 | 现在要"更安静"只能去改 necessity 阈值或各种倍率，缺一个直觉旋钮 |
| 15 | **正则黑名单**（`ban_msgs_regex`）与**提到名字更容易回**（`mentioned_bot_reply`） | MaiBot `message_receive.ban_msgs_regex`（启动时校验）、`reply_timing.mentioned_bot_reply` | 小 | 前者补我们"只有关键词表"的短板，后者是低成本的礼貌信号 |
| 16 | **中期记忆 / 聊天回想**（最近 N 条之外的"最近发生过什么"） | MaiBot `mid_term_memory` + `mid_term_memory_lenth 10` | 中 | 我们现在只有"长期群记忆"与"当前上下文"两档 |

### 3.1 对账：我们抄 MaiBot 抄得准不准

| 我们的实现 | MaiBot 官方默认 | 结论 |
|---|---|---|
| `IdleBackoff`：`min(300, 15 × 2^(n−2))`、`START_COUNT = 2`、`BYPASS_PENDING = 6` | `no_action_backoff_base_seconds 15`、`cap_seconds 300`、`start_count 2`、`bypass_pending_count 6` | **四个常数逐一相同** —— 第一轮移植是准的（现在有了官方文档作为出处） |
| necessity 阈值 `40`（真机回放定） | MaiBot 用 `80`，但它那一关还承担"攒够几条才值得思考" | 我们知道这个差异（第一轮已记录），40 是按我们"焦点群内部"这一层重新定的 |
| @ 必回 | `inevitable_at_reply true` | ✓ 一致 |
| —— | `reply_trigger_mode` 可在 `frequency` / `reply_necessity` 之间切 | 我们**并成了一条**（有意的简化；代价是失去了"按群回到纯频率模式"的退路） |

**待办提示**：如果以后真的需要"某些群退回到纯频率模式"，MaiBot 那个 `reply_trigger_mode`
就是现成的设计参考。

**不建议照搬的**：

- **概率门槛**（`initial_probability 0.02` 那套）：我们已有确定性打分，再加一层概率会互相打架。
- **`unique_session`（每人独立上下文）**：与我们的"群聊感 + 群记忆"设计相反。
- **情绪连续值**：我们的 9 档离散倍率已经够用、可解释、可调。
- **照抄"安全发送频率"数字**：第三方协议端**没有**可信数字（子代理专项检索结论：只有官方
  开放平台有明文频控表；网上流传的阈值多来自 AI 生成的 SEO 站）。

## 4. 未验证（禁止外推）

- 上面各家的**默认值**是否等于**推荐值**：文档给的是默认，不代表作者推荐。
- AstrBot 插件的"注意力机制"与"对话疲劳"两节的**具体字段与公式**本轮没读到（只看到目录里有这两节）。
- MaiBot 的 `talk_value` / 心流具体公式、框架层四家（NoneBot2 / Koishi / Yunzai / ZeroBot）的
  会话与冷却实现、协议端的风控经验数值 —— 见 §5（子代理交付，另行标注出处）。
- 我们的"60s 内 3 条"回复闸与他们的"60s 30 条消息速率"**口径不同**，不能直接比较松紧。

## 5. 源码级细节（子代理交付）

- **框架层（NoneBot2 / Koishi / Yunzai / ZeroBot）**：已并入 §1.7（源码级证据来自 nonebot2 与
  Miao-Yunzai 的 Gitea 镜像、ZeroBot 官方 docs 与 pkg.go.dev）。
- **协议端与账号安全（NapCat / LLBot / Lagrange / go-cqhttp + QQ 官方）**：已并入 §1.6 与 §2.10。
- **MaiBot 系（含衍生）源码级**：待补 —— 要核的是 `reply_necessity` 的完整评分表与阈值、
  heartflow 的 `talk_value`/发言时机公式、idle backoff、群上下文构建、多群调度。
- **AstrBot / LangBot 的流水线阶段与群响应规则**：待补 —— 要核的是两家"多条件取或"的具体
  实现与阶段清单（第一轮文档给的是阶段数，本轮要落到字段）。

（未交付的一律保持"未验证"，不用默认值外推。）
