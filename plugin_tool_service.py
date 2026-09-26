"""把**其它插件**的能力挂到 QQ 会话上（只在开放平台启用）。

## 为什么需要它

猫娘在 QQ 里只会聊天：她能调的工具只有一个 `recall_memory`（本插件每轮自己挂的）。
本体明明有二十来个插件（点歌、米家、搜索、Minecraft…），但她一个都用不上 ——
因为"能不能调"取决于**本次会话挂没挂那个工具**，而不是权限或通道。

宿主自己的聊天会话是把全量工具表塞进会话的（`main_logic/core/lifecycle.py`
的 `tool_definitions=self.tool_registry.all()`）；QQ 这条会话只挂了自己那一个。
这一步就是把这个缺口补上，并**按分级**决定谁能用哪个插件。

## 四道闸（使用者定的）

* **通道闸**：只在开放平台挂。NapCat 那边"群里什么样的人都有"，多给一层工具面就多
  一层风险；
* **启动闸**：只带**已经启动**的插件 —— 没启动的挂上去，模型点到只会拿到一个错误；
* **非 QQ 闸**：排除自己与 `qq*` 家族（让她"通过调用别的插件"再回到 QQ 发送链路是
  自指）；
* **权限闸**：`all` 档给名册里的人（admin/trusted/normal），`admin` 档只给管理员，
  **认不出来的人（`none`）一个工具都不给**。

另有一条不是闸但要命的：handler 只认**本轮挂上去的 entry 清单**，
模型不能自己编一个 entry id 来透传。

## 候选表为什么只走后台刷新

真机实测（2026-09-26）：在插件 entry handler 里 `await ctx.query_plugins(...)`
**一次都没成功过** —— 5s / 15s 都超时（日志 `Plugin query timed out after 15.0s`），
而后台任务里跑同一条查询正常。所以调用方（entry handler / 每轮生成）一律**只读缓存**，
缓存过期就踢一次后台刷新、先返回手里那份。界面那条路会等一小会儿，但同样有上限。

## 为什么是一插件一工具（而不是一 entry 一工具）

宿主只给到**插件**级元数据 + entry id 列表（`ctx.query_plugins(include_events=True)`），
**不给每个 entry 的入参 schema**。硬造 schema 会让模型照着错的参数调用；一插件一工具、
把 entry id 做成枚举，是唯一不撒谎的形状。代价是模型得知道 entry 的语义 ——
所以描述里带上插件自己的 description 与 entry 列表。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

#: 分级取值。``all`` = 所有人都能用；``admin`` = 只有管理员能用。
TIER_ALL = "all"
TIER_ADMIN = "admin"
VALID_TIERS = (TIER_ALL, TIER_ADMIN)

#: 一次挂几个插件工具的上限。工具定义**每轮都进 system 段**（没有 prompt caching），
#: 所以这是个预算闸而不是功能闸：超出的按 id 排序截断。
MAX_MOUNTED_PLUGINS = 8

#: 给界面用的同一个数字（界面要显示"最多 N 个"，别在两处各写一份）。
PLUGIN_TOOL_MAX_MOUNTED = MAX_MOUNTED_PLUGINS

#: 工具结果的字符上限（回灌给模型的那份）。宿主对 MCP 工具结果也有同类上限
#: （`config.MCP_TOOL_RESULT_MAX_TOKENS`），这里按字符给一个更保守的值：
#: 别的插件可能返回长文（搜索结果、Minecraft 状态、歌单…），整段回灌会顶爆上下文。
RESULT_MAX_CHARS = 2000

#: 调用别的插件的超时（秒）。比默认 10s 宽一点：跨插件是一次 IPC 往返。
CALL_TIMEOUT_SECONDS = 20.0

#: 读取宿主注册表的超时。实测 5s 不够（见模块 docstring），给宽一点；真正的保护是
#: "只在后台任务里查" + TTL 缓存，而不是把这个数字调来调去。
QUERY_TIMEOUT_SECONDS = 15.0

#: 候选表的缓存时长（秒）。
CANDIDATES_TTL_SECONDS = 60.0

#: 界面那条读路径最多等多久（秒）。
UI_WAIT_SECONDS = 2.0

#: 可选参数最多列几个名字（只列名，不列类型/说明）。
OPTIONAL_PARAMS_MAX_NAMES = 4

#: 每个 entry 在工具描述里占的字符上限。工具定义**每轮都进 system 段**，而一个插件
#: 可能有十几个 entry —— 不封顶就会把预算吃光。截断留省略号，看得见被砍过。
ENTRY_HINT_MAX_CHARS = 140

#: 插件 id 里凡是这种前缀都不进候选（QQ 家族 = 自己这条链路）。
EXCLUDED_ID_PREFIXES = ("qq",)

_TOOL_PREFIX = "plugin_"


class QQPluginToolService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        #: ``(取到的时刻, 候选表)``。None = 还没取过。
        self._candidates_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._refresh_task: Any = None

    # ── 候选表的取数：后台刷新 + 读缓存 ────────────────────────────

    def ensure_refresh_loop(self) -> None:
        """确保后台刷新任务在跑（幂等）。没有事件循环时静默跳过。"""
        task = self._refresh_task
        if task is not None and not task.done():
            return
        try:
            import asyncio

            self._refresh_task = asyncio.get_running_loop().create_task(
                self._refresh_loop()
            )
        except RuntimeError:
            self._refresh_task = None

    async def _refresh_loop(self) -> None:
        import asyncio

        while True:
            try:
                await self.refresh_candidates()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.plugin.logger.warning("插件工具桥后台刷新候选失败", exc_info=True)
            await asyncio.sleep(CANDIDATES_TTL_SECONDS)

    async def refresh_candidates(self) -> list[dict[str, Any]]:
        """真的去问宿主（**只在后台任务/启动时调用**），并写进缓存。"""
        rows = await self._query_host_registry()
        self._candidates_cache = (time.monotonic(), rows)
        return rows

    def cached_candidates(self) -> list[dict[str, Any]]:
        cached = self._candidates_cache
        return list(cached[1]) if cached else []

    def cache_is_fresh(self) -> bool:
        cached = self._candidates_cache
        return bool(cached) and (time.monotonic() - cached[0]) < CANDIDATES_TTL_SECONDS

    async def list_candidates(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """候选表（**读缓存**，全量非 QQ 插件）。缓存过期就踢一次后台刷新，先返回手里那份。"""
        if refresh:
            return await self.wait_for_a_fresh_cache()
        if self.cache_is_fresh():
            return self.cached_candidates()
        self.ensure_refresh_loop()
        return self.cached_candidates()

    async def wait_for_a_fresh_cache(self, timeout: float = UI_WAIT_SECONDS) -> list[dict[str, Any]]:
        """等一次刷新落进缓存（界面用），超时就给手里那份 —— 界面不该被宿主拖住。"""
        import asyncio

        self.ensure_refresh_loop()
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.cache_is_fresh():
                return self.cached_candidates()
            await asyncio.sleep(0.1)
        return self.cached_candidates()

    # ── 宿主注册表 ──────────────────────────────────────────────────

    @staticmethod
    def _registry_url() -> str:
        """宿主插件服务的基址（与 `_push_ui_event` 同一个）。"""
        from config import USER_PLUGIN_BASE

        return f"{str(USER_PLUGIN_BASE).rstrip('/')}/plugins"

    async def _fetch_registry(self) -> Any:
        """问宿主插件服务要**已注册**的插件清单。

        为什么走 HTTP 而不是 SDK 的 `ctx.query_plugins`：真机实测那条路**根本没到过
        宿主** —— 客户端 15s 超时（`Plugin query timed out after 15.0s`），而宿主插件
        服务的日志里连一条 `PLUGIN_QUERY` 都没有；同一时刻插件的 HTTP 调用
        （`/ui-api/push`）全部 200。`GET /plugins` 给的东西还更全：每个插件带
        `status` 与 `entries[{id, name, description, input_schema}]`。

        找不到 HTTP 客户端（轻量调用方/测试）时返回 None，由调用方当成"没有候选"。
        """
        try:
            from utils.internal_http_client import get_internal_http_client

            client = get_internal_http_client()
        except Exception:
            return None
        try:
            response = await client.get(self._registry_url(), timeout=QUERY_TIMEOUT_SECONDS)
            return response.json()
        except Exception as exc:
            self.plugin.logger.warning(f"查询宿主插件目录失败，插件工具桥本轮无候选: {exc}")
            return None

    async def _query_host_registry(self) -> list[dict[str, Any]]:
        """宿主插件目录里**所有非 QQ 的**插件（含 entry 列表与**是否在跑**）。

        两个消费者要的东西不一样，所以**取数时不筛启动状态**：

        * **界面**（使用者定的）：加插件时不检查有没有启动，全量出卡片让人先分配档位 ——
          否则"想给小工具分个档，得先把它启动起来"这件事本身就很别扭；
        * **提示词**：只在**真的在跑**的插件上挂工具（`select_mounted` 里那道闸）。
          没启动的挂在提示词里只会让模型点到错误、还白占每轮的预算。

        结果永远留痕（候选数 + 在跑数 + 状态分布）：「界面上一片空」必须能自证原因 ——
        是查询失败、宿主那边没有插件、还是全被非 QQ 闸排掉了。
        """
        payload = await self._fetch_registry()
        raw_plugins = (payload or {}).get("plugins") if isinstance(payload, dict) else None
        if not isinstance(raw_plugins, list):
            if payload is not None:
                self.plugin.logger.info(
                    f"宿主插件目录返回的形状不认识（{type(payload).__name__}），插件工具桥本轮无候选"
                )
            return []

        candidates: list[dict[str, Any]] = []
        statuses: dict[str, int] = {}
        for item in raw_plugins:
            if not isinstance(item, dict):
                continue
            plugin_id = str(item.get("id") or item.get("plugin_id") or "").strip()
            status = str(item.get("status") or "").strip().lower() or "(空)"
            statuses[status] = statuses.get(status, 0) + 1
            if not plugin_id or self._is_excluded(plugin_id):
                continue
            candidates.append({
                "plugin_id": plugin_id,
                "name": str(item.get("name") or plugin_id),
                "description": str(item.get("description") or ""),
                "entries": sorted(set(self._entry_ids(item))),
                "entry_hints": self._entry_hints(item),
                #: 只有 True 的那些会进提示词（见 :meth:`select_mounted`）。
                "running": status == "running",
            })
        candidates.sort(key=lambda row: row["plugin_id"])
        running = sum(1 for row in candidates if row["running"])
        self.plugin.logger.info(
            f"插件工具桥候选: {len(candidates)} 个（在跑 {running} 个）；"
            f"宿主目录 {len(raw_plugins)} 项，状态分布 {statuses}"
        )
        return candidates

    @staticmethod
    def _is_excluded(plugin_id: str) -> bool:
        """QQ 家族（含自己）不进候选：让她"通过调用别的插件"再回到 QQ 发送链路是自指。"""
        lowered = str(plugin_id or "").strip().lower()
        return any(lowered.startswith(prefix) for prefix in EXCLUDED_ID_PREFIXES)

    @staticmethod
    def _entry_ids(item: dict[str, Any]) -> list[str]:
        """插件记录里的可调 entry id 列表（形状容错：entries 也可能是纯字符串表）。"""
        raw = item.get("entries")
        if not isinstance(raw, list):
            return []
        ids: list[str] = []
        for entry in raw:
            if isinstance(entry, dict):
                value = str(entry.get("id") or entry.get("event_id") or "").strip()
            else:
                value = str(entry or "").strip()
            if value:
                ids.append(value)
        return ids

    @staticmethod
    def _entry_hints(item: dict[str, Any]) -> dict[str, str]:
        """``{entry_id: 一行说明}`` —— 说明 + 必填参数名。

        带参数名不是装饰：`params` 是自由对象（宿主没给逐 entry 的 schema 也可以照抄
        一份进来，但描述里点名"必填 time、message"比让模型猜准得多）。整行有长度上限，
        见 :data:`ENTRY_HINT_MAX_CHARS`。
        """
        raw = item.get("entries")
        if not isinstance(raw, list):
            return {}
        hints: dict[str, str] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or entry.get("event_id") or "").strip()
            if not entry_id:
                continue
            text = str(entry.get("description") or entry.get("name") or "").strip()
            required = QQPluginToolService._required_params(entry)
            optional = QQPluginToolService._optional_params(entry)
            parts = []
            if required:
                parts.append(f"必填 {'、'.join(required)}")
            if optional:
                parts.append(f"可选 {'、'.join(optional)}")
            if parts:
                params_text = "；".join(parts)
                text = f"{text}（{params_text}）" if text else params_text
            if text:
                if len(text) > ENTRY_HINT_MAX_CHARS:
                    text = text[:ENTRY_HINT_MAX_CHARS] + "…"
                hints[entry_id] = text
        return hints

    @staticmethod
    def _required_params(entry: dict[str, Any]) -> list[str]:
        schema = entry.get("input_schema")
        if not isinstance(schema, dict):
            return []
        required = schema.get("required")
        if not isinstance(required, list):
            return []
        return [str(name) for name in required if str(name or "").strip()]

    @staticmethod
    def _optional_params(entry: dict[str, Any]) -> list[str]:
        """非必填参数名（最多列几个）。

        真机教训（`writer_power_analysis:analyze_text`）：它有一个 `use_neko_model`
        可选参数 —— 缺 api_key 时**用它就能跑**。只列必填参数的话，模型看不到它，
        于是照样按缺 key 的默认路走，任务当场失败。可选参数名值得那几十个字符。
        """
        schema = entry.get("input_schema")
        if not isinstance(schema, dict):
            return []
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return []
        required = {name.lower() for name in QQPluginToolService._required_params(entry)}
        names = [
            str(name) for name in properties
            if str(name or "").strip() and str(name).lower() not in required
        ]
        return names[:OPTIONAL_PARAMS_MAX_NAMES]

    # ── 分级（配置） ────────────────────────────────────────────────

    def tiers(self) -> dict[str, str]:
        """``{plugin_id: "all"|"admin"}``；非法项直接丢掉（配置手改坏了不该炸会话）。"""
        raw = (self.plugin._qq_settings or {}).get("qq_open_plugin_tools") or {}
        if not isinstance(raw, dict):
            return {}
        clean: dict[str, str] = {}
        for key, value in raw.items():
            plugin_id = str(key or "").strip()
            tier = str(value or "").strip().lower()
            if plugin_id and tier in VALID_TIERS:
                clean[plugin_id] = tier
        return clean

    def allowed_tiers_for(self, permission_level: Any) -> set[str]:
        """这一轮这个说话人能用的档位。

        开放平台上的"所有人"指**名册里认得的人**（admin / trusted / normal）；
        认不出来的（``none``）什么都不给 —— 那正是陌生人 @ 一下就能指挥插件的情形。
        """
        level = str(permission_level or "").strip().lower()
        if level == "admin":
            return {TIER_ALL, TIER_ADMIN}
        if level in ("trusted", "normal"):
            return {TIER_ALL}
        return set()

    def select_mounted(
        self, candidates: list[dict[str, Any]], *, allowed_tiers: set[str],
    ) -> list[tuple[dict[str, Any], str]]:
        """这一轮实际要挂的 ``(候选, 档位)``：白名单 ∧ **此刻在跑** ∧ 这个人有权用。

        **启动闸就在这一处**：候选表（界面用）是全量的，提示词只带在跑的那些 ——
        没启动的挂在提示词里，模型点到只会拿到一个错误，还白占每轮的预算。
        没有可调 entry 的插件同样跳过（挂上去只会让模型空调用）。

        顺序按 plugin_id 排：挂载顺序不该取决于调用方给的候选顺序（同一次调用要可复现）。
        """
        tiers = self.tiers()
        by_id = {row["plugin_id"]: row for row in candidates}
        chosen: list[tuple[dict[str, Any], str]] = []
        for plugin_id, tier in sorted(tiers.items()):
            if tier not in allowed_tiers:
                continue
            row = by_id.get(plugin_id)
            if row is None or not row.get("running"):
                continue
            if not row.get("entries"):
                continue
            chosen.append((row, tier))
        return chosen[:MAX_MOUNTED_PLUGINS]

    # ── 工具定义与分发 ──────────────────────────────────────────────

    @staticmethod
    def tool_name(plugin_id: str) -> str:
        return f"{_TOOL_PREFIX}{plugin_id}"

    def build_tool_definition(self, candidate: dict[str, Any], tier: str) -> Any:
        from main_logic.tool_calling import ToolDefinition

        entries = list(candidate.get("entries") or [])
        hints = candidate.get("entry_hints") if isinstance(candidate.get("entry_hints"), dict) else {}
        name = str(candidate.get("name") or candidate.get("plugin_id") or "")
        description = str(candidate.get("description") or "").strip()
        tier_hint = "所有人都可以用" if tier == TIER_ALL else "只有管理员可以用"
        lines = [
            f"调用本体的「{name}」插件（{candidate.get('plugin_id')}，{tier_hint}）。",
        ]
        if description:
            lines.append(description)
        lines.append("可用的 entry_id：")
        for entry_id in entries:
            hint = str(hints.get(entry_id) or "").strip()
            lines.append(f"  - {entry_id}：{hint}" if hint else f"  - {entry_id}")
        lines.append("params 是一个 JSON 对象，内容按该 entry 自己的入参填。")
        return ToolDefinition(
            name=self.tool_name(candidate["plugin_id"]),
            description="\n".join(lines),
            parameters={
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "enum": entries,
                        "description": "要调用的功能 id（必须取自上面列出的那些）",
                    },
                    "params": {
                        "type": "object",
                        "description": "传给该 entry 的参数对象；没有参数就留空对象",
                    },
                },
                "required": ["entry_id"],
            },
            metadata={"source": "qq_auto_reply.plugin_tool_bridge"},
        )

    def build_handler(
        self,
        mounted: list[tuple[dict[str, Any], str]],
        *,
        session_key: str = "",
    ) -> Callable[[Any], Any]:
        """这一轮的分发器：把模型的工具调用转成 `call_entry`。

        entry 只认**这一轮挂上去的那份清单**（模型不能自己编一个 entry 名来透传），
        这是这条链路唯一的安全边界之一。
        """
        from main_logic.tool_calling import ToolResult

        allowed: dict[str, set[str]] = {
            self.tool_name(row["plugin_id"]): set(row.get("entries") or [])
            for row, _tier in mounted
        }
        #: 每个工具对应的"查异步任务"的 entry（同一插件里 id 含 status 的那个）。
        pollers: dict[str, str] = {}
        for row, _tier in mounted:
            poller = self._find_poller_entry(row)
            if poller:
                pollers[self.tool_name(row["plugin_id"])] = poller

        async def _handle(tool_call: Any) -> Any:
            call_id = getattr(tool_call, "call_id", "") or ""
            name = str(getattr(tool_call, "name", "") or "")
            entries = allowed.get(name)
            if entries is None:
                return ToolResult(call_id=call_id, name=name, output="这个工具本轮没有挂载。")
            arguments = getattr(tool_call, "arguments", None) or {}
            if not isinstance(arguments, dict):
                arguments = {}
            entry_id = str(arguments.get("entry_id") or "").strip()
            if entry_id not in entries:
                return ToolResult(
                    call_id=call_id, name=name,
                    output=(
                        f"entry_id {entry_id!r} 不在可用列表里"
                        f"（可用：{'、'.join(sorted(entries))}）。"
                    ),
                )
            params = arguments.get("params")
            if not isinstance(params, dict):
                params = {}
            plugin_id = name[len(_TOOL_PREFIX):]
            output, payload = await self.call_plugin_entry_with_payload(
                plugin_id, entry_id, params, session_key=session_key,
            )
            output += self._async_followup_note(
                payload, plugin_id=plugin_id, poller=pollers.get(name, ""),
            )
            return ToolResult(call_id=call_id, name=name, output=output)

        return _handle

    @staticmethod
    def _find_poller_entry(candidate: dict[str, Any]) -> str:
        """同一插件里"按 task_id 查状态"的那个 entry（有就用它引导模型去查）。"""
        for entry_id in candidate.get("entries") or []:
            lowered = str(entry_id).lower()
            if "status" in lowered or lowered.startswith("get_") or "query" in lowered:
                return str(entry_id)
        return ""

    @staticmethod
    def _find_task_id(payload: Any) -> tuple[str, str]:
        """从结果里认出"这是个异步任务"：返回 ``(字段名, 值)``。"""
        if not isinstance(payload, dict):
            return "", ""
        for key, value in payload.items():
            name = str(key or "")
            if name == "task_id" or name.endswith("_task_id"):
                text = str(value or "").strip()
                if text:
                    return name, text
        return "", ""

    def _async_followup_note(self, payload: Any, *, plugin_id: str, poller: str) -> str:
        """异步任务要**当场把"下一步"告诉模型**。

        真机教训（`writer_power_analysis:analyze_text`）：结果里只有
        `{"task_id": …, "status": "queued"}`，模型于是对用户说"等结果出来我第一时间
        告诉你"—— 而**这条承诺根本没法兑现**：插件会话一轮只走一次工具轮，
        没有任何东西会再回来喂结果。所以这里明写：本轮没有结果、不要承诺主动通知、
        对方再问时用哪个 entry 带哪个字段去查。
        """
        field, value = self._find_task_id(payload)
        if not field:
            return ""
        where = f"{plugin_id}:{poller}" if poller else f"{plugin_id} 里查状态的那个 entry"
        return (
            "\n（这是**异步任务**，本轮拿不到结果。不要承诺「结果出来我主动告诉你」——"
            f"你没有办法再回来喂结果。请让对方稍后再问一次，那时用 {where} 带上 "
            f"{field}={value} 去查。）"
        )

    async def call_plugin_entry(
        self, plugin_id: str, entry_id: str, params: dict[str, Any], *, session_key: str = "",
    ) -> str:
        """跨插件调用，返回给模型看的短文本。"""
        text, _payload = await self.call_plugin_entry_with_payload(
            plugin_id, entry_id, params, session_key=session_key,
        )
        return text

    async def call_plugin_entry_with_payload(
        self, plugin_id: str, entry_id: str, params: dict[str, Any], *, session_key: str = "",
    ) -> tuple[str, Any]:
        """同上，但把**原始结果**一起给出来（分发器要靠它认出异步任务的 task_id）。"""
        plugins = getattr(self.plugin, "plugins", None)
        call_entry = getattr(plugins, "call_entry", None)
        if not callable(call_entry):
            return "当前宿主没有提供跨插件调用接口。", None
        target = f"{plugin_id}:{entry_id}"
        try:
            result = await call_entry(target, params, timeout=CALL_TIMEOUT_SECONDS)
        except Exception as exc:
            self.plugin.logger.warning(f"调用插件 {target} 失败: {exc}")
            return f"调用 {target} 失败：{type(exc).__name__}", None
        is_err = getattr(result, "is_err", None)
        failed = bool(callable(is_err) and is_err())
        self.plugin._emit_log(
            "INFO",
            f"[PluginTool] {target} -> {'err' if failed else 'ok'}"
            + (f" (会话 {session_key})" if session_key else ""),
        )
        if failed:
            error = getattr(result, "error", result)
            return self.render_result(str(error), ok=False), None
        payload = getattr(result, "value", result)
        return self.render_result(payload, ok=True), payload

    @staticmethod
    def render_result(payload: Any, *, ok: bool) -> str:
        """结果 → 模型可读文本（带长度上限，绝不把长文整段塞回去）。"""
        if isinstance(payload, str):
            text = payload
        else:
            try:
                text = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                text = str(payload)
        text = str(text or "").strip()
        if not text:
            text = "（插件没有返回内容）" if ok else "（调用失败，没有更多信息）"
        if not ok:
            text = "调用失败：" + text
        if len(text) > RESULT_MAX_CHARS:
            text = text[:RESULT_MAX_CHARS] + f"…（结果过长，已截断 {len(text) - RESULT_MAX_CHARS} 字）"
        return text
