"""fail-to-pass 证据：还原"空块也算回复"的旧行为，确认 `test_qq_empty_reply_not_a_reply.py` 会红。

注入方式：把 `block_has_content` 换成"永远返回 True" —— 这恰好等价于修复前
`finalize` 里**没有**那段全空块归零，因为归零的条件是
`blocks and not any(block_has_content(b) for b in blocks)`。不改源文件。

同时跑一个"正常注入"对照：不注入时必须全绿，否则说明 red 来自别的原因。

手动运行（不参与 pytest 收集）：
    python plugin/plugins/qq_auto_reply/tests/verify_empty_reply_fail_to_pass.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # N.E.K.O 仓库根

import pytest  # noqa: E402
from plugin.plugins.qq_auto_reply.reply_postprocess_node import (  # noqa: E402
    QQReplyPostprocessNode,
)

TEST_FILE = str(
    Path(__file__).resolve().parent / "test_qq_empty_reply_not_a_reply.py"
)
_ORIGINAL = QQReplyPostprocessNode.block_has_content


def _run() -> int:
    # 不能加 `-p no:randomly`：pytest.ini 的 addopts 带着固定 seed，禁用插件会
    # 让那个参数未识别 → exit=4（用法错误），而 exit != 0 会被误读成"抓住了 bug"。
    try:
        return int(pytest.main([TEST_FILE, "-q", "--no-header"]))
    except SystemExit as exc:
        return int(exc.code or 0)


def _restore() -> None:
    QQReplyPostprocessNode.block_has_content = _ORIGINAL
    # 上面那行把 staticmethod 拆成了普通函数，按原样装回去
    QQReplyPostprocessNode.block_has_content = staticmethod(_ORIGINAL)


def main() -> int:
    results: list[tuple[str, bool, int]] = []

    # 1) 旧行为：任何块都算有内容 ⇒ 空 <msg> 又是"回复"了
    QQReplyPostprocessNode.block_has_content = staticmethod(lambda block: True)
    code_old = _run()
    results.append(("旧行为（空块也算回复）", code_old != 0 and code_old != 4, code_old))
    print(f"[{'OK  ' if results[-1][1] else 'MISS'}] 注入旧行为: exit={code_old}（期望 1）")

    _restore()

    # 2) 对照：修复在位 ⇒ 必须绿
    code_new = _run()
    ok = code_new == 0
    results.append(("对照（修复在位）", ok, code_new))
    print(f"[{'OK  ' if ok else 'MISS'}] 对照: exit={code_new}（期望 0）")

    missed = [name for name, good, _ in results if not good]
    print()
    if missed:
        print(f"[FAIL] {len(missed)} 项不符合预期: {missed}")
        return 1
    print("[PASS] 2/2 —— 旧行为必红、修复在位必绿，测试确实钉住了这个缺陷")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
