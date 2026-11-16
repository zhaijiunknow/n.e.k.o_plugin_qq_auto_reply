"""换通道后把人和群认回来：开放平台的 openid → OneBot 时期的 QQ 号 / 群号。

OneBot 按 QQ 号、群号存名单和记忆。开放平台拿不到这两样，只给 32 位 openid，而且同一个人
私聊是 ``user_openid``、每个群里又各是一个 ``member_openid``。换通道之后按新 ID 什么都查
不到：信任级别、昵称、群设置不生效，群记忆、群成员记忆、非管理员的私聊记忆从零开始。

这里只存一张**别名表**（openid → 旧账号），在取名单、拼记忆键的地方换算一次。旧记录原地
不动：撤销只是删一行映射，切回 OneBot 也不受影响。

## 认领必须由管理员授权

平台不给 QQ 号，没法验证"你真的是那个号"。任何人发一句"我是 123456"就能认领的话，认领
主人的号就等于拿到管理员权限和主人的记忆。所以流程是：

1. 管理员在控制台对名单里某个旧 QQ 用户 / 群生成一次性认领码（``issue_code``）；
2. 对方在新通道发 ``认领 <码>``：私聊发 → 绑定私聊 openid；群里 @ 她发 → 用户码绑定这个人
   在这个群的 openid，群码绑定这个群。

码十分钟过期、只能用一次（群里发出来别人看得见，用过即废），只存在内存里。

## 不用码的两条路

* **管理员自动匹配**（``auto_link_admin``）：名单里只有一个 QQ 号的管理员时，openid 形态的
  管理员条目自动认到它上面。那些 openid 是扫码绑定时平台给出的（或管理员自己加的），
  是"这个人就是主人"唯一能被证明的地方。有两个以上 QQ 管理员就不猜。撤销过的不再自动认。
* **管理员在控制台直接匹配**（``link``）：新通道上出现过、还没认领的 ID 会列出来，附最近
  一句话帮助辨认，管理员点一下指给某个旧账号。

别人的 ID 做不到全自动：平台不给 QQ 号，没有任何信号能说明一个 openid 是谁。

## 第一版管到哪里

名单（级别、昵称、群设置）、记忆服务的键（群 / 群成员 / 私聊对象 / 信赖度账号）、按群写的
提示词。插件本地按 ID 存的状态（好感度、群消息汇总等）还没接。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any


class QQIdentityLinks:
    FILENAME = "identity_links.json"
    CODE_TTL_SECONDS = 600.0
    #: 去掉了 0/O、1/I 这类容易看错的字符。8 位 ≈ 40 bit，十分钟内猜不中。
    CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    CODE_LENGTH = 8
    #: 整条消息只有这一句才算（不从闲聊里捞）。
    COMMAND_RE = re.compile(r"^\s*[/#]?(?:认领|绑定|claim|bind)\s+([A-Za-z0-9]{8})\s*$", re.IGNORECASE)
    KINDS = ("user", "group")

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._links: dict[str, dict[str, str]] = {"user": {}, "group": {}}
        #: 码 → {kind, legacy_id, expires_at}。只在内存里：重启即作废。
        self._codes: dict[str, dict[str, Any]] = {}
        #: 被撤销过的自动匹配（openid）：不再自动认回去。
        self._auto_off: set[str] = set()
        #: 新通道上最近出现的 ID → {kind, id, group, text, at}。只在内存里，含一句话预览。
        self._seen: dict[tuple[str, str], dict[str, Any]] = {}
        self._loaded = False

    # ── 存取 ────────────────────────────────────────────────────

    def _path(self) -> Path:
        return Path(self.plugin.data_path(self.FILENAME))

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self._path().read_text(encoding="utf-8"))
        except Exception:
            return
        for kind, key in (("user", "users"), ("group", "groups")):
            table = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(table, dict):
                self._links[kind] = {
                    str(linked).strip(): str(legacy).strip()
                    for linked, legacy in table.items() if str(linked).strip() and str(legacy).strip()
                }
        if isinstance(payload, dict) and isinstance(payload.get("auto_off"), list):
            self._auto_off = {str(item) for item in payload["auto_off"]}

    def _save(self) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(
            {"version": 1, "users": self._links["user"], "groups": self._links["group"],
             "auto_off": sorted(self._auto_off)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

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

    def _roster_has(self, kind: str, legacy_id: str) -> bool:
        manager = getattr(self.plugin, "permission_mgr" if kind == "user" else "group_permission_mgr", None)
        if manager is None:
            return False
        rows = manager.list_users() if kind == "user" else manager.list_groups()
        return any(str(row.get("qq" if kind == "user" else "group_id") or "") == legacy_id for row in rows)

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
            raise ValueError("需要指定要认领的旧 QQ 号或群号")
        if not self._roster_has(kind, legacy):
            raise ValueError("这个账号不在名单里，不能发认领码")
        if legacy in self._links[kind]:
            raise ValueError("这个账号本身是认领来的别名，请对它指向的旧账号发码")
        ts = time.time() if now is None else now
        self._drop_expired(ts)
        code = "".join(secrets.choice(self.CODE_ALPHABET) for _ in range(self.CODE_LENGTH))
        self._codes[code] = {"kind": kind, "legacy_id": legacy, "expires_at": ts + self.CODE_TTL_SECONDS}
        return {"code": code, "kind": kind, "legacy_id": legacy, "expires_in": int(self.CODE_TTL_SECONDS)}

    def claim(self, code: str, *, sender_id: Any, group_id: Any = "", now: float | None = None) -> dict[str, str]:
        """用码认领。返回 ``{status, kind, legacy_id, linked_id}``；只有 ``linked`` 会消耗这个码。

        status：``linked`` / ``invalid``（码不对或过期）/ ``needs_group``（群码发在了私聊）/
        ``already``（这个 ID 已经认领过别的）/ ``same``（它就是那个旧账号）。
        """
        self._load()
        self._drop_expired(time.time() if now is None else now)
        row = self._codes.get(str(code or "").strip().upper())
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
        del self._codes[str(code).strip().upper()]
        self._links[kind][linked] = legacy
        self._save()
        return {"status": "linked", **result}

    def unlink(self, kind: str, linked_id: Any) -> bool:
        self._load()
        linked = str(linked_id or "").strip()
        if kind not in self.KINDS or self._links[kind].pop(linked, None) is None:
            return False
        if kind == "user":
            self._auto_off.add(linked)   # 撤销的若是自动匹配，别在下次载入名单时又认回去
        self._save()
        return True

    # ── 不用码：管理员直接匹配 / 管理员自动匹配 ─────────────────

    def link(self, kind: str, linked_id: Any, legacy_id: Any) -> None:
        """管理员在控制台直接把一个新 ID 指给名单里的旧账号。"""
        self._load()
        linked, legacy = str(linked_id or "").strip(), str(legacy_id or "").strip()
        if kind not in self.KINDS or not linked or not legacy:
            raise ValueError("需要指定新 ID 和要匹配的旧账号")
        if linked == legacy:
            raise ValueError("新 ID 和旧账号是同一个")
        if not self._roster_has(kind, legacy) or legacy in self._links[kind]:
            raise ValueError("要匹配的旧账号不在名单里")
        if linked in self._links[kind]:
            raise ValueError("这个 ID 已经认领过了，请先撤销")
        self._links[kind][linked] = legacy
        self._auto_off.discard(linked)
        self._seen.pop((kind, linked), None)
        self._save()

    OPENID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")

    def auto_link_admin(self, permission_mgr: Any) -> list[str]:
        """名单里只有一个 QQ 号管理员时，把 openid 形态的管理员条目自动认到它上面。"""
        try:
            self._load()
            admins = [str(row.get("qq") or "") for row in permission_mgr.list_users() if row.get("level") == "admin"]
        except Exception:
            return []
        legacy = [qq for qq in admins if qq.isdigit()]
        if len(legacy) != 1:
            return []
        added = [
            qq for qq in admins
            if self.OPENID_RE.match(qq) and qq not in self._links["user"] and qq not in self._auto_off
        ]
        for qq in added:
            self._links["user"][qq] = legacy[0]
        if added:
            self._save()
            self.plugin.logger.info(f"[Identity] 管理员自动匹配: {len(added)} 个 openid → {legacy[0]}")
        return added

    SEEN_MAX = 60

    def note_seen(self, message: dict[str, Any]) -> None:
        """记下新通道上出现的、还没认领的人和群（只在内存里），给控制台的直接匹配用。"""
        sender = str(message.get("user_id") or "").strip()
        group = str(message.get("group_id") or "").strip() if message.get("message_type") == "group" else ""
        text = str(message.get("content") or "")[:24]
        for kind, value in (("user", sender), ("group", group)):
            if not value or value in self._links[kind] or self._roster_has(kind, value):
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
            "seen": [row for key, row in reversed(list(self._seen.items())) if key[1] not in self._links[key[0]]],
            "candidates": {
                "users": [
                    {"id": str(row.get("qq")), "level": row.get("level"), "nickname": row.get("nickname") or ""}
                    for row in (users.list_users() if users else []) if str(row.get("qq") or "").isdigit()
                ],
                "groups": [
                    {"id": str(row.get("group_id")), "level": row.get("level")}
                    for row in (groups.list_groups() if groups else []) if str(row.get("group_id") or "").isdigit()
                ],
            },
        }

    # ── 收到"认领 <码>" ─────────────────────────────────────────

    REPLIES = {
        "linked_user": "认领成功：以后在这里也认得你了。",
        "linked_group": "认领成功：这个群接上原来的记录了。",
        "invalid": "认领码无效或已过期，请让管理员重新生成。",
        "needs_group": "这是群的认领码，请在要认领的群里 @ 我发送。",
        "already": "这里已经认领过了；要改的话请让管理员先撤销。",
        "same": "这里就是那个账号本身，不需要认领。",
    }

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
        result = self.claim(match.group(1), sender_id=sender, group_id=group)
        status = result["status"]
        if status == "linked":
            status = f"linked_{result['kind']}"
            self.plugin.logger.info(
                f"[Identity] 认领成功: {result['kind']} {result['linked_id'][:6]}… → {result['legacy_id']}"
            )
            invalidate = getattr(self.plugin, "_invalidate_private_session", None)
            if callable(invalidate) and kind == "private":
                try:
                    await invalidate(sender)
                except Exception:
                    pass
        text = self.REPLIES[status]
        try:
            if kind == "group":
                segments = [{"type": "text", "data": {"text": text}}]
                if message.get("message_id"):
                    segments.insert(0, {"type": "reply", "data": {"id": str(message["message_id"])}})
                await client.send_group_message_segments(group, segments)
            else:
                await client.send_message(sender, text)
        except Exception as exc:
            self.plugin.logger.warning(f"[Identity] 认领结果没发出去（{type(exc).__name__}）")
        return True
