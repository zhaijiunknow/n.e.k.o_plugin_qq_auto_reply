"""fail-to-pass 证据：休眠（「这个群冷漠了」）的每条判据都是必要条件。

使用者口径（2026-09-29）：「群聊如果冷漠了就休眠，大概就是半个小时没有任何发言」。
这条证据把每一处接线拆一遍，确认拆掉就会红：

- 触发：静默够久 → 睡下；不够久 / 从没说过话 / 开关关掉 / 阈值配 0 → 都不睡
- 睡着时分数冻住；醒来时分数保留（不必从零熬）
- 唤醒：点名即醒，且把回溯补回游标推到此刻（睡着那段的账不补）
- 自动醒（`dormancy_auto_wake_seconds > 0`）
- 门控：睡着的群**只答点名**（普通消息 ignore，@ / 引用 / 关键词照旧必回）
- 存档往返与"启动时清掉旧破冰时代的标记"

铁律同 verify_attention_scope_fail_to_pass.py：目标用例先绿、锚点唯一、恢复放 finally
并逐字节核对、每轮跑控制组。

手动运行（不参与 pytest 收集），要在**真 app root** 里跑（`parents[4]` 要能落到宿主仓库）：
    python plugin/plugins/qq_auto_reply/tests/verify_dormancy_fail_to_pass.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]  # N.E.K.O 仓库根
PLUGIN = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

TARGET_FILES = [
    str(TESTS / "test_qq_dormancy.py"),
    str(TESTS / "test_qq_attention_heat.py"),
]
CONTROL_FILE = str(TESTS / "test_qq_permission_levels.py")

MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "attention_service.py",
        "总开关关掉也照样睡（关不掉的开关比没有开关更糟）",
        "        if not self._dormancy_enabled():\n            return False\n        idle = self._dormancy_idle_seconds()",
        "        if False:\n            return False\n        idle = self._dormancy_idle_seconds()",
    ),
    (
        "attention_service.py",
        "阈值配 0 还照样睡（0=关掉自动休眠 被写反）",
        "        if idle <= 0 or self._state_is_dormant(state, now):",
        "        if self._state_is_dormant(state, now):",
    ),
    (
        "attention_service.py",
        "不管静默多久都睡（刚有人说话的群也被睡掉）",
        "        if last_message_at <= 0 or now - last_message_at < idle:\n            return False",
        "        if False:\n            return False",
    ),
    (
        "attention_service.py",
        "「一直睡」写不出来（永远不设 dormant_forever，睡一觉立刻醒）",
        "        state.dormant_forever = auto_wake <= 0",
        "        state.dormant_forever = False",
    ),
    (
        "attention_service.py",
        "自动醒的秒数不算进 dormant_until（点了自动醒却一直睡）",
        "        state.dormant_until = now + auto_wake if auto_wake > 0 else 0",
        "        state.dormant_until = 0",
    ),
    (
        "attention_service.py",
        "睡着的群分数照掉（醒来要从零熬）",
        "        if state.heat == \"dormant\":\n            # 睡着的群分数冻住",
        "        if False:\n            # 睡着的群分数冻住",
    ),
    (
        "attention_service.py",
        "热度档不再认休眠（面板与提示词都说它在聊）",
        "        if bool(state.dormant_forever) or int(state.dormant_until or 0) > now:\n            return \"dormant\"",
        "        if False:\n            return \"dormant\"",
    ),
    (
        "attention_service.py",
        "存档不读休眠标记（睡下 → 下一次读状态又醒了）",
        "            dormant_until=int(data.get(\"dormant_until\") or 0),\n            dormant_forever=bool(data.get(\"dormant_forever\") or False),",
        "            dormant_until=0,\n            dormant_forever=False,",
    ),
    (
        "attention_service.py",
        "启动时不清旧标记（旧破冰时代睡过的群升级后立刻睡下且不醒）",
        "                payload[\"dormant_until\"] = 0\n                payload[\"dormant_forever\"] = False",
        "                pass",
    ),
    (
        "attention_service.py",
        "唤醒时不动回溯补回游标（睡着那半小时的旧账被翻出来补）",
        "        state.last_focus_at = self._current_time()\n        state.last_focus_reason = f\"wake:{reason}\"",
        "        state.last_focus_reason = f\"wake:{reason}\"",
    ),
    (
        "attention_gate_service.py",
        "门控不看休眠（睡着的群照样被普通消息叫起来）",
        "        if is_dormant:",
        "        if False:",
    ),
]


def _adapt(source: str, anchor: str) -> str:
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
            print(f"[MISS] {rel}: 锚点出现 {count} 次（期望 1 次），本项结论无效: {old_raw[:70]!r}")
            results.append((f"{rel}: {label}", False))
            continue
        mutated = source.replace(old, new, 1)
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
    results.append(("对照（休眠在位必须绿）", control_ok))
    print(f"[{'OK  ' if control_ok else 'MISS'}] 对照: exit={code_clean}（期望 0）")

    missed = [n for n, good in results if not good]
    print()
    if missed:
        print(f"[FAIL] {len(missed)} 项不符合预期: {missed}")
        return 1
    print(f"[PASS] {len(results)}/{len(results)} —— 休眠的每条判据都是必要条件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
