"""fail-to-pass 证据：破冰之后"按住焦点 + 起始记账"的七处接线都是必要条件。

被验证的东西（真机 bug：破冰发出后焦点立刻被抢走，接话的人被 non_focus 丢掉）：

- 送出成功后**要按住焦点**（否则下一拍就被更热闹的群抢走）；
- 按住的**时长要真的可配**（破冰 120s ≠ @ 的 90s，来由不同）；
- **要清 `msgs_after_reply`**（接话反馈闭环的起点，否则旧计数漏进这一轮）；
- **要记频率环**（主动发言也是她说了话，Pacing 口径才一致）；
- 记账失败**不许**把"已经送出的破冰"改写成失败；
- 出厂值**只有一处说法**（settings_schema 的默认值 == 门控的回落值）。

铁律同 verify_outbound_guard_fail_to_pass.py：目标用例先绿、锚点唯一、恢复放 finally
并逐字节核对、每轮跑控制组。

手动运行（不参与 pytest 收集）：
    python plugin/plugins/qq_auto_reply/tests/verify_icebreaker_hold_fail_to_pass.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]  # N.E.K.O 仓库根
PLUGIN = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

TARGET_FILES = [
    str(TESTS / "test_qq_icebreaker_hold.py"),
    str(TESTS / "test_qq_attention_behavior.py"),
]
CONTROL_FILE = str(TESTS / "test_qq_permission_levels.py")

MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "attention_gate_service.py",
        "破冰后不再按住焦点（真机 bug 原样复现）",
        "                        hold = self._icebreaker_hold_seconds()\n"
        "                        if hold > 0:\n"
        "                            attn.lock_group(group_id, seconds=hold, reason=\"icebreaker\")\n",
        "                        hold = self._icebreaker_hold_seconds()\n"
        "                        if False:\n"
        "                            attn.lock_group(group_id, seconds=hold, reason=\"icebreaker\")\n",
    ),
    (
        "attention_gate_service.py",
        "破冰后不记账（反馈周期/频率环都断）",
        "                        attn.note_proactive_speech(group_id)",
        "                        pass  # 记账被摘掉",
    ),
    (
        "attention_gate_service.py",
        "记账异常不再兜住（已送出的破冰被报成失败）",
        "                    except Exception:\n"
        "                        self._logger.warning(\n"
        "                            \"[Icebreaker] 破冰已送出，但焦点按住/记账失败（本次发言仍然算成功）\",\n"
        "                            exc_info=True,\n"
        "                        )\n",
        "                    except Exception:\n"
        "                        raise\n",
    ),
    (
        "attention_service.py",
        "按住的秒数参数被忽略（破冰退回 @ 的时长）",
        "        hold = self._lock_seconds() if seconds is None else max(0, int(seconds))",
        "        hold = self._lock_seconds()",
    ),
    (
        "attention_service.py",
        "主动发言不清 msgs_after_reply（旧计数漏进这一轮）",
        "        state.last_reply_at = ts\n        state.msgs_after_reply = 0",
        "        state.last_reply_at = ts\n        state.msgs_after_reply = int(state.msgs_after_reply or 0)",
    ),
    (
        "attention_service.py",
        "主动发言不记频率环（Pacing 口径漏掉主动发言）",
        "        times = self._reply_times.setdefault(key, [])\n        times.append(ts)",
        "        times = self._reply_times.setdefault(key, [])\n        times.extend([])",
    ),
    (
        "settings_schema.py",
        "出厂值被改成 0（按住默认关闭，两处说法不一致）",
        '    SettingSpec("icebreaker_hold_seconds", "int", 120, saveable=True,',
        '    SettingSpec("icebreaker_hold_seconds", "int", 0, saveable=True,',
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
    results.append(("对照（接线在位必须绿）", control_ok))
    print(f"[{'OK  ' if control_ok else 'MISS'}] 对照: exit={code_clean}（期望 0）")

    missed = [n for n, good in results if not good]
    print()
    if missed:
        print(f"[FAIL] {len(missed)} 项不符合预期: {missed}")
        return 1
    print(f"[PASS] {len(results)}/{len(results)} —— 破冰按住焦点的七处接线都是必要条件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
