# 群聊「该不该接话」机制调研（外部项目对照 + 我们插件的改造方案）

> 目的：我们现在的「像不像接话」判定只有一个布尔启发式（`message_dispatcher.py:426` `_looks_like_human_followup`：短句/接话前缀/问号结尾）。本文严肃调研开源 QQ/群聊 bot 工程与多方可对话做法，给出**带出处**的机制对照，并据此设计状态化判定。
>
> 所有结论都标注了取证方式：**【原文】**=读到了源文件/官方文档原文（附路径或 URL）；**【二手】**=来自可靠二手描述但未读原文；**【未验证】**=没找到或没读到，禁止外推。

---

## 0. 取证方式与限制（先读，影响可信度判断）

本机环境的两个坑，值得记下来供后续复用：

1. **harness 的 `web_fetch` 在本机全域名失效**：DNS 把 `github.com`/`raw.githubusercontent.com` 指向 `127.0.0.1`，其余域名解析到 `198.18.x.x`（非公网），web_fetch 一律以 `resolves to a non-public IP address` 拒绝。
2. **但网络本身是通的**。可用路径：
   - 直连抓取任意 URL：`.dsh-artifacts/fetch.py`（HTML 自动转纯文本）
   - GitHub 仓库文件/文件树：`.dsh-artifacts/fetch-gh.py`（走 jsDelivr，`data.jsdelivr.com` 拉树、`cdn.jsdelivr.net/gh/...` 拉文件）
   - `gh-proxy.com` 镜像在 `web_fetch` 下可用（子代理发现）
3. **抓下来的非 ASCII 文件必须用 read 工具读**，不要走 PowerShell `Get-Content`（GBK 乱码，历史上毁过一次 `docs/SESSION-HANDOFF.md`）。

被调研项目与被读到的版本：

| 项目 | 仓库 / 分支 | 读到的内容 |
|---|---|---|
| MaiBot（MaiCore） | 现行仓库 `Mai-with-u/MaiBot@main`（`DrSmoothl/MaiBot` 为旧路径，jsDelivr 上两者都能取到，但内容新旧不同——**引用行号前必须确认 slug**） | **1.x「Maisaka」架构**：`src/maisaka/focus/{manager,runtime_mixin}.py`、`prompts/zh-CN/maisaka_chat_focus.prompt`、`src/maisaka/{reasoning_engine,chat_loop_service}.py`、`src/maisaka/builtin_tool/{wait,no_action,continue_tool}.py`、`src/chat/heart_flow/*`、`src/config/official_configs.py`（187,765B）；**0.x「心流」架构**（仅存于 GitLab 旧 commit，`@main` 已 404）：`src/plugins/willing/mode_*.py`、`src/heart_flow/sub_heartflow.py`、`src/plugins/person_info/relationship_manager.py` |
| AstrBot | `AstrBotDevs/AstrBot@master`（子代理核到 v4.28.1 / `69ae35a`） | `astrbot/core/pipeline/**`、`builtin_stars/astrbot/group_chat_context.py`、`core/config/default.py` |
| LangBot | `langbot-app/LangBot@master`（子代理核到 `5e21d02` / 4.11.0-beta.3） | `pkg/pipeline/{resprule,ratelimit,bansess,cntfilter}**`、`templates/metadata/pipeline/trigger.yaml` |
| ChatLuna | `ChatLunaLab/chatluna`（子代理核到 `2f69f09`） | `packages/core/src/config.ts`、`middlewares/chat/{allow_reply,cooldown_time,message_delay,chat_time_limit_check}.ts` |
| nonebot-plugin-llmchat | `git.fuquan.moe/fuquan/nonebot-plugin-llmchat`（0.6.0） | 子代理读源 + patch |
| KiraAI | `xxynet/KiraAI@main`（v2.34.7） | 子代理读 12 个源文件 + 文件树 |
| Astrbot_plugin_Heartflow | `advent259141/Astrbot_plugin_Heartflow`（v2.3.0） | 子代理读 `main.py`、`_conf_schema.json` |
| QQ 机器人官方文档 | `bot.q.qq.com/wiki/develop/api-v2/server-inter/message/*`、`bot.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html` | 官方原文 |

---

## 1. 结论速览

1. **「要不要回」在开源实现里几乎都是「事件级二值门控 + 无记忆概率」**：`@`/引用/昵称/前缀/正则命中就回，否则 `random() < p`（LangBot `random` 默认 0、AstrBot `active_reply` 默认关且 `possibility_reply=0.1`、ChatLuna `randomReplyFrequency` 默认 0、llmchat `random_trigger_prob` 默认 0.05、KiraAI `group_proactive_chat_probability` 0.1）。**没有一家把「谁在跟谁说话」做成状态**。
2. **真正的状态化建模只有两条路线**：
   - **MaiBot 路线**：焦点槽（同一时间只有一个会话在决策）+ 冷却唤醒 + `@` 抢占 + Timing Gate（`continue`/`no_action`/`wait`）+ Planner 工具化决策。**与我们的「竞态/焦点」最接近，但它的仲裁在开放平台通道上才是活的**。
   - **Heartflow 路线（AstrBot 第三方插件）**：能量（energy）+ 五维加权评分 + 阈值 0.6 + 判官 LLM，专管「没人叫 bot 时要不要插话」，且**刻意排除被 @ 的消息**。
3. **多群并发仲裁在主流实现里普遍缺失**：AstrBot/KiraAI/LangBot 的限流都按会话分桶，**没有任何全局配额或「哪个群优先」的调度**——插话总量随活跃群数线性增长。这正是我们 `attention_service` 试图解决、却在当前通道上被短路掉的问题（`attention_gate_service.py:200`）。
4. **`@` 语义各家的实现高度一致**：@bot / 引用 bot 的消息 / 唤醒前缀开头 / 私聊默认唤醒；且都处理了一个我们没处理的细节——**群聊首段 @ 的是别人时不算「在叫我」**（AstrBot `waking_check/stage.py:117-131` 原文注释：`如果是群聊，且第一个消息段是 At 消息，但不是 At 机器人或 At 全体成员，则不唤醒`）。
5. **[原文·官方文档] 我们当前通道的真相**：QQ 开放平台群聊**只推送 @ 机器人的消息**（`GROUP_AT_MESSAGE_CREATE`）。我们插件的 intent 掩码是 `(1 << 25) | (1 << 12)`（`_vendor/connection_onebot/qq_open_plat.py:351`），而「群消息（全量模式）」`GROUP_MESSAGE_CREATE` 的 Intent 也是 `GROUP_AND_C2C_EVENT (1<<25)` —— **即：只要在开放平台后台开启「接收所有消息」，事件就会推到我们已经在订阅的这条 websocket 上，我们只需要在代码里处理这个事件类型**。在此之前，「该不该插话」这个问题在群聊里**根本不存在**：我们能收到的每条群消息都已经被 @ 了。

6. **我们现在的「像不像接话」规则，正好是文献里最弱的 baseline**：「取最近 4 条里第一条非自己发言，≤60s 就抑制」= addressee 研究里的 preceding-speaker 启发式，短会话 p@1 63.5%、**长会话 Acc 只有 13.08%**（Le et al. 2019 Table 3）；而 CHI 2025 的 Inner Thoughts 直接论证：在**没人被点名**的场景，next-speaker prediction **本质上不适定**，应该改成 LLM 按 8 条启发式打 1–5 分、过阈值才说话。
7. **两个可以直接抄的节流机制**：MaiBot 的**空闲退避**（连续不回复 → 检查间隔从 15s 起、最长 300s，但积压 6 条消息立刻醒）与**动态频率乘子**（≥160s 且 ≥20 条才用 LLM 判「过于频繁/正常/过少」，`×0.8/×1.0/×1.2`，钳制 `[0.1,1.5]`）；以及 Bocchy 的**热度分档冷却**（excitement 9–10→20s … 1–4→300s）。

---

## 2. 门控骨架对照

### 2.1 AstrBot：9 阶段责任链【原文】

`astrbot/core/pipeline/stage_order.py`：

```python
STAGES_ORDER = [
    "WakingCheckStage",        # 检查是否需要唤醒
    "WhitelistCheckStage",     # 检查是否在群聊/私聊白名单
    "SessionStatusCheckStage", # 检查会话是否整体启用
    "RateLimitStage",          # 检查会话是否超过频率限制
    "ContentSafetyCheckStage", # 检查内容安全
    "PreProcessStage",         # 预处理
    "ProcessStage",            # 交由 Stars 处理（a.k.a 插件），或者 LLM 调用
    "ResultDecorateStage",     # 处理结果，比如添加回复前缀、t2i、转换为语音 等
    "RespondStage",            # 发送消息
]
```

调度是**洋葱模型**（`scheduler.py:44-80`）：stage 返回 `AsyncGenerator` 时先跑前置、再递归跑后续、再回来跑后置；任意一步 `event.stop_event()` 即终止传播。这比我们的「一串 if/return」更易扩展，但语义等价。

唤醒条件（`waking_check/stage.py:113-159`）**【原文】**：
1. 以 `wake_prefix`（默认 `["/"]`）开头 → 唤醒，剥离前缀；**例外**：群聊且首段是 `At`、但 @ 的不是自己也不是 all → `break` 不唤醒；
2. `At` 且 `qq == self_id`；`AtAll`（除非 `ignore_at_all`）；**`Reply` 且 `sender_id == self_id`（引用 bot 的消息）**；
3. 私聊默认唤醒（除非 `friend_message_needs_wake_prefix`）；
4. 插件 handler filter 全 AND 通过。
未唤醒 → 末尾 `event.stop_event()`。

**主动回复**（`builtin_stars/astrbot/group_chat_context.py:112-130`）**【原文】**：

```python
if not cfg["enable_active_reply"]: return False
if event.get_message_type() != MessageType.GROUP_MESSAGE: return False
if event.is_at_or_wake_command: return False      # 被叫时不主动（与唤醒互斥）
if cfg["ar_whitelist"] and (umo 与 group_id 都不在白名单): return False
match cfg["ar_method"]:
    case "possibility_reply": return random.random() < cfg["ar_possibility"]
```

### 2.2 LangBot：11 阶段 + 群响应规则取「或」【原文】

阶段顺序硬编码在 `pkg/api/http/service/pipeline.py:20-32`（子代理核到）：`GroupRespondRuleCheckStage → BanSessionCheckStage → PreContentFilterStage → PreProcessor → RequireRateLimitOccupancy → MessageProcessor → ReleaseRateLimitOccupancy → PostContentFilterStage → ResponseWrapper → LongTextProcessStage → SendResponseBackStage`。

群响应规则（`pkg/pipeline/resprule/resprule.py:52-62`）**【原文·本人复核】**：

```python
for rule_matcher in self.rule_matchers:  # 任意一个匹配就放行
    res = await rule_matcher.match(...)
    if res.matching:
        query.message_chain = res.replacement
        return StageProcessResult(CONTINUE)
return StageProcessResult(INTERRUPT)
```

四条规则（`resprule/rules/`）：`at-bot`（找到并**删除** @bot 段，连删两次因为「回复消息时会 at 两次」）、`prefix`（命中即**就地剥掉前缀**）、`regexp`（`SafeRegexError` 兜底）、`random`（`random.random() < rule_dict['random']`）。
默认值（`templates/metadata/pipeline/trigger.yaml`）**【原文】**：`at=false`、`prefix=[]`、`regexp=[]`、`random=0`。
> 注意：规则的副作用（删 @、剥前缀）与 `replacement` 机制意味着**「判定」与「消息净化」是同一趟**——我们的 `_looks_like_human_followup` 只判定、不净化。

### 2.3 我们插件（对照）

10 个出口（`attention_gate_service.py:200-298`），**第 1 个出口在开放平台通道上直接返回**：

```python
if self.plugin.qq_client and not self.plugin.qq_client.needs_attention:
    return GateDecision("reply", reason="no_attention_needed", force_reply=is_at_bot)
```

其后是 @锁/黑名单/非焦点让位/焦点过低/关键词唤醒/回复猫娘/突发闸/焦点群。另有一层在 dispatcher 之外的**插话抑制**（`message_dispatcher.py:442`），只在 `strategy_mode != "neko_dynamic"` 时计算（`:965`）。

---

## 3. 「要不要回」的判定因子对照

| 因子 | MaiBot | AstrBot | LangBot | ChatLuna | llmchat | KiraAI | 我们 |
|---|---|---|---|---|---|---|---|
| @ bot | ✅ | ✅ | ✅（`at`，删 @ 段） | ✅（`allowAtReply`，比对元素 id） | ✅（`event.is_tome()`） | ✅ | ✅（但通道上恒真） |
| @ 全体 | — | ✅（`ignore_at_all` 可关） | — | — | — | ⚠️ **误判为提及 bot**【原文 `qq.py:564`】 | `mentions_all`（open_plat 里恒 False） |
| 引用 bot 消息 | — | ✅ | — | ⚠️ `allowQuoteReply` 默认 **false** | — | ✅（`type=="reply"` 比 `user_id`） | `is_reply_to_bot`（在门控链里，当前不可达） |
| **首段 @ 别人 → 不唤醒** | — | ✅ | — | — | — | — | ❌ |
| 昵称/别名 | `alias_names`（`is_mentioned_bot_in_message`，当前版本该函数在 heartflow 里被注释掉了） | — | — | ✅ `isNickname`(默认 true，**开头**匹配) / `isNickNameWithContent`(默认 false，任意位置) | — | ✅（`waking_words` 子串） | ❌ |
| 前缀唤醒 | — | ✅ `wake_prefix` | ✅ `prefix` | ✅（命令/预设） | ✅ `ignore_prefixes`（反向） | ❌ | 无 |
| 正则 | — | — | ✅ `regexp` | — | — | — | backlog 标签关键词 |
| 概率 | — | ✅ 0.1（默认关） | ✅ `random`（默认 0） | ✅ `randomReplyFrequency`（默认 0） | ✅ `random_trigger_prob` 0.05（**可单群覆盖**） | ✅ 0.1 | `open_reply_probability` 0.1（**neko_dynamic 下不生效**，见 `reply_decision_node.py:73-83`） |
| 频率/限流 | `talk_value` 0–1 + 时段规则 | `rate_limit{time:60,count:30,strategy:stall}`（按 UMO） | `rate-limit{window-length:60,limitation:60,strategy:drop}` | `msgCooldown`（默认 0）+ `chatTimeLimit` 200/小时/模型 | ❌ 无 | ❌ 无（只有全局信号量 3） | 突发闸 60s/3（门控链里，当前不可达） |
| 白/黑名单 | — | ✅ `id_whitelist`（默认开、表为空） | ✅ `access-control{mode,blacklist,whitelist}`，支持 `group_*`/`*_123` | ✅ `blackList`（Koishi 条件属性） | ✅ `blacklist_user_ids` | ✅ 群 allow/deny list | ✅ 群/用户分层（trusted/open/normal） |
| 消息合并/防抖 | — | ✅ `empty_mention_waiting`（只 @ 时等 60s） | ✅ `message-aggregation`（默认关，delay 1.5s，缓冲 10 条） | ✅ `messageQueue`+`messageQueueDelay`（默认 0 → 实际不防抖） | ✅ `pending_events`（概率判定**之前**入队） | ✅ debounce 2s，`max_buffer_messages` 5 | ✅ 群 5s / 私 1s 缓冲 |
| LLM 自判弃权 | ✅ Planner 的 `no_action`/`finish` | — | — | — | ✅ `<<<LLMCHAT_NO_REPLY>>>` | — | ✅ `[LLM自判]` |

---

## 4. 状态变量对照（我们最缺的部分）

| 状态 | MaiBot | AstrBot | Heartflow | ChatLuna | llmchat | 我们 |
|---|---|---|---|---|---|---|
| 焦点槽（同一时间一个活跃会话） | ✅ `FOCUS_SLOT_LIMIT = 1`，按 scope 分组（`focus_groups` 配置，不配则全局共享一个） | ❌ | ❌ | ❌ | ❌ | ✅（但当前通道空转） |
| 冷却/唤醒时间戳 | ✅ `focus_cool_time`（默认 **120s**）；超时后「被其它会话的新消息唤醒一次」；`@` 可**无视冷却**立即抢占 | ❌ | ✅ `last_reply_time`/`last_trigger_time` + `min_reply_interval_seconds`（默认 0） | ✅ `msgCooldown`（**全局单变量** `lastChatTime`） | ❌ | ✅ 锁 90s / 蜜月 60s / 焦点线 4.0 / 保持线 2.0 |
| 未读计数 | ✅（focus overview 里按 chat 显示「未读（未决策消息）消息数」） | ✅ `raw_records[umo]`（消费式队列，注入后只留该条之后） | — | — | ✅ `pending_events`（maxlen） | ✅ backlog（757KB 落盘） |
| 能量/预算 | — | — | ✅ `energy` 1.0，回复 `-0.1`、未回复 `+0.02`、每 5 分钟恢复、跨日 `+0.2`（默认值来自 `_conf_schema.json`） | — | — | ✅ 疲劳（作息 + 负载），但只调速不判门 |
| 多维评分 | — | — | ✅ 五维加权 `0.25/0.2/0.2/0.15/0.2` + 阈值 `0.6` + 权重可配 | — | — | ⚠️ 注意力单标量 0–10 |
| 情绪/心情 | ✅（`emotion` 影响进退速率与抢/让焦点） | ❌ | — | — | — | ✅ 9 档倍率（半活，见 §7） |
| 「上次回复后群里怎么反应了」 | ✅ retro review（回溯补回） | ⚠️ Follow-up 门票（同发送者才抢占） | ✅ **接话反馈**：上次回复后 ≥3 条 → 注入「（上次回复后群里进行了热烈讨论）」；0 条 → 「（上次回复后无人接话）」 | — | — | ❌（只有回溯补回，不喂回评分） |
| 谁在跟谁说话（addressee） | ⚠️ 仅 `is_mentioned` 标记 + `⚠️[DIRECTED AT YOU]` 风格提示（AstrBot 有） | ✅ 提示层标记 | ❌ | ⚠️ 非 bot 的 @ 渲染成 `<at name=... id=.../>` | ❌ | ❌（只有 `mentions_other_user` 布尔） |

---

## 5. 三个最值得移植的机制（附出处）

### 5.1 MaiBot：焦点槽 + 冷却唤醒 + `@` 抢占【原文】

`src/maisaka/focus/manager.py`：
- `FOCUS_SLOT_LIMIT = 1`；`try_enter_focus` 在槽满时返回 `False`（`:215-216`）；`release_focus_and_block_next_entry` 可以让出槽并**阻止它抢下一次**（`:241-259`）。
- `focus_groups`（`experimental.focus_groups`，默认 `[]`）是**互通组**：不配置时所有启用 Focus 的聊天共享一个焦点；配置后**不同组可以同时 Focus**——这正好对应我们「同时活跃的群」问题。
- `get_focus_cool_time()` 默认 **120 秒**（`official_configs.py:883-900`，原文注释：`Focus 模式下关注聊天超过该秒数没有进入循环时，会被其他聊天的新消息唤醒一次`）。
- `experimental.focus_mode` 默认 `False`，注释：`开启后仍正常创建聊天流，但同一时间只有一个 Maisaka 处于活跃关注状态，且忽略聊天频率控制`。

`src/maisaka/focus/runtime_mixin.py`：
- `_arm_focus_cooldown_timer()`：焦点会话空闲时武装一个定时器（`:135-144`），到点若**别的运行中会话有未读消息**，就把决策权交给它（`:146-170`）。
- `_maybe_schedule_focus_at_wakeup()`：**别的会话里有人 @ 了 bot → 无视冷却立即抢占**（`:200-219`，`ignore_cool_time=True, wakeup_reason="at"`）。
- `FOCUS_NO_ACTION_EXIT_THRESHOLD = 5`：连续 5 次 `no_action` → **主动释放该群的焦点**（`record_no_action_cycle_result`，`:92-125`）。这是「久坐焦点」的自愈机制，比我们固定 90s 锁更贴合「群不活跃就让位」。
- `FOCUS_SWITCH_NEW_MESSAGE_LIMIT = 20`：切换焦点时自动接入目标会话最多 20 条未读消息（`:455`、`:573-589`）。
- `_get_pending_attention_flags()`：逐条消息的 `is_mentioned` 标记（`:44-50`）。
- focus overview 给模型的字段（`:384-400`）：`未读（未决策消息）消息数`、`是否有人提及{bot_name}`、`最新一条消息`。

### 5.2 MaiBot：Timing Gate（把「什么时候说」做成工具）【原文】

`prompts/zh-CN/maisaka_chat_focus.prompt` 原文要点：
- `同一时间只有一个聊天处于活跃关注状态，你只在这个聊天中进行决策`；末尾收到 `<focus_chat_overview>`；
- `如果某个聊天显示有人 @ 或提及 {bot_name}，或者未读过多、内容更值得关注，优先考虑使用 switch_chat 切换过去`；
- 工具：`reply` / `fetch_histroy` / `switch_chat` / `query_memory` / `no_action` / `finish`；
- **`query_memory` 的使用政策**：`群聊里更克制；私聊里如果对方提到"之前""上次""最近""还记得吗""我喜欢""我说过"等类似的信号，可以更积极考虑检索` —— 与我们 §4.0ai 的召回提示同源，但它是**分场景强弱**的。

`src/maisaka/builtin_tool/`：
- `wait(seconds)`：`暂停当前对话并固定等待一段时间，期间不因新消息提前恢复`；
- `no_action()`：`本轮暂时不进行任何动作，等待新的外部消息`，保留连续 Planner 状态；
- `continue()`：进入下一轮；`finish()`：结束连续 Planner。

`src/maisaka/reasoning_engine.py` 常量：`TIMING_GATE_TOOL_NAMES = {"continue","no_action","wait"}`、`TIMING_GATE_MAX_ATTEMPTS = 3`（非法动作按 `no_action` 处理）、`PLANNER_NO_TOOL_FINISH_THRESHOLD = 3`（连续 3 轮没调工具视为 finish）、`HISTORY_DEFERRED_TOOL_RESULT_NAMES = {"wait"}`。

**对我们的启示**：我们现在只有「回 or 不回」，缺少「**等一下再说**」这个动作。`wait` + `no_action` 的组合能让模型自己控制节奏（尤其在群里多人同时说话时），而且它的等待语义是「**不因新消息提前恢复**」——这是一个明确的取舍，值得照抄。

### 5.3 AstrBot / Heartflow：消费式群上下文 + 心流评分 + 接话反馈【原文·子代理核】

- **消费式队列**（AstrBot `group_chat_context.py:162-197`）：LLM 请求时把该条之前的群消息一次性注入，队列只保留该条之后的内容；配 `⚠️[DIRECTED AT YOU]` 标记（`:253-260`）让模型区分「在跟我说话」与「群友互聊」。语义恰是「你上次回复之后群里发生了什么」，**天然不重复注入、不需要记「上次发言时间」**。
- **Follow-up 门票**（`process_stage/follow_up.py:186-188`）：只有**同一发送者**的新消息才能抢占正在运行的 agent，且严格按 `seq` 队头推进（注释原文 `Strict ordering: only the head (next_turn) can continue.`），无活跃 runner 时整个 UMO 条目被 `pop` 回收。→ 解决多人同时追问导致的回复错位。
- **Heartflow**（`advent259141/Astrbot_plugin_Heartflow` v2.3.0）：`energy` 衰减/恢复 + 五维加权评分（`0.25/0.2/0.2/0.15/0.2`）+ `reply_threshold=0.6` + LLM 判官（`judge_max_retries=3`、`judge_timeout_seconds=30`）+ `min_reply_interval_seconds`（默认 0）+ `max_tracked_chats=1000` LRU（跳过锁住的群）+ **生成失败回滚冷却预留**（`main.py:646-647,720-726,923-928`）。它**刻意排除被 @ 的消息**（`main.py:752-755` `if event.is_at_or_wake_command: return False`），把「被叫」交回原生流程以避免双重回复。接话反馈：统计上次回复后的用户消息数，`>=3` 注入「（上次回复后群里进行了热烈讨论）」，`==0` 注入「（上次回复后无人接话）」。

---

### 5.4 MaiBot：「频率触发」vs「必要性触发」+ 空闲退避【原文】

`src/config/official_configs.py`（`Mai-with-u/MaiBot@main`，187,765B）里 `ChatReplyTimingConfig` 的键与默认值：

| 键 | 默认 | 原文语义 |
|---|---|---|
| `reply_trigger_mode` | `"frequency"` | `frequency` = 频率触发：**按照新消息数量决定思考**；`reply_necessity` = 必要性触发：**综合新消息数量、内容、过往发言决定思考**（`REPLY_TRIGGER_MODE_OPTION_DESCRIPTIONS`，`:23-31`） |
| `talk_value` / `private_talk_value` | `1` | 聊天频率 0–1，越小越沉默（`:537`、`:554`） |
| `inevitable_at_reply` | `True` | 开启后，被 @ 时会尽量回复（`:589-597`） |
| `talk_value_rules` | 两条：`group 00:00-08:59 → 0.8`、`group 09:00-18:59 → 1.0` | 「可让麦麦在某些群、私聊或时段更活跃或更安静」（`:724-740`）；字段 `platform / item_id / rule_type / time / value`，`time` 支持 `*` 与跨零点区间 |
| `max_consecutive_wait_count` | `3` | Planner 最多连续调用 `wait` 多少次，达到上限后 `wait` 拒绝继续等待（`:631-643`） |
| `planner_interrupt_max_consecutive_count` | `0` | 思考时来了新消息，最多重新思考多少次；0 = 不限制（`:616-629`） |
| `no_action_backoff_base_seconds` | `15` | **连续决定不回复后，下一次检查前先等多久**（`:645-661`） |
| `no_action_backoff_cap_seconds` | `300` | 退避等待的最长时间（`:663-677`） |
| `no_action_backoff_start_count` | `2` | 连续几次不回复后开始放慢检查（`:679-693`） |
| `no_action_backoff_bypass_pending_count` | `6` | **等待期间新消息达到多少条就立刻重新处理**；0 = 不按条数打断等待（`:695-709`） |

**这是一整套「静默越久、检查越稀，但消息堆积到阈值就立刻醒」的自适应节流**——比我们现在的固定 60s 突发闸精细得多，而且它把「不说话」变成一个**有代价、会累积**的状态。

另有两个 1.x 新增的**提示词档位**（不是数值状态）：`experimental.emotion_trait`（`rational_calm`/`neutral`/`sentimental`，默认 `neutral`）与 `experimental.attention_drift.drift_level`（`subtle`/`active`/`scattered`/`wild`，默认 `scattered`，注释原文：`控制注意力漂移的整体表现档位，而不是用数值概率描述`）。

0.x「心流」架构（GitLab 旧 commit，`@main` 已 404）里则是一套**连续值状态机**，可作设计参考【原文·子代理读 GitLab 镜像】：

| 状态 | 取值 | 更新 |
|---|---|---|
| 回复意愿 `chat_reply_willing[chat_id]` | `0 ~ 3.0` | **每秒 ×0.9**；回复前 `-1.8`；回复后（`<1` 时）`+0.4`；被提及 `+1` / 普通提及 `+0.05` |
| 兴趣值 `InterestChatting.interest_level` | `0 ~ 15.0` | 指数衰减 `*= decay_rate ** dt`，可增减 |
| 回复概率 `current_reply_probability` | `0 ~ 1`，基准 `0.05` | 每秒 `+0.08`；跨阈值重置回 `0.05`；跌破则 `probability_decay_factor ** dt` |
| 关系值 `relationship_value` | `-1000 ~ 1000`，六档 | 阻尼函数 `cos(pi*old/2000)` / `exp(old/2000)` + 连击系数，**持久化到 MongoDB** |
| 情绪 `MoodState(valence, arousal)` | 各 `-1.0 ~ 1.0` | 指数回归中性，受 `neuroticism`/`agreeableness` 调节 |
| 全局档位 `MaiState` | `OFFLINE`/`PEEKING`/`NORMAL_CHAT`/`FOCUSED_CHAT` | 600s 检查，`random() < 0.1` 掉线；各档超时 60/600/300/600s 带权转移 |
| 可插拔意愿模式 | `classical`/`dynamic`/`llmcheck`/`custom`/`mxp` | `dynamic`：高意愿期 4~8 句 → `0.5`、低意愿期 ≥15 句 → `0.3`，否则 `0.03*min(count,10)`，上限 `0.75`；追问（同人 120s 内 ≤5 条）`+0.3`。`llmcheck`：LLM 输出 0~1，`≤0.1 → min(0.03,p)`、`≥0.8 → max(p,0.90)`，**仅在「连续对话」时触发**，白名单外的群恒为 0，睡眠时段直接 0 |

### 5.5 学术界与工业界的量化依据【原文·子代理读 PDF 原文】

**(a) 我们现在的规则，恰好就是文献里最弱的那个 baseline。**
`_detect_group_interjection_suppression` 的核心是「取最近 4 条消息，找第一条不是自己发的，若 ≤60s 就抑制」——这就是 addressee 文献里的 **preceding-speaker heuristic**（认为「下一个说话人 = 上一个说话人」）。实测：

| 规则/模型 | 指标 | 出处 |
|---|---|---|
| 随机 | ADR **1.24%** | Le et al. 2016, [D16-1231](https://aclanthology.org/D16-1231/) Table 3 |
| **回应最近说话者（规则基线）** | ADR **55.73 / 55.63 / 55.62%**（Nc=5/10/15） | 同上 |
| Preceding（短会话 Len-5） | p@1 **63.50**，Acc **40.46** | Le et al. 2019, [D19-1199](https://aclanthology.org/D19-1199/) Table 3 |
| Preceding（长会话 Len-15） | p@1 **54.97**，**Acc 仅 13.08** | 同上 |
| 最佳神经模型（dynamic speaker embedding） | ADR **68.54%** | D16-1231 |
| MPC-BERT | Acc **80.31**（Len-5）→ **52.59**（Len-15） | [2021.acl-long.285](https://aclanthology.org/2021.acl-long.285/) Table 3 |

**结论：会话越长、人越多，这个规则失效越快**（Acc 从 40% 掉到 13%）。我们的群恰好是「人多、话密」的场景，也就是这个规则最不适用的一端。（诚实标注：`Preceding` 的数值在两篇论文间口径不同——Le 2019 自报 p@1 63.5，MPC-BERT/GIFT 引用版为 55.73——**跨论文比较不安全**，只看同篇内趋势。）

**(b) 结构信号（谁回谁/谁对谁说）比文本线索更有价值。**
- HeterMPC（[ACL 2022](https://aclanthology.org/2022.acl-long.349/)）把 utterance 与 interlocutor（speaker/addressee）做成两类节点、六种关系边（`reply/replied-by/speak/spoken-by/address/addressed-by`）；**消融去掉 interlocutor 节点，BLEU-1 从 12.61 掉到 11.80**——0.8 BLEU 就是「谁对谁说」的净贡献。
- MPC-BERT 的 **RUR（reply-to-user recognition）** 任务对 addressee 识别贡献最大（去掉后 92.42 → 89.98）。
→ 我们已经有 `is_reply_to_bot` / `quoted_message_id` / `mentions_other_user`，但**没有把「谁回谁」做成图/计数状态**。

**(c) LLM 时代的范式：把「该不该接话」做成 1–5 分打分器 + 表达阈值。**
Inner Thoughts（CHI 2025，[arXiv:2501.00383](https://arxiv.org/abs/2501.00383)，24 人 × 4 场同步群聊的 think-aloud 研究，394 条引文 → 10/23/68 层主题）给出 **8 条接话启发式**，按提及次数排序：
**Relevance（77）> Information Gap（33）> Expected Impact（23）> Urgency > Coherence > Originality > Balance > Dynamics**。
实现：类 G-Eval 的 CoT prompt 对候选发言打 **1–5 分**，**同时要求给出正面与负面动机**（作者称可减少高估），`im_score ≥ imThreshold(1–5)` 才发言；在 7 项指标上全面优于「next-speaker prediction + persona」基线，**82% 被试更偏好**。核心论点：next-speaker prediction 在 **self-selection（没人被点名）** 场景**本质上不适定**——"no deterministic mapping between prior utterances and the next speaker"。
→ 这对我们很关键：**「像不像接话」这种规则式判定，在没人被点名时注定只能是个弱基线；真正的出路是让 LLM 按维度打分。**

MUCA（[arXiv:2401.04883](https://arxiv.org/abs/2401.04883)）给出 3W 框架（**What / When / Who**）与子话题状态机（`not_discussed` / `being_discussed` / `well_discussed`），并用「参与度均衡（conversation evenness）」作为指标——它明确警告：主动搭话（pinging a lurker）必须谨慎设计 **timing / frequency / contents**，否则带来负面感受。Inner Thoughts 同样明确：**「只被动响应」和「永远在响应」两个极端都不好**。

**(d) 工业界的频率自适应与冷却分档（二手，DeepWiki 基于源码）**
- MaiBot 动态频率控制：`talk_value` 作为 `random.random()` 的阈值；**触发动态调整需「距上次调整 ≥160 秒 且 ≥20 条消息」**，用 LLM 分析最近 20 条判断「过于频繁 / 正常 / 过少」，分别 `×0.8 / ×1.0 / ×1.2`，乘子**钳制在 `[0.1, 1.5]`**。
- bocchy-discord-bot：LLM 给群聊打 **excitement 1–10**，据此设动态冷却：**9–10→20s；7–8→60s；5–6→120s；1–4→300s**（越热闹越能多说，越冷清越少打扰）。
- 说明：这两条来自 DeepWiki 自动生成的文档，**未读原仓库源码**，按二手证据对待。

**(e) 纯文本群聊的时间阈值：没有公开可用值（未验证）。**
语音侧有扎实的量化：Heldner & Edlund 2010（KTH 全文）——说话人之间间隔的**众数偏离 0 约 200 ms**，**14%–19% 的 gap <200ms**，**55%–59%** 属于「不可察觉 gap 或重叠」，引用 Walker & Trimboli (1982)：**人对说话人间沉默的察觉阈值接近 200 ms**；Azure Voice Live API 的 `silence_duration_ms` **默认 500**、`speech_duration_ms` 默认 200ms（server_vad）。但**这些是语音信号，纯文本群聊没有对应研究**（本次尝试的两条文本/CMC 路径都失败）。→ 我们的 60s 阈值只能靠**自家数据标定**，不能引用文献当依据。

---

## 6. 关键约束：QQ 官方通道的真相

### 6.1 现状：群里每条消息都已经 @ 了她

- 插件在开放平台只处理两个事件：`GROUP_AT_MESSAGE_CREATE` 与 `C2C_MESSAGE_CREATE`（`qq_open_plat.py:950`、`:970`），且 `GROUP_AT_MESSAGE_CREATE` 分支里 **`"is_at_bot": True` 是硬编码**（`:1006`）。
- 官方文档【原文】：`QQ 群聊 接收事件 = GROUP_AT_MESSAGE_CREATE / GROUP_MESSAGE_CREATE`（[消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)）。
- 后果：**在我们的群里，「插话抑制」这条逻辑在结构上永远不可能触发**——`_detect_group_interjection_suppression` 第一句就是 `if is_at_bot: return ""`（`message_dispatcher.py:454`）。就算把它从 `neko_dynamic` 解锁，也一样。

### 6.2 出路：开启「接收所有消息」（全量模式）

官方文档【原文·[群消息（全量模式）](https://bot.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)】：

```
当机器人开启了"接收所有消息"功能后，群里的每一条消息（不限于@机器人）都会推送此事件。
各字段含义与 GROUP_AT_MESSAGE_CREATE 完全一致。
事件名 GROUP_MESSAGE_CREATE     Intent GROUP_AND_C2C_EVENT (1<<25)
```

我们订阅的 intent 掩码已经是 `(1 << 25) | (1 << 12)`（`qq_open_plat.py:351`）——**与全量模式同一个 intent**。所以只要后台开启该功能，事件就会到达现有连接；插件侧只需要新增对该事件类型的处理（去掉 `is_at_bot` 硬编码、改用 `mentions` 判定）。**是否需要资质/审核：未验证**（该页未写申请条件）。

### 6.3 全量模式的字段里有现成的 addressee 原料

官方文档字段表【原文】：

| 字段 | 用途 |
|---|---|
| `mentions []User` | 消息中 @ 的用户列表（含 `id`/`username`/`bot`）→ 可判「第一个 @ 的是不是我」 |
| `message_scene.ext` | `msg_idx=…`、`ref_msg_idx=…`（引用场景）、`auth_token` → **引用关系** |
| `msg_elements []MsgElement` | `message_type`：`0=普通文本, 3=结构化卡片, 101=并行消息, 102=聊天记录, 103=引用消息`；每个元素自带 `author` 与 `content` → **谁引用了谁、引用了什么** |
| `author.member_role` | `member=普通成员, admin=管理员, owner=群主` → **可直接用于权限分层** |
| `content` | 已去除 @ 机器人的前缀 |

我们当前只从 `content` 里正则抠 `<@!id>` 来算 `mentions_other_user`（`qq_open_plat.py:986-996`），**完全没用 `mentions` / `msg_elements` / `member_role`**。

### 6.4 其他官方硬约束【原文】

- 被动消息（回复用户）：**群聊有效期 5 分钟、每条消息最多回复 5 次**；单聊 60 分钟 / 4 次。
- 消息去重：相同 `msg_id` 可能多次推送，需结合 `msg_seq`；相同 `msg_id + msg_seq` 重复发送会失败，多次回复要递增 `msg_seq`。
- 主动消息：群聊 `60/qpm`（Bot 维度）、`20/qpm`（单关系维度）、`1000 条/群/天`；频道私信每用户每天 2 条、每机器人每天 200 条。
- 撤回：发送超过 2 分钟不可撤回。

---

## 7. 我们插件的改造方案（分阶段）

> 设计原则：**每一项都要能在当前通道上真正被执行**，避免再造一层「写好了但走不到」的代码（现在的注意力门控就是反面教材，`attention_gate_service.py:200`）。

### 阶段 A（不依赖通道变更，立刻有价值）

A1. **接通「频率闸」**：`reply_decision_node.py` 的 `neko_dynamic` 分支（`:73-83`）目前对 `trusted`/`open` 直接 `action="reply"`，概率闸只存在于 legacy 分支（`:98-106`）。把频率闸提到模式无关的位置，并允许用注意力倍率缩放（reuse `attention["multiplier"]`）。
A2. **突发闸前移**：`reply_burst_limit`（60s/3 条）现在在门控链出口 1 之后；把它移到 `needs_attention` 判断之前，让它对所有通道生效。
A3. **门控决策落文件**：`[Gate]`/`[Attention]`/`[Emotion]`/`[LLM自判]` 走 `_emit_log` 只进内存环形缓冲（`__init__.py:154-161`，maxlen 500），重启即丢。照 `emit_bridge_log` 的双写办法把**被拒原因**写文件，否则「她为什么没回」永远查不到。
A4. **`@` 排序与 @全体**：用官方 `mentions` 数组替代正则抠 `<@!id>`，实现「第一个 @ 的是别人 → 降低优先级」（AstrBot 规则）；`mentions_all` 目前是恒 False 的占位。

### 阶段 B（状态化「该不该接话」，替换布尔启发式）

B1. **多状态替代布尔**：`_looks_like_human_followup` 现在返回 bool，且只吃文本（长度/前缀/问号）。改为返回一个**结构化判定对象**，至少区分：`addressed_to_bot` / `addressed_to_other` / `human_to_human_followup` / `topic_continuation` / `bot_reply_followup(无人接话|热烈讨论)` / `unknown`，每态附证据字段（谁@谁、引用了谁、距上一条非自己发言的秒数、上一条是不是自己发的）。
B2. **状态变量（新）**：
   - `last_bot_reply_at`: 每群上次发言时刻 → 支撑 Heartflow 式「上次回复后群里反应」。
   - `msgs_after_bot_reply`: 上次发言后**别人**的发言条数（0 / 1-2 / ≥3 分档）。
   - `last_human_pair_at` + `pair_key`: 最近「A→B」接话对（谁回谁）→ 替代现在「最近 4 条里找第一条非自己发言」的粗糙规则。
   - `consecutive_bot_replies`: 连续发言计数（现在完全没有「连发上限」概念）。
   - `focus_slot` + `focus_since` + `focus_release_reason`: 焦点槽改为**可被 `@` 抢占、连续 N 次「无需发言」后自动释放**（MaiBot 的两条规则），取代固定 90s 锁。
B3. **判定流程（建议顺序，规则只做短路，不做终判）**：
   1. **显式地址短路**（Sacks 规则 1a：被点名者有义务接话）：@ 我（且首个 @ 是我）／引用我的消息／文本含我的昵称 → 高优先接话；
   2. 引用了**别人**、@ 了**别人** → 人-人接话，**降分**（不是直接判死——文献里这个规则长会话只有 13% 准确率）；
   3. **必要性打分**（替代现在的布尔启发式）：按 Inner Thoughts 的 8 条启发式打 1–5 分——Relevance / Information Gap / Expected Impact / Urgency / Coherence / Originality / Balance / Dynamics——并**要求同时给出正反动机**；`score ≥ im_threshold` 才进入候选。群聊里 Relevance 权重最高（77 次提及），Information Gap 次之（33）；
   4. **结构特征**（HeterMPC/MPC-BERT 证明这是净收益最大的信号）：把「谁回谁」做成计数状态而不是布尔——`reply_to_bot_count_60s`、`human_pair_count_60s`（同一对 A→B 的连续接话轮数）、`last_non_self_speaker`、`speaker_turn_count`；
   5. **会话越长越保守**：参与人数/窗口长度作为置信度衰减因子（文献：Acc 80.31→52.59、p@1 63.5→54.97）；
   6. 通过后仍未到概率闸/注意力倍率 → 走 §阶段 A1 的频率闸。
B4. **节流与自愈（照抄 MaiBot + Bocchy，替代固定突发闸）**：
   - **空闲退避**：连续 N 次「无需回复」（建议起点 2）后，下一次检查前等待 `15s`，随后按连续次数放大，上限 `300s`；**等待期间积压消息达到 6 条立刻重新处理**（`no_action_backoff_bypass_pending_count`）。这比我们的 60s/3 条突发闸更符合「对话频率自适应」。
   - **动态频率乘子**：每 ≥160 秒且 ≥20 条消息，用一次轻量 LLM 调用判「过于频繁 / 正常 / 过少」→ 该群频率 `×0.8 / ×1.0 / ×1.2`，钳制 `[0.1, 1.5]`，最终 `random() < base_talk_value × multiplier`。
   - **热度分档冷却**（可选，先做 A/B）：excitement 9–10→20s；7–8→60s；5–6→120s；1–4→300s。
   - **焦点自愈**：连续 5 次「无需发言」主动释放焦点并禁止回抢（MaiBot `FOCUS_NO_ACTION_EXIT_THRESHOLD`），取代固定 90s 锁。
B5. **把「接话反馈」写回评分**：`msgs_after_bot_reply == 0` → 注意力回落加速（她插了话没人理）；`>=3` → 提升该群焦点分（话题在我们这边）。对应 Heartflow 的两句注入（`（上次回复后群里进行了热烈讨论）` / `（上次回复后无人接话）`）与 MaiBot 的 retro review。
B6. **给模型一条「等一下」的出路**：引入 `wait(seconds)` / `no_action` 式节奏动作（MaiBot Timing Gate），`wait` 语义取「**等待期间不因新消息提前恢复**」，并设 `max_consecutive_wait_count = 3` 上限。我们现在只有「回 / 不回」二值。

### 阶段 C（通道升级后才有意义）

C1. 处理 `GROUP_MESSAGE_CREATE`（全量模式），`is_at_bot` 改为由 `mentions` 计算，`content` 已是去 @ 文本。
C2. 解析 `msg_elements`（`message_type=103` 引用消息自带 `author`）与 `message_scene.ext.ref_msg_idx` → 真正的「谁在引用谁」图，作为 addressee 状态的数据源（现在 `quoted_message_id` 有值但**拿不到被引用者的 uid**）。
C3. 用 `author.member_role`（member/admin/owner）驱动权限分层，替代/补充现在手工维护的 `trusted_groups`。
C4. 引入 `wait` 式节奏工具（MaiBot Timing Gate）：让模型能选「现在不回，等 N 秒再判断」，而不是二值。

---

## 8. 未验证清单（禁止外推）

1. **QQ 开放平台「接收所有消息」的申请条件与资质要求**未在事件文档中写明，未验证能否为当前机器人（`qq_open_app_id=1903565393`）开启。
2. 全量模式事件的实际推送字段是否与文档完全一致（例如 `mentions` 是否包含 bot 自身）——**未实机验证**。
3. 所有被调研项目的**运行时行为**均未实测，结论全部来自静态代码/文档阅读；Yunzai-Bot 原仓库已被 GitHub 停用（403），只读到 TRSS fork 且**未经二次复核**；ZeroBot 结论来自子代理全文 grep，未经本人复核。
4. LangBot `trigger.yaml` 元数据默认值与 `default-pipeline-config.json` **互相矛盾**（`at`: false vs true；`prefix`: [] vs `["ai"]`；`check-sensitive-words`: false vs true；`quote-origin`: false vs true）——哪份最终生效取决于 `coerce_pipeline_config` 的合并逻辑，**未逐行验证**。本方案只引用规则**机制**，不引用其冲突默认值。
5. ChatLuna 的 `defaultGroupRouteMode`（`shared`/`personal`）与 `chatTimeLimit`（200/小时/模型）语义来自代码；官方文档站 `guide/useful-configurations.html` 已过期（至少 6 个键默认值不符），**以代码为准**。
6. 子代理给出的 GitCode 博客（LangBot 主动回复）**不可作为证据**：正文被登录墙挡住且页面自述「由 AIGC 生成」。
7. Heartflow 的默认值来自其 `_conf_schema.json`（子代理独立复核），但**未运行验证**其评分公式在真实群聊中的表现。
8. **纯文本群聊没有公开可用的时间阈值**：语音侧有 200ms/500ms 量级的扎实研究（Heldner & Edlund 2010；Azure Voice Live），但文本侧的两次尝试（UMN 的 IM turn-taking 论文为图片型 PDF、CNKI 微信研究需付费）**都没拿到数字**。我们的 60s 阈值只能靠自家数据标定，**不能引用文献当依据**。
9. **`Preceding` 基线的数值在两篇论文间不一致**（Le 2019 自报 p@1 63.50；MPC-BERT/GIFT 引用版为 55.73），指标口径不同——**跨论文比较数值不安全**，本方案只引用同篇内趋势。
10. 学术结论全部来自子代理读 PDF 原文，其中 Akker & Traum 2009、Duncan 1972、Stivers et al. 2009 的具体数字**未验证**；Inner Thoughts 论文自述的开源地址 `liubruce.me/inner_thoughts` **实测 404**；MPC-BERT 仓库存在性**未验证**（GitHub TLS 在本机被阻断）。
11. MaiBot 的「Dynamic Frequency Control」（160s/20 条、×0.8/×1.2、钳制 [0.1,1.5]）与 Bocchy 的热度冷却映射来自 **DeepWiki 自动生成文档（二手）**，本轮**未读原仓库源码复核**。
12. **修正上一轮的一处错误**：本文档早期草稿（以及我此前的口头结论）把 MaiBot 的仓库写成 `DrSmoothl/MaiBot`，且提到过 `src/plugins/chat/willing_manager.py`。实际情况是：现行仓库为 `Mai-with-u/MaiBot`，`willing_manager.py` 在**当前 main 上不存在**（属于 0.x 架构，只存于 GitLab 旧 commit）；两个 slug 在 jsDelivr 上都能取到但内容新旧不同，**引用行号前必须先确认 slug**。

---

## 9. 出处清单

**MaiBot**（现行 `Mai-with-u/MaiBot@main`，经 jsDelivr 抓取；旧 slug `DrSmoothl/MaiBot` 亦可取但内容较旧）
- `prompts/zh-CN/maisaka_chat_focus.prompt`（Focus 模式、工具集、`query_memory` 使用政策）
- `src/maisaka/focus/manager.py`（`FOCUS_SLOT_LIMIT`、`ISOLATED_SCOPE_PREFIX`、`try_enter_focus`、`release_focus_and_block_next_entry`、`get_focus_cool_time`、`switch_focus`）
- `src/maisaka/focus/runtime_mixin.py`（冷却唤醒、`@` 抢占、`FOCUS_NO_ACTION_EXIT_THRESHOLD`、`FOCUS_SWITCH_NEW_MESSAGE_LIMIT`、focus overview 字段）
- `src/maisaka/reasoning_engine.py`（Timing Gate 常量与流程）、`src/maisaka/chat_loop_service.py`（prompt 渲染、`group_chat_attention_block`、per-chat 注意事项尾注）
- `src/maisaka/builtin_tool/{wait,no_action,continue_tool}.py`（工具语义原文）
- `src/config/official_configs.py`（`ChatReplyTimingConfig`：`reply_trigger_mode`/`talk_value`/`inevitable_at_reply`/`talk_value_rules`/`max_consecutive_wait_count`/`planner_interrupt_max_consecutive_count`/`no_action_backoff_*`；`ExperimentalConfig.focus_mode/focus_on_private/focus_groups/focus_cool_time`；`emotion_trait`/`attention_drift`）
- **0.x「心流」架构**（GitLab 旧 commit，`@main` 已 404，仅作设计参考）：`src/plugins/willing/mode_{classical,dynamic,llmcheck,custom,mxp}.py`、`src/heart_flow/sub_heartflow.py`、`src/plugins/person_info/relationship_manager.py`、`src/heart_flow/mai_state_manager.py`
- ⚠️ `gitlab.mikumikumi.xyz/maibot/maibot/-/blob/…/src/plugins/chat/willing_manager.py`：**当前 main 上不存在，勿引用**
- 二手：[Dynamic Frequency Control with talk_value](https://deepwiki.com/DrSmoothl/MaiBot/8.2-dynamic-frequency-control-with-talk_value)

**AstrBot**（`AstrBotDevs/AstrBot@master`）
- `astrbot/core/pipeline/stage_order.py`、`scheduler.py`、`waking_check/stage.py`、`rate_limit_check/stage.py`、`whitelist_check/stage.py`、`session_status_check/stage.py`、`process_stage/follow_up.py`
- `astrbot/builtin_stars/astrbot/{main.py,group_chat_context.py}`
- 官方文档：<https://docs.astrbot.app/dev/astrbot-config.html>、<https://docs.astrbot.app/use/proactive-agent.html>

**LangBot**（`langbot-app/LangBot@master`）
- `src/langbot/pkg/pipeline/resprule/{resprule.py,rule.py,rules/{atbot,prefix,random,regexp}.py}`
- `src/langbot/pkg/pipeline/ratelimit/{ratelimit.py,algos/fixedwin.py}`、`bansess/bansess.py`、`cntfilter/filters/cntignore.py`、`aggregator.py`
- `src/langbot/templates/metadata/pipeline/trigger.yaml`、`templates/default-pipeline-config.json`

**ChatLuna**（`ChatLunaLab/chatluna@2f69f09`）
- `packages/core/src/config.ts`、`middlewares/chat/{allow_reply,cooldown_time,message_delay,chat_time_limit_check}.ts`
- 文档：<https://chatluna.chat/guide/useful-configurations.html>（已过期，仅作语义参考）

**nonebot-plugin-llmchat**（`git.fuquan.moe/fuquan/nonebot-plugin-llmchat@0.6.0`）
- `nonebot_plugin_llmchat/{__init__.py,config.py,state.py,persistence.py,output_protocol.py,prompts.py}`
- 指定 commit：`6db55055a2`（per-group `random_trigger_prob`）、`4af60b8145`（修复未生效）

**KiraAI**（`xxynet/KiraAI@main` v2.34.7）
- `core/adapter/src/qq/qq.py`、`core/plugin/builtin_plugins/chat/{main.py,schema.json}`、`core/config/default.py`、`message_manager.py`

**Heartflow**（`advent259141/Astrbot_plugin_Heartflow@v2.3.0`）
- `main.py`、`_conf_schema.json`

**学术（子代理读 PDF/ar5iv 原文）**
- Addressee / 回复选择：[Le et al. 2016, D16-1231](https://aclanthology.org/D16-1231/) · [Le et al. 2019, D19-1199](https://aclanthology.org/D19-1199/) · [MPC-BERT, ACL 2021](https://aclanthology.org/2021.acl-long.285/) · [GIFT, ACL 2023](https://aclanthology.org/2023.acl-long.651/) · [Ravuri & Stolcke 2014](https://www.isca-archive.org/interspeech_2014/ravuri14_interspeech.pdf) · [Akker & Traum 2009（内容未验证）](https://www.isca-archive.org/diaholmia_2009/akker09_diaholmia.pdf)
- 多方可对话建模：[Topic-BERT, EMNLP 2020](https://aclanthology.org/2020.emnlp-main.533/) · [HeterMPC, ACL 2022](https://aclanthology.org/2022.acl-long.349/)
- Turn-taking：[Sacks, Schegloff & Jefferson 1974](https://webspace.science.uu.nl/~vrees101/code/turntaking/Turntaking_SSJ.pdf) · [Heldner & Edlund 2010（KTH 全文）](https://kth.diva-portal.org/smash/get/diva2:388247/FULLTEXT01) · [Azure Voice Live API 默认值](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to) · [Stivers et al. 2009（题录，数字未验证）](https://www.pnas.org/doi/abs/10.1073/pnas.0903616106)
- LLM 时代：[MUCA, arXiv:2401.04883](https://arxiv.org/abs/2401.04883) · [Inner Thoughts, CHI 2025, arXiv:2501.00383](https://arxiv.org/abs/2501.00383) / [ACM DOI](https://dl.acm.org/doi/full/10.1145/3706598.3713760) · [clawgarden `should_i_respond`（非论文）](https://docs.rs/clawgarden-agent/0.9.3/clawgarden_agent/pi_rpc/fn.should_i_respond.html)
- 工业案例：[XiaoIce 正确出处 arXiv:1812.08989](https://arxiv.org/abs/1812.08989)（**注：本任务提示里给的 1810.09572 不是 XiaoIce，实为 [21cm 宇宙学论文](https://arxiv.org/abs/1810.09572)，是提问方的错误**；且 XiaoIce 论文全文无 group chat / interrupt 内容） · [小冰上线三天后被微信移除（2014-06-03）](http://www.ecns.cn/business/2014/06-03/117095.shtml) · [bocchy-discord-bot 情境介入（二手）](https://deepwiki.com/botarhythm/bocchy-discord-bot/3.2-contextual-intervention-system)

**QQ 官方**
- [消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)（收发场景、被动/主动消息与配额、去重、撤回）
- [群消息（全量模式）](https://bot.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)（`GROUP_MESSAGE_CREATE`、`mentions`、`msg_elements`、`member_role`）

**本插件（对照基线）**
- `attention_gate_service.py:200-298`、`message_dispatcher.py:426-477,963-966,1026-1051`、`reply_decision_node.py:49-107`、`attention_service.py:442-489,995-1011`、`_vendor/connection_onebot/qq_open_plat.py:351,950-1014`
