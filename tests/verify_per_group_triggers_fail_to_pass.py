"""fail-to-pass 证据：**按群触发**的破冰 / 群记忆摘要 / 回溯补回，每一条判据都是必要条件。

背景（2026-09-29 使用者口径「每个群自己管自己的注意力」）：跨群焦点取舍删掉后，
原来挂在"焦点切换"上的三件事全部改成按群触发 ——

- 破冰：从"焦点反复落到同一群却没人说话"（数切换次数）改成"**这个群自己**静了多久"
- 群记忆摘要：从"焦点离开这个群时推一次"改成"**每群自己的**间隔推一次"
- 回溯补回：从"焦点切到这个群"改成"**这个群自己**凉转热的那个瞬间"

每件事都必须真的按群判、并且真的会被跳过条件拦住，否则：死群会被反复打扰、
破冰变成刷屏、凉群回来时把陈年旧账全翻出来补一遍。

铁律同 verify_attention_scope_fail_to_pass.py：目标用例先绿、锚点唯一、恢复放 finally
并逐字节核对、每轮跑控制组。

手动运行（不参与 pytest 收集），要在**真 app root** 里跑（`parents[4]` 要能落到宿主仓库）：
    python plugin/plugins/qq_auto_reply/tests/verify_per_group_triggers_fail_to_pass.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]  # N.E.K.O 仓库根
PLUGIN = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

TARGET_FILES = [
    str(TESTS / "test_qq_per_group_maintenance.py"),
    str(TESTS / "test_qq_per_group_retro_trigger.py"),
]
CONTROL_FILE = str(TESTS / "test_qq_permission_levels.py")

MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "attention_gate_service.py",
        "休眠的群照样被破冰叫起来（她主动开口没人接，白说）",
        '        if bool(getattr(state, "dormant_forever", False)) or int(getattr(state, "dormant_until", 0) or 0) > now:',
        "        if False:",
    ),
    (
        "attention_gate_service.py",
        "有人点名叫她、锁还没到期也去破冰（该回应人的时候另起话题）",
        '        if int(getattr(state, "lock_until", 0) or 0) > now:',
        "        if False:",
    ),
    (
        "attention_gate_service.py",
        "不看这个群静了多久（刚有人说话的群也被破冰打断）",
        "        if now - last_message_at < idle_seconds:",
        "        if False:",
    ),
    (
        "attention_gate_service.py",
        "破冰没有冷却（每个 tick 都问一次 LLM，破冰变成刷屏）",
        "        if now - int(self._last_icebreaker_at.get(group_id, 0)) < idle_seconds:",
        "        if False:",
    ),
    (
        "attention_gate_service.py",
        "`icebreaker_idle_seconds=0` 关不掉主动破冰（0 被当成「随便破」）",
        "        if idle_seconds <= 0:",
        "        if False:",
    ),
    (
        "attention_gate_service.py",
        "群记忆摘要不看间隔（每个 tick 都推一次）",
        "        if now - int(self._last_digest_at.get(group_id, 0)) < interval:",
        "        if False:",
    ),
    (
        "attention_gate_service.py",
        "群记忆关着也照样推（opt-in 被绕过）",
        '        if not bool((getattr(self.plugin, "_qq_settings", {}) or {}).get("group_memory_enabled", False)):',
        "        if False:",
    ),
    (
        "attention_gate_service.py",
        "一轮里给每个冷群都破一次冰（她变成群发器；重载后所有冷群同时被打扰）",
        "            if not broke_ice and self._participates_in_attention(group_id):\n"
        "                broke_ice = await self._maybe_break_ice(group_id, now)",
        "            if self._participates_in_attention(group_id):\n"
        "                broke_ice = await self._maybe_break_ice(group_id, now)",
    ),
    (
        "attention_gate_service.py",
        "维护循环被一轮异常杀死（她从此再也不主动说话，且日志里查不到）",
        "            except Exception as e:\n"
        '                self._logger.warning(f"[Gate] 按群维护轮次异常，已跳过本轮: {e}")',
        "            except Exception:\n                raise",
    ),
    (
        "attention_gate_service.py",
        "回溯补回不看门槛（一两条没看过的消息也要花一次 LLM 调用）",
        "        if not unreviewed or len(unreviewed) < min_unreviewed:",
        "        if not unreviewed:",
    ),
    (
        "attention_gate_service.py",
        "回溯补回没有按群冷却（凉转热比焦点切换频繁得多，会把旧账反复翻出来）",
        "        if cooldown > 0 and now - last_at < cooldown:",
        "        if False:",
    ),
    (
        "attention_gate_service.py",
        "凉转热的判定去问别的群（判据不落在本群身上）",
        "        now_in_conversation = bool(attention.is_in_conversation(key))",
        '        now_in_conversation = bool(attention.is_in_conversation("__some_other_group__"))',
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
            print(f"[MISS] {rel}: 锚点出现 {count} 次（期望 1 次），本项结论无效: {old_raw[:70]!r}")
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
    results.append(("对照（按群触发在位必须绿）", control_ok))
    print(f"[{'OK  ' if control_ok else 'MISS'}] 对照: exit={code_clean}（期望 0）")

    missed = [n for n, good in results if not good]
    print()
    if missed:
        print(f"[FAIL] {len(missed)} 项不符合预期: {missed}")
        return 1
    print(f"[PASS] {len(results)}/{len(results)} —— 破冰 / 摘要 / 补回的每条按群判据都是必要条件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
