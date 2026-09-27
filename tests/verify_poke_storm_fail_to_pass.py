"""fail-to-pass 证据：戳一戳"只跟戳、不回话"的六处接线都是必要条件。

被验证的东西（使用者 2026-09-27：「戳戳风暴就不需要回复了，只需要跟戳」）：

- 风暴**要回戳**（旧行为是"人多就不回戳"）；
- 风暴**不许进管线**（旧行为是"人多就注入 LLM 让她说话"）；
- "戳她"的判定**要真的按 target 判**（写死 True 会把"戳别人"也吞掉）；
- 跟戳**要有上限**（否则同一人触发无限互戳）；
- 风暴**要留痕**（不然日志里看不出发生过什么）；
- 黑名单闸**仍在戳一戳分支之前**（这次改动没动它，但必须防回归）。

铁律同 verify_outbound_guard_fail_to_pass.py：目标用例先绿、锚点唯一、恢复放 finally
并逐字节核对、每轮跑控制组。

手动运行（不参与 pytest 收集）：
    python plugin/plugins/qq_auto_reply/tests/verify_poke_storm_fail_to_pass.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]  # N.E.K.O 仓库根
PLUGIN = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

TARGET_FILES = [str(TESTS / "test_qq_poke_storm.py")]
#: 控制组：同一个文件、同一个入口，改动它们就是"拦多了/拦少了"
CONTROL_FILE = str(TESTS / "test_qq_user_blacklist.py")

MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "message_dispatcher.py",
        "风暴不回戳（旧行为：人多就不回戳）",
        "                if len(timestamps) < self.POKE_BACK_MAX_PER_POKER:",
        "                if storm_count < self.POKE_STORM_MIN_POKERS and len(timestamps) < self.POKE_BACK_MAX_PER_POKER:",
    ),
    (
        "message_dispatcher.py",
        "风暴仍然进管线（旧行为：人多就让她说话）",
        "                return  # 一律不回话：戳一戳不进管线，也不抢焦点",
        "                pass  # 变异：放它继续往下走（旧行为）",
    ),
    (
        "message_dispatcher.py",
        "「戳她」判定写死（把戳别人也吞成回戳）",
        "            is_poke_me = bool(self_id and target_id == self_id)",
        "            is_poke_me = True",
    ),
    (
        "message_dispatcher.py",
        "跟戳没有上限（同一人无限互戳）",
        "    POKE_BACK_MAX_PER_POKER = 2",
        "    POKE_BACK_MAX_PER_POKER = 999",
    ),
    (
        "message_dispatcher.py",
        "风暴不留痕（日志里看不出发生过什么）",
        '                if storm_count >= self.POKE_STORM_MIN_POKERS:\n'
        '                    self.plugin._emit_log(\n'
        '                        "INFO",\n'
        '                        f"[Poke] 群{group_id} 戳一戳风暴（{storm_count} 人）→ 只跟戳，不回复",\n'
        '                    )',
        "                if False:\n"
        "                    pass",
    ),
]


def _adapt(source: str, anchor: str) -> str:
    """把按 LF 写的锚点适配到目标文件的换行风格（本仓库 CRLF 居多）。"""
    if "\r\n" in source and "\r\n" not in anchor:
        return anchor.replace("\n", "\r\n")
    return anchor


def _run(*test_files: str) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *test_files, "-q", "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode


def main() -> int:
    results: list[tuple[str, bool]] = []
    sources = {rel: (PLUGIN / rel).read_text(encoding="utf-8")
               for rel, _l, _o, _w in MUTATIONS}

    baseline = _run(*TARGET_FILES)
    if baseline != 0:
        print(f"[FAIL] 目标用例在干净树上就是红的（exit={baseline}）—— 本次取证无效，先修它")
        return 1
    print(f"[OK  ] 目标用例在干净树上绿（exit={baseline}）")

    for rel, label, old_raw, new_raw in MUTATIONS:
        source = sources[rel]
        old, new = _adapt(source, old_raw), _adapt(source, new_raw)
        count = source.count(old)
        if count != 1:
            print(f"[MISS] {rel}: 锚点出现 {count} 次（期望 1 次），本项结论无效: {old_raw[:60]!r}")
            results.append((f"{rel}: {label}", False))
            continue
        mutated = source.replace(old, new, 1)
        if mutated == source:
            print(f"[MISS] {rel}: 替换没有实际发生")
            results.append((f"{rel}: {label}", False))
            continue

        target_code = control_code = -1
        try:
            (PLUGIN / rel).write_text(mutated, encoding="utf-8")
            target_code = _run(*TARGET_FILES)
            control_code = _run(CONTROL_FILE)
        finally:
            (PLUGIN / rel).write_text(source, encoding="utf-8")

        restored = (PLUGIN / rel).read_text(encoding="utf-8") == source
        ok = target_code != 0 and control_code == 0 and restored
        results.append((f"{rel}: {label}", ok))
        print(
            f"[{'OK  ' if ok else 'MISS'}] {rel} — {label}: "
            f"目标 exit={target_code}（期望非 0）；控制组 exit={control_code}（期望 0）；已恢复={restored}"
        )

    code_clean = _run(*TARGET_FILES)
    control_ok = code_clean == 0
    results.append(("对照（只跟戳的接线在位必须绿）", control_ok))
    print(f"[{'OK  ' if control_ok else 'MISS'}] 对照: exit={code_clean}（期望 0）")

    missed = [n for n, good in results if not good]
    print()
    if missed:
        print(f"[FAIL] {len(missed)} 项不符合预期: {missed}")
        return 1
    print(f"[PASS] {len(results)}/{len(results)} —— 戳一戳「只跟戳」的五处接线都是必要条件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
