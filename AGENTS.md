# AGENTS.md

## 两条发布分支（2026-11-15 起）

| 分支 | 版本 | 界面 | tag |
|---|---|---|---|
| `main` | 稳定版 | 只有 QQ 开放平台；OneBot（NapCat / SnowLuma）界面不出现，后端能力不动 | `vX.Y.Z` |
| `beta-onebot-ui` | 测试版 | 保留 OneBot 界面，仅支持 SnowLuma 部署与启动 | `vX.Y.Z-beta.N`，release 标成 Pre-release |

起因：NapCat / SnowLuma 的 release 不能稳定拉取，一键部署对新用户不可靠；已经下载好的用户
还能继续用，所以 OneBot 界面留在测试版。

- **2026-11-22 起测试版移除 NapCat 界面、部署和启动支持**：保留 SnowLuma 与通用 OneBot，合入 main 时不得恢复 NapCat。兼容字段及共用接口名可保留，旧配置迁移会关闭 OneBot 开机自启。
- **`beta-onebot-ui` 最初为 `main` + 一个 revert**：它从 main 分出来，第一个提交把 main 上
  「稳定版去除 OneBot 界面」那个提交整个还原。所以同步方向只有一个 ——
  `git checkout beta-onebot-ui && git merge main`，不要反过来合。
- 合并时必然冲突的只有 `plugin.toml` 的版本号和 `README.md` 顶部的版本段（两边各写各的），
  保留测试版那一侧再按需改号。
- main 上稳定版专有的改动都带 `[stable-no-onebot-ui]` 标记，集中在 `static/index.html`、
  `static/status.html`、`static/open_platform.html`、`static/onebot-legacy.js`、两个语言包和
  `tests/test_qq_stable_no_onebot_ui.py`。以后在 main 上再动这些地方，合进测试版时保留测试版
  那一侧（测试版没有 `onebot-legacy.js` 和那个测试文件）。
- 通用改动（后端、提示词、开放平台页的其它部分）一律先进 main，再合到测试版。
- 发版：在对应分支上改 `plugin.toml` 版本 + README，推分支，再推同名 tag
  （CI 要求 tag 与 `plugin.toml` 版本一致）。
- 后续计划：开放平台并入本体之后，OneBot 这条线转正（把 `beta-onebot-ui` 合回 main，
  并移除插件里的开放平台部分）。

## 跑测试（独立检出，宿主在同级目录 `../N.E.K.O`）

```bash
../N.E.K.O/.venv/Scripts/python -m pytest tests -q -o asyncio_mode=auto
```

- `-o asyncio_mode=auto` 必须带：这个开关写在宿主的 `pytest.ini` 里，从插件目录跑时读不到，
  漏了会有几百条 async 用例报 `async def functions are not natively supported`。
- 换宿主版本：设 `NEKO_APP_ROOT=<宿主根目录>`（`tests/conftest.py` 读它）。要验证"已发布宿主"
  上的退级路径（`connector_seam` 回退到 `_vendor/`），用
  `git -C ../N.E.K.O archive <tag> | tar -x -C <目录>` 解一份出来再指过去。
- `tests/test_qq_noqa_hygiene.py` 会扫到本地的 `.dsh-artifacts/`（已 gitignore）；那里放着宿主
  文件副本时这条会红，与改动无关。

## Lint（与 CI 同款）

```bash
ruff check --ignore-noqa --isolated --no-respect-gitignore --target-version py311 \
  --line-length 120 --select E4,E7,E9,F,I --exclude vendor --exclude .dsh-artifacts .
```

CI 钉的是 `ruff==0.12.4`，且 `--isolated` 会让 `ruff.toml` 失效 —— 所以 `_vendor/` 里的副本也必须
自己干净（从宿主拷文件进来之后要跑一遍，见 `_vendor/connection_onebot/PROVENANCE.md`）。
