"""账号关联是插件唯一的身份匹配机制：新 ID → 名册中的已有账号（用户或群）。

只有管理员直接关联或签发的一次性认领码可以建边；不自动猜测管理员身份。
只允许一层映射，不迁移历史记忆或合并历史信赖度。解除仅改变后续读写，
关联期间写到目标账号的数据不会拆回。宿主已有的身份绑定不在这里修改。

权限/昵称/群设置、记忆键、信赖度账号键和群提示词沿用现有解析接点。
插件本地好感度、群消息汇总等不迁移。所有管理操作仅由 identity_link 控制台入口提供。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any


class QQIdentityLinks:
    FILENAME = "identity_links.json"
    CODE_TTL_SECONDS = 600.0
    #: 去掉了 0/O、1/I 这类容易看错的字符。8 位 ≈ 40 bit，十分钟内猜不中。
    CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    CODE_LENGTH = 8
    #: 同一个人十分钟内最多失败这么多次；超过之后不回复、也不再受理，免得被当成回声筒。
    MAX_FAILED_ATTEMPTS = 5
    #: 整条消息只有这一句才算（不从闲聊里捞）。
    COMMAND_RE = re.compile(r"^\s*[/#]?(?:认领|绑定|claim|bind)\s+([A-Za-z0-9]{8})\s*$", re.IGNORECASE)
    KINDS = ("user", "group")

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._links: dict[str, dict[str, str]] = {"user": {}, "group": {}}
        #: 码 → {kind, legacy_id, level, expires_at}。只在内存里：重启即作废。
        self._codes: dict[str, dict[str, Any]] = {}
        #: 发送者 → 最近几次认领失败的时刻。只在内存里。
        self._failures: dict[str, list[float]] = {}
        self._mutation_lock = asyncio.Lock()
        #: 新通道上最近出现的 ID → {kind, id, group, text, at}。只在内存里，含一句话预览。
        self._seen: dict[tuple[str, str], dict[str, Any]] = {}
        self._loaded = False

    # ── 存取 ────────────────────────────────────────────────────

    def _path(self) -> Path:
        return Path(self.plugin.data_path(self.FILENAME))

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            payload = json.loads(self._path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._loaded = True
            return
        if not isinstance(payload, dict):
            raise ValueError("账号关联文件格式错误，请先修复，不能覆盖原记录")
        draft = {"user": {}, "group": {}}
        for kind, key in (("user", "users"), ("group", "groups")):
            table = payload.get(key, {})
            if not isinstance(table, dict):
                raise ValueError("账号关联文件格式错误，请先修复，不能覆盖原记录")
            draft[kind] = {
                str(linked).strip(): str(legacy).strip()
                for linked, legacy in table.items() if str(linked).strip() and str(legacy).strip()
            }
        self._links = draft
        self._loaded = True

    def _save(self, links: dict[str, dict[str, str]]) -> None:
        """先原子替换磁盘，再发布内存；失败不改变关联，也不消耗码。"""
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(
            {"version": 1, "users": links["user"], "groups": links["group"]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        self._links = links

    async def _commit(self, kind: str, linked: str, legacy: str | None,
                      *, expected_level: str | None = None) -> None:
        """沿用现有会话锁/结算，不另造迁移系统。结算不完就拒绝这次变更。

        在旧映射下结算，避免积压历史被写进新目标；持锁到映射落盘。
        用户关联可能影响群成员记忆，因此保守刷新现有群会话。
        """
        runtime = getattr(self.plugin, "session_runtime_service", None)
        sessions = getattr(self.plugin, "_user_sessions", {})
        handoff = getattr(self.plugin, "session_handoff_service", None)
        primary = f"private:{linked}" if kind == "user" else f"group:{linked}"
        async with AsyncExitStack() as stack:
            locked: set[str] = set()
            if runtime is not None:
                # await 期间可能出现新群会话；全部结算完后才进行无 await 的提交。
                while True:
                    keys = {primary}
                    if kind == "user":
                        keys.update(key for key in sessions if key.startswith("group:"))
                    pending = sorted(keys - locked)
                    if not pending:
                        break
                    for key in pending:
                        await stack.enter_async_context(await runtime.get_session_lock(key))
                        locked.add(key)
                        data = sessions.get(key)
                        if data is not None:
                            data["handoff_revoked"] = True
                        if handoff is not None:
                            handoff.forget(key)
                        if not await runtime.discard_session(key, reason="identity_link_changed"):
                            raise ValueError("会话尚未结算完成，账号关联未改变，请稍后重试")
            if legacy is not None:
                # 名册可能在结算的 await 期间改变，落盘前必须再次验证。
                self._validate_link(kind, linked, legacy)
                if expected_level is not None and self._own_level(kind, legacy) != expected_level:
                    raise ValueError("目标级别已变化，请重新生成认领码")
            draft = {key: dict(value) for key, value in self._links.items()}
            if legacy is None:
                draft[kind].pop(linked)
            else:
                draft[kind][linked] = legacy
            if handoff is not None:
                # 无活跃会话时也可能有旧权限/身份下的接续摘要。
                if kind == "user":
                    handoff.forget_all()
                else:
                    handoff.forget(primary)
            self._save(draft)
            self._seen.pop((kind, linked), None)

    # ── 换算（热路径：只查内存）─────────────────────────────────

    def _resolve(self, kind: str, value: Any) -> str:
        key = str(value or "").strip()
        try:
            self._load()
        except Exception:
            return key
        return self._links[kind].get(key, key)

    def user(self, actor_id: Any) -> str:
        """这个人的旧账号；没认领过就原样返回。"""
        return self._resolve("user", actor_id)

    def group(self, group_id: Any) -> str:
        return self._resolve("group", group_id)

    # ── 认领码 ──────────────────────────────────────────────────

    def _own_level(self, kind: str, value: str) -> str:
        """这个 ID 自己在名单里的级别（``none`` = 没有条目）。单次查表，收消息的热路径上也用它。"""
        manager = getattr(self.plugin, "permission_mgr" if kind == "user" else "group_permission_mgr", None)
        return manager.own_level(value) if manager is not None else "none"

    def _roster_has(self, kind: str, legacy_id: str) -> bool:
        return self._own_level(kind, legacy_id) != "none"

    def _forks(self, kind: str, linked: str, legacy: str) -> bool:
        """认领后权限和记忆会不会分叉：新 ID 自己有名单条目，而且级别和旧账号不一样。

        读名单时自己的条目优先，记忆键却落到旧账号 —— 一个普通用户认到管理员头上，权限
        还是普通，读到的却是管理员的记忆。级别一致不算，但仍需管理员明确授权关联。
        """
        own = self._own_level(kind, linked)
        return own != "none" and own != self._own_level(kind, legacy)

    def _drop_expired(self, now: float) -> None:
        for code in [c for c, row in self._codes.items() if row["expires_at"] <= now]:
            del self._codes[code]

    def has_pending(self, now: float | None = None) -> bool:
        self._drop_expired(time.time() if now is None else now)
        return bool(self._codes)

    def issue_code(self, kind: str, legacy_id: Any, *, now: float | None = None) -> dict[str, Any]:
        """给名单里的一个旧账号 / 群发一次性认领码。只能由管理员在控制台触发。"""
        self._load()
        legacy = str(legacy_id or "").strip()
        if kind not in self.KINDS or not legacy:
            raise ValueError("需要指定要认领的目标用户或群 ID")
        if not self._roster_has(kind, legacy):
            raise ValueError("这个账号不在名单里，不能发认领码")
        if legacy in self._links[kind]:
            raise ValueError("这个账号本身是别名，请对它指向的目标账号发码")
        ts = time.time() if now is None else now
        self._drop_expired(ts)
        code = "".join(secrets.choice(self.CODE_ALPHABET) for _ in range(self.CODE_LENGTH))
        self._codes[code] = {"kind": kind, "legacy_id": legacy, "level": self._own_level(kind, legacy),
                             "expires_at": ts + self.CODE_TTL_SECONDS}
        return {"code": code, "kind": kind, "legacy_id": legacy, "expires_in": int(self.CODE_TTL_SECONDS)}

    def _validate_link(self, kind: str, linked: str, legacy: str) -> None:
        if kind not in self.KINDS or not linked or not legacy:
            raise ValueError("需要指定新 ID 和名册中的目标账号")
        if linked == legacy:
            raise ValueError("新 ID 和目标账号是同一个")
        table = self._links[kind]
        if not self._roster_has(kind, legacy) or legacy in table:
            raise ValueError("目标已不在名册里，或目标本身是别名")
        if linked in table:
            raise ValueError("这个 ID 已经关联过了，请先解除")
        if linked in table.values():
            raise ValueError("这个账号已有其他 ID 指向它，不能再作为别名关联")
        if self._forks(kind, linked, legacy):
            raise ValueError("这个 ID 自己在名单里是另一个级别，请先处理权限冲突")

    async def claim(self, code: str, *, sender_id: Any, group_id: Any = "",
                    now: float | None = None) -> dict[str, str]:
        """兑换管理员签发的码；成功落盘后才消耗。兑换时重新校验名册和映射。"""
        async with self._mutation_lock:
            self._load()
            self._drop_expired(time.time() if now is None else now)
            code = str(code or "").strip().upper()
            row = self._codes.get(code)
            if row is None:
                return {"status": "invalid"}
            kind, legacy = row["kind"], row["legacy_id"]
            linked = str((group_id if kind == "group" else sender_id) or "").strip()
            result = {"kind": kind, "legacy_id": legacy, "linked_id": linked}
            if not linked:
                return {"status": "needs_group", **result}
            if linked == legacy:
                return {"status": "same", **result}
            if linked in self._links[kind]:
                return {"status": "already", **result}
            try:
                self._validate_link(kind, linked, legacy)
                await self._commit(kind, linked, legacy, expected_level=row["level"])
            except ValueError:
                return {"status": "conflict", **result}
            except OSError:
                return {"status": "save_failed", **result}
            self._codes.pop(code, None)
            return {"status": "linked", **result}

    async def unlink(self, kind: str, linked_id: Any) -> bool:
        async with self._mutation_lock:
            self._load()
            linked = str(linked_id or "").strip()
            if kind not in self.KINDS or linked not in self._links[kind]:
                return False
            await self._commit(kind, linked, None)
            return True

    async def link(self, kind: str, linked_id: Any, legacy_id: Any) -> None:
        """管理员直接关联；与认领码共用校验、会话刷新和提交。"""
        async with self._mutation_lock:
            self._load()
            linked, legacy = str(linked_id or "").strip(), str(legacy_id or "").strip()
            self._validate_link(kind, linked, legacy)
            await self._commit(kind, linked, legacy)

    SEEN_MAX = 60

    def note_seen(self, message: dict[str, Any]) -> None:
        """记下新通道上出现的、还没认领的人和群（只在内存里），给控制台的直接匹配用。"""
        sender = str(message.get("user_id") or "").strip()
        group = str(message.get("group_id") or "").strip() if message.get("message_type") == "group" else ""
        text = str(message.get("content") or "")[:24]
        for kind, value in (("user", sender), ("group", group)):
            if not value or value in self._links[kind] or value in self._links[kind].values():
                continue
            self._seen.pop((kind, value), None)
            self._seen[(kind, value)] = {"kind": kind, "id": value, "group": group, "text": text, "at": int(time.time())}
        while len(self._seen) > self.SEEN_MAX:
            self._seen.pop(next(iter(self._seen)))

    def snapshot(self) -> dict[str, Any]:
        """给控制台看的：已有的映射 + 名单里可以发码的旧账号。不含任何认领码。"""
        self._load()
        users = getattr(self.plugin, "permission_mgr", None)
        groups = getattr(self.plugin, "group_permission_mgr", None)
        return {
            "users": dict(self._links["user"]), "groups": dict(self._links["group"]),
            "seen": [row for key, row in reversed(list(self._seen.items()))
                     if key[1] not in self._links[key[0]] and key[1] not in self._links[key[0]].values()],
            "candidates": {
                "users": [
                    {"id": str(row.get("qq")), "level": row.get("level"), "nickname": row.get("nickname") or ""}
                    for row in (users.list_users() if users else []) if str(row.get("qq") or "") not in self._links["user"]
                ],
                "groups": [
                    {"id": str(row.get("group_id")), "level": row.get("level")}
                    for row in (groups.list_groups() if groups else []) if str(row.get("group_id") or "") not in self._links["group"]
                ],
            },
        }

    # ── 收到"认领 <码>" ─────────────────────────────────────────

    REPLIES = {
        "save_failed": "保存关联失败，身份没有改变，认领码未消耗，请稍后重试。",
        "linked_user": "认领成功：以后在这里也认得你了。",
        "linked_group": "认领成功：这个群接上原来的记录了。",
        "invalid": "认领码无效或已过期，请让管理员重新生成。",
        "needs_group": "这是群的认领码，请在要认领的群里 @ 我发送。",
        "already": "这里已经认领过了；要改的话请让管理员先解除关联。",
        "same": "这里就是那个账号本身，不需要认领。",
        "conflict": "目标或权限已变化、存在关联冲突，或会话尚未结算，请让管理员检查后重试。",
    }

    def _locked_out(self, sender: str, now: float) -> bool:
        recent = [at for at in self._failures.get(sender, ()) if now - at < self.CODE_TTL_SECONDS]
        if recent:
            self._failures[sender] = recent
        else:
            self._failures.pop(sender, None)
        return len(recent) >= self.MAX_FAILED_ATTEMPTS

    async def handle_message(self, message: dict[str, Any]) -> bool:
        """是认领指令就处理掉并回一句，返回 True（这条消息不再进对话）。

        只在开放平台上、并且此刻有没过期的认领码时才拦：没人在认领的时候，"绑定 XXXXXXXX"
        这样的话照常当聊天处理。群里要 @ 她才算。
        """
        client = getattr(self.plugin, "qq_client", None)
        if client is None or str(getattr(client, "CHANNEL", "") or "") != "open":
            return False
        kind = str(message.get("message_type") or "")
        if kind not in ("private", "group"):
            return False
        try:
            self._load()
            self.note_seen(message)
        except Exception:
            pass
        if kind == "group" and not message.get("is_at_bot"):
            return False
        match = self.COMMAND_RE.match(str(message.get("content") or message.get("raw_message") or ""))
        if match is None or not self.has_pending():
            return False
        sender = str(message.get("user_id") or "").strip()
        group = str(message.get("group_id") or "").strip() if kind == "group" else ""
        now = time.time()
        if self._locked_out(sender, now):
            return True          # 连着失败太多次：不受理、不回复，也不让这句话进对话
        try:
            result = await self.claim(match.group(1), sender_id=sender, group_id=group)
        except (OSError, ValueError):
            result = {"status": "save_failed"}
        status = result["status"]
        if status not in ("linked", "save_failed", "conflict"):
            self._failures.setdefault(sender, []).append(now)
        if status == "linked":
            status = f"linked_{result['kind']}"
            self.plugin.logger.info(
                f"[Identity] 认领成功: {result['kind']} {result['linked_id'][:6]}… → {result['legacy_id']}"
            )
        # 私聊和群聊都带回复锚点：开放平台上没有它就是主动消息，有额度，可能发不出去。
        segments = [{"type": "text", "data": {"text": self.REPLIES[status]}}]
        if message.get("message_id"):
            segments.insert(0, {"type": "reply", "data": {"id": str(message["message_id"])}})
        try:
            if kind == "group":
                sent = await client.send_group_message_segments(group, segments)
            else:
                sent = await client.send_private_message_segments(sender, segments)
        except Exception as exc:
            sent = None
            self.plugin.logger.warning(f"[Identity] 认领结果没发出去（{type(exc).__name__}）")
        else:
            if not sent:
                # 适配器发送失败不抛异常，只返回 None。
                self.plugin.logger.warning(f"[Identity] 认领结果没发出去（平台未确认；结果是 {status}）")
        return True
