"""fail-to-pass 证据：破冰休眠的十处接线都是必要条件。

被验证的东西（使用者 2026-09-27：「破冰一次还是没有人接话 → 拖入休眠，用其他群竞态；
休眠的群可以用 @ 唤醒」+「**没 @ 一直休**」）：

- 「没人接」**要真的接在破冰那一轮上**（普通回复没人接不许睡群）；
- 休眠群**要真的退出竞争**（否则"让其他群竞态"是空话）；
- **全员休眠不许变成全体静音**（退回最高分群，而不是返回空焦点）；
- 休眠**要放掉破冰那把锁**（锁优先于休眠过滤，不放锁等于白让）；
- **@ 要能唤醒**（清休眠 + 照常上锁）；
- 总开关 **关掉就真的不睡**，而且**当场让所有休眠群回来**（判定 + 清标记两条都要）。
- 默认口径 **「没 @ 一直休」**不许被写成"到点自己醒"。

铁律同 verify_outbound_guard_fail_to_pass.py：目标用例先绿、锚点唯一、恢复放 finally
并逐字节核对、每轮跑控制组。

手动运行（不参与 pytest 收集）：
    python plugin/plugins/qq_auto_reply/tests/verify_icebreaker_dormant_fail_to_pass.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]  # N.E.K.O 仓库根
PLUGIN = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

TARGET_FILES = [
    str(TESTS / "test_qq_icebreaker_dormant.py"),
    str(TESTS / "test_qq_icebreaker_hold.py"),
]
CONTROL_FILE = str(TESTS / "test_qq_permission_levels.py")

MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "attention_service.py",
        "破冰没人接也不再休眠（功能整个失效）",
        "            if tier == \"silent\" and self._apply_dormancy(\n"
        "                state, now, reason=\"icebreaker_no_reply\",\n"
        "            ):",
        "            if tier == \"silent\" and False:",
    ),
    (
        "attention_service.py",
        "普通回复没人接也睡群（判据放宽到所有结算）",
        "        if state.proactive_pending:\n"
        "            state.proactive_pending = False\n"
        "            if tier == \"silent\" and self._apply_dormancy(",
        "        if True:\n"
        "            state.proactive_pending = False\n"
        "            if tier == \"silent\" and self._apply_dormancy(",
    ),
    (
        "attention_service.py",
        "休眠群不退出竞争（其他群拿不到竞态机会）",
        "        awake = [state for state in states if not self._is_asleep(state, now)]\n"
        "        if awake:\n"
        "            states = awake",
        "        awake = [state for state in states if True]\n"
        "        if awake:\n"
        "            states = awake",
    ),
    (
        "attention_service.py",
        "全员休眠时返回空焦点（变成集体静音）",
        "        awake = [state for state in states if not self._is_asleep(state, now)]\n"
        "        if awake:\n"
        "            states = awake",
        "        awake = [state for state in states if not self._is_asleep(state, now)]\n"
        "        if awake or True:\n"
        "            states = awake",
    ),
    (
        "attention_service.py",
        "休眠不放锁（让位被按住时长挡住）",
        "        state.lock_until = 0\n"
        "        state.last_focus_reason = f\"dormant:{reason}\"",
        "        state.last_focus_reason = f\"dormant:{reason}\"",
    ),
    (
        "attention_service.py",
        "@ 不清休眠（唤醒失效）",
        "        if int(state.dormant_until or 0) or bool(state.dormant_forever):\n"
        "            state.dormant_until = 0\n"
        "            state.dormant_forever = False\n"
        "            self.plugin._emit_log(\n"
        "                \"INFO\", f\"[Attention] 群{normalized_group_id} 从休眠中唤醒（点名）\",",
        "        if False:\n"
        "            state.dormant_until = 0\n"
        "            state.dormant_forever = False\n"
        "            self.plugin._emit_log(\n"
        "                \"INFO\", f\"[Attention] 群{normalized_group_id} 从休眠中唤醒（点名）\",",
    ),
    (
        "attention_service.py",
        "总开关失效（关也关不掉）",
        "        if not self._dormant_enabled() or self._state_is_dormant(state, now):",
        "        if self._state_is_dormant(state, now):",
    ),
    (
        "attention_service.py",
        "关掉开关不立刻放行（标记还在，用户看不到变化）",
        "        if not self._dormant_enabled():\n            return False\n        return self._state_is_dormant(state, now)",
        "        return self._state_is_dormant(state, now)",
    ),
    (
        "attention_service.py",
        "关掉开关时标记清不掉（关了再开又立刻睡下）",
        "            if (int(state.dormant_until or 0) or bool(state.dormant_forever)) and not self._dormant_enabled():\n"
        "                state.dormant_until = 0\n"
        "                state.dormant_forever = False",
        "            if False:\n"
        "                state.dormant_until = 0\n"
        "                state.dormant_forever = False",
    ),
    (
        "attention_service.py",
        "「一直休」被当成自动醒（时间一过自己醒了）",
        "        state.dormant_until = now + seconds if seconds > 0 else 0\n"
        "        state.dormant_forever = seconds <= 0",
        "        state.dormant_until = now + (seconds if seconds > 0 else 60)\n"
        "        state.dormant_forever = False",
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
    results.append(("对照（休眠接线在位必须绿）", control_ok))
    print(f"[{'OK  ' if control_ok else 'MISS'}] 对照: exit={code_clean}（期望 0）")

    missed = [n for n, good in results if not good]
    print()
    if missed:
        print(f"[FAIL] {len(missed)} 项不符合预期: {missed}")
        return 1
    print(f"[PASS] {len(results)}/{len(results)} —— 破冰休眠的十处接线都是必要条件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
