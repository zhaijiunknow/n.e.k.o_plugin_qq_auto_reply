# connection_onebot — 副本出处

宿主第一方连接器包 `utils.connection.onebot` 的副本，用作"宿主还没带这个包"时的回退。
解析入口：`connector_seam.py`。

## 出处

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/zhaijiunknow/N.E.K.O.git`（`origin`） |
| 分支 / commit | `QQ` @ `4e83ac50e89dc9f4a6ea78af8bcde3a6280671d2`（2026-09-12） |
| 上游 PR | `Project-N-E-K-O/N.E.K.O#2996`（`feat(qq): 适配器从插件中拆除并合入本体`），**未合并** |

副本一共 6 个文件，其中 5 个来自 `utils/connection/onebot/`，1 个是插件自撰：

```
__init__.py  factory.py  onebot_client.py  onebot_connection.py  qq_open_plat.py   ← 上游 5 个
qq_open_platform_media.py                                                          ← 插件自撰，上游**没有**这个文件
```

（上游同目录下的 `utils/connection/__init__.py` 只有 docstring、零 re-export，无需复制。）

⚠️ **行数相同不代表内容相同**：副本与上游的 `qq_open_plat.py` 恰好都是 1126 行（功能性改动
加的行与减的行正好抵消），逐字核对必须用 `git diff --no-index`，看行数会得出错误结论。

## 标记约定：`LOCAL-PATCH`

副本里任何**非 lint** 的差异，都必须在文件头留一行 `# LOCAL-PATCH: <commit> <摘要>`：

```bash
grep -rn "LOCAL-PATCH" _vendor/connection_onebot/
```

`tests/test_qq_connector_seam.py::test_vendored_local_patches_are_marked_and_kept` 会核对：
标记还在、下面的接线还在、文件名清单与本文件对得上。

## 相对上游的本地改动（一）：功能性改动

**这一节是 2026-09-27 补的。** 2026-09-26 的
`4302a9ea6280954929b644fe9404adebc69f10a2`（「开放平台三处缺口：私聊发图 / 不再白烧 TTS /
入站文件附件」）改了副本里的 `qq_open_plat.py`（5 个 hunk）并新增了
`qq_open_platform_media.py`，**却没有动本文件** —— 于是"副本不是逐字一致"这件事在文档里
只剩 lint 那一半，功能性差异一点痕迹都没有。补这一节 + 文件头标记就是补这个缺口。

| 位置（副本） | 上游 | 副本里改成了什么 | 真机是否生效 |
|---|---|---|---|
| `qq_open_plat.py` import 块 | 无 | `from . import qq_open_platform_media` | 仅回退部署（见下） |
| `QQOpenPlatformConnection.send_private_message` | 只把图当文字 `[图片]` 发 | 单聊真发图：`qq_open_platform_media.send_private_image(...)`（`msg_type=7`），上传失败才退回文字 | 仅回退部署 |
| `QQOpenPlatformConnection.send_private_image`（新方法） | 不存在 | 薄转发到 `qq_open_platform_media.send_private_image` | 仅回退部署（插件**刻意不用**它，见下） |
| `QQOpenPlatformConnection._upload_group_image` | 内联的旧式直传（56 行） | 薄转发到 `qq_open_platform_media.upload_image(scope="groups", …)`：旧式直传仍是首选，文档里的 URL / 分片作回退 | 仅回退部署 |
| `QQOpenPlatformConnection._extract_attachments` | 只产出 `{"type", "url"}` | 平台给了文件名就带 `"name"`（消费方回退到 URL 尾巴） | 仅回退部署 → **真机上拿不到文件名**，见下 |
| `qq_open_platform_media.py`（整个文件） | **不存在** | 上传流程的**自由函数**版（第一个参数是连接对象），好让**宿主那份**连接器也能用 | ✅ **真机生效**（群图 + 单聊图都走它） |

### 真机生效面（2026-09-27 核实）

运行时连的是**宿主那份**连接器（插件日志每轮都打
`[QQ] 连接器来源: host (utils.connection.onebot)`），所以上表里**只有最后一行的
`qq_open_platform_media` 在真机路径上**；`qq_open_plat.py` 那 5 个 hunk 只有在"宿主没带
连接器"的回退部署里才会跑到。插件侧对应的调用点是
`reply_delivery_node._send_sticker`：开放平台的群图与单聊图**一律直调** media 里的自由函数
（私聊刻意不走连接器上那个 `send_private_image()` —— 它固定 `record_sent=True`，而表情包
一直是 `record_sent=False`）。

**真机证据（2026-09-27 03:19，开放平台正式环境）**::

    03:19:39  WARNING - [QQOpenPlatform] 图片直传上传未拿到 file_info
    03:19:41  INFO    - [QQOpenPlatform] 图片上传成功(分片): wIFo43EanZwsn01Ru9mCJ4t6

两行都出自 `qq_open_platform_media`（`[QQOpenPlatform]` 前缀是它打的），结论是：
**旧式直传在真机上已经失效，文档里的分片上传是活的** —— 这正是当初"两条都试"要回答的问题
（2026-09-26 那次实测同结论，见 `reply_delivery_node._send_sticker` 的 docstring；
上面是 09-27 的又一次独立复现）。所以这个自撰模块是**在役代码**，不是历史遗留。

**已知缺口**：`_extract_attachments` 那个 `"name"` 只在回退部署里生效，用宿主连接器时
`enrichment._attachment_files` 只能拿 URL 尾巴当标签（只影响 prompt 里那行标签，不影响
内容）。原始 `att` 字段在连接器归一化后就没了，插件侧接不住 —— 要真修得推宿主 PR。

**丢了会怎样**（这正是要标记的原因 —— 重新同步上游会**静默**回退，不报错）：

1. `qq_open_platform_media.py` 被删或被改名：**真机立刻坏**。文件层面是
   `connector_seam` 一解析就抛 `ModuleNotFoundError`（启动自动回复直接失败），
   函数层面是表情包投递 `AttributeError` —— 群图与单聊图都直调它；
2. `qq_open_plat.py` 那 5 个 hunk 被上游覆盖回去：**真机不变**（连的是宿主那份），
   但回退部署丢能力 —— 单聊发图退回只发 `[图片]`、群图退回只试（真机已失效的）旧式直传、
   附件文件名丢失；
3. 最糟的一种：只拷 5 个上游文件、顺手 `rm` 掉 `qq_open_platform_media.py` ——
   同第 1 条，而且这次是"以为自己在做正确的事"。

### 重新同步上游的顺序

1. `git log --oneline -- _vendor/connection_onebot/` 加上面那条 grep，先看清有哪些本地改动；
2. 拷上游的 `__init__ / factory / onebot_client / onebot_connection / qq_open_plat`，
   **保留** `qq_open_platform_media.py`（上游没有它）；
3. 按上表把功能性改动重新应用到新版本上（冲突以上游为准，语义保持）；
4. 重跑下面那段 lint 修复；
5. 跑 `pytest plugin/plugins/qq_auto_reply/tests/test_qq_connector_seam.py`（两组守卫）。

## 相对上游的本地改动（二）：lint

（lint 之外还有上面那一节的功能性改动。）副本**不是逐字一致**的：为了让 CI 的 ruff 步骤过，
跑了一遍自动修复。
CI 的调用是 `ruff check --ignore-noqa --isolated --target-version py311 --line-length 120
--select E4,E7,E9,F,I --exclude vendor plugin-repo` —— 注意 `--isolated` 让插件自己的
`ruff.toml` 失效、`--ignore-noqa` 让 `# noqa` 失效，所以副本必须自己干净。

复现方式：

```bash
ruff check --fix --unsafe-fixes --ignore-noqa --isolated --target-version py311 \
  --line-length 120 --select E4,E7,E9,F,I _vendor/connection_onebot/
```

自动修复（6 处）：

- `qq_open_plat.py`：import 块排序（`import re as _re` 归位）；`f"[QQOpenPlatform] token 已获取"`
  去掉无占位符的 `f` 前缀；`import os, mimetypes` 拆成两行。
- `onebot_client.py`：删掉 `_handle_group_ban_notice` 与 `_cleanup_sent_message_cache`
  里冗余的函数内 `import time`（模块级 `import time` 已存在）。

手工修复（5 处，该 ruff 版本对 E701/E702 不提供自动修复）：`qq_open_plat.py` 里两处
`try: await self._ws.close()` / `except Exception: pass` 拆行，以及一处
`self._ws = None; self.ws = None` 拆成两行。

**建议把上面这些同样修到 PR #2996 的分支上** —— 它们本来就是真的 lint 问题，
宿主侧 CI 迟早也会红；修完之后本副本可以恢复逐字复制。

## 运行时依赖

`websockets`（两个 client）与 `httpx`（`qq_open_plat.py`）。包内只有 stdlib + 这两个第三方，
全是相对 import，**不引用宿主任何东西** —— 这正是它可以被复制出来的原因。
插件的 `pyproject.toml` 里 `dependencies = []`，这两个包靠宿主运行环境提供（与副本存在与否无关）。

## 什么时候删掉这个目录

PR #2996 合并、且插件声明的最低支持宿主版本都带 `utils.connection.onebot` 之后：

0. **先把副本里的功能性改动搬进宿主**（`qq_open_platform_media.py` 整套 + `qq_open_plat.py`
   那三处接线）。删副本等于把这些能力一起删掉：单聊发图、URL/分片上传、附件文件名。
   当初把它们写成"对任何一份连接对象都能跑的自由函数"，为的就是"插件改不了宿主文件"；
   合并之后应当正着修在宿主里。
1. 删除整个 `_vendor/`；
2. 把 `connector_seam.py` 收敛成一行 re-export（或直接改回
   `from utils.connection.onebot import …`）；
3. 把 `tests/test_qq_connector_seam.py` 里"回退可用"那条与漂移守卫一起清掉。

在这之前，`tests/test_qq_connector_seam.py` 的漂移守卫会在宿主存在时比对
`OneBotConnector` 协议的成员集合，副本与宿主对不上就红 —— 那是拆分时的安全网；
`test_vendored_local_patches_are_marked_and_kept` 则盯住本文件与副本里那些本地改动。
拆 `_vendor/` 时这两条一起删。
