"""fail-to-pass 证据：逐个把浮动刷新的必要条件拆掉，确认 `test_qq_status_refresh_reachable.py` 会红。

拆的四种情况，每一种都对应一个"看似能用其实不能用"的假修复：

1. 按钮整个删掉         → 问题原样存在（刷新又只在顶部）
2. `fixed` 换成 `absolute` → 它跟着内容滚走，滚到底照样够不着
3. 去掉 scroll 监听       → 按钮永不出现，等于没加
4. 去掉 `.wrap` 的底部留白 → 它压住最后一张卡片的操作区

铁律（沿用 verify_page_structure_fail_to_pass.py）：替换前确认替换真的发生；
恢复放 `finally` 并逐字节核对；每个用例起新进程（`pytest.main` 同进程不会重新导入
模块，改文件对已 import 的模块无效）。

手动运行（不参与 pytest 收集）：
    python plugin/plugins/qq_auto_reply/tests/verify_status_refresh_fail_to_pass.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]  # N.E.K.O 仓库根
PLUGIN = Path(__file__).resolve().parents[1]
PAGE = PLUGIN / "static" / "status.html"
TEST_FILE = Path(__file__).resolve().parent / "test_qq_status_refresh_reachable.py"

#: (说明, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str]] = [
    (
        "按钮整个删掉（刷新又只剩顶部那个）",
        '<button id="btn-refresh-fab">',
        "<!-- 删掉了 --><button id=\"refresh-fab-removed\">",
    ),
    (
        "fixed 换成 absolute（按钮跟着内容滚走）",
        "position:fixed; right:20px; bottom:20px; z-index:40;",
        "position:absolute; right:20px; bottom:20px; z-index:40;",
    ),
    (
        "去掉 scroll 监听（按钮永不出现）",
        "window.addEventListener('scroll', syncFab, { passive: true });",
        "/* 监听被去掉 */",
    ),
    (
        "去掉 .wrap 的底部留白（压住最后一张卡片）",
        ".wrap { max-width:760px; margin:0 auto; padding-bottom:76px; }",
        ".wrap { max-width:760px; margin:0 auto; }",
    ),
]


def _run_pytest() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(TEST_FILE), "-q", "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode


def main() -> int:
    source = PAGE.read_text(encoding="utf-8")
    results: list[tuple[str, bool]] = []

    for label, old, new in MUTATIONS:
        if source.count(old) != 1:
            print(f"[MISS] 锚点出现 {source.count(old)} 次（期望 1 次），本项结论无效: {old[:50]!r}")
            results.append((label, False))
            continue
        mutated = source.replace(old, new, 1)
        if mutated == source:
            print(f"[MISS] 替换没有实际发生: {old[:50]!r}")
            results.append((label, False))
            continue

        try:
            PAGE.write_text(mutated, encoding="utf-8")
            code = _run_pytest()
        finally:
            PAGE.write_text(source, encoding="utf-8")

        restored = PAGE.read_text(encoding="utf-8") == source
        ok = code == 1 and restored
        results.append((label, ok))
        print(f"[{'OK  ' if ok else 'MISS'}] {label}: exit={code}（期望 1）；源码已恢复={restored}")

    # 对照：修复在位时必须绿
    code_clean = _run_pytest()
    control_ok = code_clean == 0
    results.append(("对照（修复在位必须绿）", control_ok))
    print(f"[{'OK  ' if control_ok else 'MISS'}] 对照: exit={code_clean}（期望 0）")

    missed = [name for name, good in results if not good]
    print()
    if missed:
        print(f"[FAIL] {len(missed)} 项不符合预期: {missed}")
        return 1
    print(f"[PASS] {len(results)}/{len(results)} —— 浮动刷新的四个必要条件都真的被钉住了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
