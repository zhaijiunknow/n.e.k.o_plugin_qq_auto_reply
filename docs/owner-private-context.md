# 主人私聊 → 群：最近对话与长期记忆

群聊回复默认只用群域记忆。主人本人在群里说话时，插件可以额外把**他自己的私聊**
带进这一轮的提示词——解决"私聊刚说过的事，到了群里她就不记得"。这是单向只读
披露：群聊数据从不写回私聊，私聊数据也不会因为读了就被复制。

## 两个独立开关

| 键 | 默认 | 带什么 | 用来回答 |
|---|---|---|---|
| `cross_domain_owner_memory` | `False` | 私聊会话里**最近 N 条**对话（双向，含她自己的回复） | "刚才说到哪" |
| `cross_domain_owner_longterm_memory` | `False` | 私聊主线的**长期记忆**（`fetch_bootstrap_memory` 那份语料） | "她记不记得我" |

配套参数：`cross_domain_owner_context_rounds`（默认 8，2–40，双向合计）、
`cross_domain_owner_longterm_max_chars`（默认 1200，200–4000，按行截断）。

两个开关各撤各的：只关掉长期记忆不会连最近聊到的几句一起丢。两段都只在
"发言人就是插件权限名册里的 `admin` 本人"时出现，与 QQ 群主/群管理员头衔无关。

## 授权：每轮一枚令牌

`owner_private_context.capture_owner_private_token` 在**任何 await 之前**按接收时刻的
发言人权限铸造一枚只含真实发送者 ID 的令牌。之后每次使用（构建提示词、生成前、
投递前）都重新调用 `owner_private_token_live`：

1. 两个开关至少一个仍打开；
2. 该发送者在名册里**现在**仍是 `admin`（降级立即失效，不靠接收时刻的旧章）；
3. 令牌形态合法。

合成轮（主动发言、缓冲合并、回溯）没有真实发言人，一律不铸造令牌，也不会因为
占位发送者是管理员账号就把私聊内容带进群。

## 三段撤销

从"装配提示词"到"真发出去"之间隔着登录态、bootstrap、召回等网络等待，用户可能
正好在这一刻关掉开关。三处都查：

```
session_instruction_service 生成（段原文进 QQInstructionBundle）
  → reply_context_node 在 await 之后按令牌/开关摘段（段原文存进 QQReplyContext）
  → reply_generation_service._sanitize_for_live_consent 生成前再摘一次，
    并把两个开关记进 consent 依赖快照（缓冲路径投递前据此复核）
```

摘除按"整段 + 分隔符"匹配，不会误伤正文里恰好同名的文字。

## 与既有私聊记忆路径的关系

群记忆 + 群成员记忆都打开时，`group_private_memory` 已经会把主人私聊的
**长期记忆**拼进核心记忆段。这种情况下长期段跳过（`already_in_core`），
同一段文字不会在 prompt 里出现两遍。这是"两条路汇到同一份语料"，不是两套数据。

## 提示词里的约束

两段各带一段硬性约束：只能用来理解主人此刻指的是什么，不得在群里复述原文、
引用细节或主动展开私事；被问起时用"你之前跟我说过"这种含糊说法接住。
文案走 `prompts.owner_private_context` / `prompts.owner_longterm_context`，
可被 i18n 包与提示词覆盖层替换。

## 图库存储目录（同批改动）

`gallery_dir` 把**图库与表情包共用的图片库存**（`sticker.json` + `sticker/`）搬到
用户指定的目录：

- 只接受**绝对路径**，相对路径直接拒绝（相对路径随宿主工作目录漂移，图会散掉）；
- 空值 = 保持历史布局（插件数据目录），行为与旧版完全一致；
- 改目录**不会**自动搬旧文件：原目录的图留在原处，确认新目录可用后手动拷贝；
- 保存失败会把运行时的值一起回滚，不让进程里跑着一个磁盘上不存在的配置。

## 验证

```bash
pytest tests/test_qq_owner_private_context.py
ruff check --ignore-noqa --isolated --no-respect-gitignore --target-version py311 \
  --line-length 120 --select E4,E7,E9,F,I --exclude vendor .
```

自动测试覆盖：令牌只在接收时 admin + 开关打开时铸造；开关关闭/主人降级后令牌
失效；最近对话读取（多模态压平、草稿排除、条数与单条长度钳制）；长期记忆按行
截断与缓存（失败沿用旧值、`already_in_core` 跳过）；段摘除；图库目录默认布局、
跟随配置、相对路径拒绝、归一与清空。
