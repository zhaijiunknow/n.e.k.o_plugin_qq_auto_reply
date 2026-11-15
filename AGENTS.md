# AGENTS.md

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
