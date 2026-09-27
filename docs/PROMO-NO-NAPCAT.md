# `promo-no-napcat-ui` 分支说明（宣传录制专用）

**这条分支的唯一目的**：录宣传视频时，前端**不出现 NapCat 页入口、也不出现「一键部署」**。

使用者口径（2026-09-27）：

> 「现在需要录制视频做宣传，但是 napcat 可能有风险，所以需要出一个专门去掉 napcat 前端的
>   版本，开一个分支，在前端隐藏 napcat 页的入口和一键部署即可」

「即可」是关键词：**只做前端隐藏**，别的都不动。

## 改了什么（4 处前端 + 1 处看门狗 + 1 个分支专用测试）

| 文件 | 改动 |
| --- | --- |
| `static/index.html` | 删掉两张入口卡：`NapCat`（→ napcat.html）与「一键部署」（→ status.html）。首页只剩 `QQ 开放平台` 与 `旧版控制中心` |
| `static/open_platform.html` | 顶部「切换到 NapCat」的跳转去掉 |
| `static/status.html` | 「一键部署」卡片（含 `btn-deploy` / `btn-apply` / 部署日志）加 `style="display:none"` |
| `tests/test_qq_console_page_navigation.py` | `EXPECTED_NAV` 跟着改（首页不再链 napcat/status，开放平台页不再链 napcat）—— 跳转**机制**不变，只是图变了 |
| `tests/test_qq_promo_no_napcat.py` | **本分支专用**看门狗：把"隐藏"钉成断言，合并 main 后卡片一旦回来立刻红 |

改动处都留了 `[promo-no-napcat-ui]` 标记注释，方便合并时定位。

## 刻意**没有**动的东西（三条边界）

1. **页面本身照发**：`static/napcat.html` 仍在包里，直接输 URL 照样打开。
   本分支隐藏的是「入口」，不是页面 —— 要连 NapCat 时（或以后想恢复）不必回 main。
2. **DOM 只隐藏不删除**：`status.html` 的部署卡片必须留在 DOM 里。
   页内 JS 会**无条件** `document.getElementById('btn-deploy'/'btn-apply')` 绑事件，
   把元素删掉会让整页开页即抛、一片空白 —— 比"看得见"糟得多。
   （`tests/test_qq_promo_no_napcat.py` 有两条用例分别守住"隐藏"与"仍在 DOM"。）
3. **后端能力全留**：`deploy` 入口、NapCat 安装/配置/自启逻辑一行没改。
   录视频用的是已经配好的环境，前端不需要按钮。

## 它**不是**一份安全隔离

- 隐藏入口 ≠ 禁用功能：谁直接访问 `…/static/napcat.html` 仍然能打开那页；
- 插件后端的 `deploy` 入口也照旧可调（`plugin/plugins/qq_auto_reply` 的能力没动）。

要的是"视频里不出现"，这是**录制用构建**，不是"去掉 NapCat 能力"的发行版。

## 怎么跟 main 保持同步

main 会一直长，这条分支靠 `merge` 跟：

```
git checkout promo-no-napcat-ui
git merge main
python -m pytest plugin/plugins/qq_auto_reply/tests -q     # 或在仓库根跑
```

**合并后必看**：`tests/test_qq_promo_no_napcat.py` 是否仍然全绿。
它专门拦"main 把卡片带回来"这件事 —— 那张卡回来时不会报错、没人会发现，
直到视频里出现 NapCat 为止。冲突基本只会落在那 4 个前端文件和那张 `EXPECTED_NAV` 上
（改动处都有 `[promo-no-napcat-ui]` 标记）。

## 如果要出录制用的包

推 `v*` tag 会触发 CI 打包上传（与本仓其它版本同一套流程，
`plugin.toml` 的版本必须等于 tag 名）。**注意**：这样发出去的 release 会挂在同一个
插件市场仓库下、并且和正式版本共用版本号空间 —— 要么把它标成 prerelease，
要么先跟使用者确认要不要发。

## 合回 main 的时候

把 `tests/test_qq_promo_no_napcat.py` **删掉**（它断言的是"卡片必须不存在"，
与 main 的行为相反），并把 4 处前端改动、那张 `EXPECTED_NAV` 一起还原。
换句话说：**这条分支永远不该合回 main**，除非使用者改主意要让隐藏成为正式行为
（那时应当把隐藏做成配置项，而不是靠一条长期分叉的分支）。
