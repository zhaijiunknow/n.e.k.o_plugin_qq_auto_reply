# connection_onebot — 副本出处

宿主第一方连接器包 `utils.connection.onebot` 的副本，用作"宿主还没带这个包"时的回退。
解析入口：`connector_seam.py`。

## 出处

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/zhaijiunknow/N.E.K.O.git`（`origin`） |
| 分支 / commit | `QQ` @ `4e83ac50e89dc9f4a6ea78af8bcde3a6280671d2`（2026-09-12） |
| 上游 PR | `Project-N-E-K-O/N.E.K.O#2996`（`feat(qq): 适配器从插件中拆除并合入本体`），**未合并** |

所取的 5 个文件，均来自 `utils/connection/onebot/`：

```
__init__.py  factory.py  onebot_client.py  onebot_connection.py  qq_open_plat.py
```

（同目录下的 `utils/connection/__init__.py` 只有 docstring、零 re-export，无需复制。）

## 相对上游的本地改动

副本**不是逐字一致**的：为了让 CI 的 ruff 步骤过，跑了一遍自动修复。
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

1. 删除整个 `_vendor/`；
2. 把 `connector_seam.py` 收敛成一行 re-export（或直接改回
   `from utils.connection.onebot import …`）；
3. 把 `tests/test_qq_connector_seam.py` 里"回退可用"那条与漂移守卫一起清掉。

在这之前，`tests/test_qq_connector_seam.py` 的漂移守卫会在宿主存在时比对
`OneBotConnector` 协议的成员集合，副本与宿主对不上就红 —— 那是拆分时的安全网。
