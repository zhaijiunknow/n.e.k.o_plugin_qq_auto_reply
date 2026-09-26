"""注意力门控服务 — 基于多群注意力竞争的消息分发决策

职责：
1. 每条群消息到达时，更新该群注意力、判定是否回复
2. 检测焦点群切换，触发回溯补回流程
3. 回溯补回：摘要 → LLM 挑选需回复的消息 → 逐条补回
4. 全局休眠判定

底层依赖 QQAttentionService 提供注意力分数、衰减、focus 判定。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .feedback_classifier import QQFeedbackClassifier
from .pipeline_models import backlog_sender_label
from .reply_necessity import DEFAULT_TRIGGER_SCORE, GroupSpeechTracker, IdleBackoff, NecessitySignals, score_necessity


class GateDecision:
    """门控决策结果"""
    __slots__ = ("action", "reason", "force_reply")

    def __init__(self, action: str, reason: str = "", force_reply: bool = False):
        self.action = action      # "reply" | "ignore"
        self.reason = reason
        self.force_reply = force_reply


class FocusShiftResult:
    """焦点切换结果"""
    __slots__ = ("previous_focus_group", "new_focus_group", "triggered_at")

    def __init__(self, previous_focus_group: str = "", new_focus_group: str = "", triggered_at: int = 0):
        self.previous_focus_group = previous_focus_group
        self.new_focus_group = new_focus_group
        self.triggered_at = triggered_at


class QQAttentionGateService:
    """基于注意力的多群门控 + 回溯补回（含疲劳睡眠）"""

    async def start_proactive_loop(self) -> None:
        """保留接口兼容性——破冰由焦点切换冷场计数触发。"""
        pass

    async def stop_proactive_loop(self) -> None:
        """保留接口兼容性。"""
        pass

    def _touch_group(self, group_id: str) -> None:
        """保留接口兼容性——冷场检测已改为焦点切换计数。"""
        pass

    def _mark_active(self, group_id: str) -> None:
        """保留接口兼容性——原先只用于更新疲劳计时，疲劳系统已删除。"""
        pass

    def _necessity_threshold(self) -> float:
        """阈值来自设置（``reply_necessity_threshold``）；0 = 关闭这一关。

        ⚠️ 不能用 ``or`` 兜底：配置成 0 是「关闭」的合法值，``0 or 默认`` 会把它吃掉。
        """
        raw = (self.plugin._qq_settings or {}).get("reply_necessity_threshold", DEFAULT_TRIGGER_SCORE)
        return DEFAULT_TRIGGER_SCORE if raw is None else max(0.0, float(raw))

    def _evaluate_necessity(
        self, *, group_id: str, sender_id: str, message_text: str, is_reply_to_bot: bool, now: float,
    ):
        """组装信号并打分。信号全部来自本进程已有的状态，不额外查库。"""
        threshold = self._necessity_threshold()
        pending = self._speech.pending_count(group_id, now=now)
        attention = self.plugin.attention_service
        frequency = 1.0
        if attention:
            try:
                frequency = min(1.0, float(attention.get_group_multiplier(group_id) or 1.0))
            except Exception:
                frequency = 1.0
        signals = NecessitySignals(
            message_text=message_text,
            is_group=True,
            focus_active=True,
            is_reply_to_bot=is_reply_to_bot,
            pending_count=max(1, pending),
            pending_threshold=self._pending_threshold(),
            self_ratio=self._speech.self_ratio(group_id, now=now),
            idle_reached_average=self._speech.last_gap_seconds(group_id, now=now) >= 30.0,
        )
        return score_necessity(signals, threshold=threshold, frequency=frequency)

    def _pending_threshold(self) -> int:
        """积压压力的参照条数。跟着缓冲上限走：缓冲越容易合并，参照越高。"""
        value = (self.plugin._qq_settings or {}).get("buffer_max_count")
        base = 17 if value is None else max(1, int(value))
        return max(1, min(10, base - 1))

    # ── 冷场破冰：焦点反复落到同一群但无人发言时触发 ──

    _DEFAULT_PROACTIVE_TOPICS = [
        "群聊已经安静了一段时间，你可以主动在群里说点什么来活跃气氛。分享一个想法、提一个有趣的问题、或者聊聊你最近经历的事。注意保持自然，不要像系统消息一样说话。",
        "群里好像冷场了，你可以随便聊点轻松的——比如最近看到的有趣的事、一个冷知识、或者问问大家最近都在忙什么。",
        "你是这个群的活跃分子，看到没人说话，可以抛出一个话题暖暖场。不用很正式，像朋友闲聊一样自然开头就好。",
    ]

    def _pick_proactive_topic(self) -> str:
        """从用户配置的 proactive_topics 中随机选一个，避免连续重复。"""
        import random as _random
        topics = list((self.plugin._qq_settings or {}).get("proactive_topics") or [])
        if not topics:
            topics = list(self._DEFAULT_PROACTIVE_TOPICS)
        if not topics:
            return ""
        topic = _random.choice(topics)
        if len(topics) > 1:
            last = getattr(self, "_last_proactive_topic_idx", -1)
            tries = 0
            while topics.index(topic) == last and tries < 10:
                topic = _random.choice(topics)
                tries += 1
        self._last_proactive_topic_idx = topics.index(topic)
        return topic

    async def _try_icebreaker(self, group_id: str) -> bool:
        """焦点反复切到此群但无人发言 → 用主动话题破冰。"""
        # 有缓冲回复待交付时跳过
        if getattr(self.plugin, "reply_buffer_service", None):
            gkey = self.plugin._build_session_key(sender_id=group_id, is_group=True, group_id=group_id)
            if self.plugin.reply_buffer_service.has_pending(gkey):
                self._logger.info("[Icebreaker] 群有缓冲回复待交付，跳过")
                return False
        topic = self._pick_proactive_topic()
        if not topic:
            return False
        self._logger.info(f"[Icebreaker] 群 {group_id} 尝试破冰话题: {topic[:40]}")
        try:
            from .pipeline_models import QQReplyRequest
            request = QQReplyRequest(
                message_text=f"[系统] {topic}",
                sender_id=self.plugin._admin_qq or "0",
                is_group=True,
                group_id=group_id,
                is_at_bot=True,
                source_kind="proactive_speech",
                group_scene_mode="group_collective",
                fallback_to_text_on_voice_failure=True,
                use_memory_context=False,
                ephemeral_session=False,
            )
            async def _run_icebreaker():
                svc = self.plugin.session_memory_service
                before = svc.session_history_len(f"group:{group_id}")
                try:
                    return await self.plugin.reply_pipeline.run(request)
                finally:
                    svc.record_synthetic_prompt_rows(f"group:{group_id}", before)
            outcome = await self.plugin._run_with_session_lock(
                f"group:{group_id}", _run_icebreaker,
            )
            if outcome.action == "reply" and outcome.reply_text:
                self._logger.info(f"[Icebreaker] 破冰消息已发送: {outcome.reply_text[:50]}...")
                self.plugin.runtime_service.record_pipeline_outcome(
                    source="proactive_speech", request=request, outcome=outcome,
                )
                # 更新活跃时间并持久化，防止重复触发
                attn = getattr(self.plugin, "attention_service", None)
                if attn:
                    state = attn.get_state(group_id)
                    state.last_reply_at = attn._current_time()
                    attn._write_state(state)
                return True
            else:
                self._logger.info("[Icebreaker] AI 决定不回应破冰话题")
        except Exception:
            self._logger.warning("[Icebreaker] 破冰话题发送失败", exc_info=True)
        return False

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._last_focus_group: str = ""
        self._focus_shifting: bool = False
        self._retroactive_lock = asyncio.Lock()
        self._digest_tasks: set[asyncio.Task] = set()
        self._cold_focus_count: dict[str, int] = {}  # 群 → 连续冷场切换次数
        self._reply_timestamps: dict[str, list[int]] = {}  # 群 → 最近回复时间戳列表
        # 「这句该不该接」的两个状态机（内存态，重启即失）
        self._speech = GroupSpeechTracker()
        self._backoff = IdleBackoff()
        self._logger = plugin.logger

    def _check_reply_burst(self, group_id: str, now: int) -> bool:
        """检查最近是否回复过于频繁：60秒内超过3条 → 强制静默。"""
        timestamps = self._reply_timestamps.get(group_id, [])
        settings = self.plugin._qq_settings or {}
        window = max(1, int(settings.get("reply_burst_window_seconds", 60) or 60))
        max_replies = max(1, int(settings.get("reply_burst_max_replies", 3) or 3))
        # 清理过期记录
        timestamps[:] = [t for t in timestamps if now - t < window]
        return len(timestamps) >= max_replies

    def _record_reply(self, group_id: str, now: int) -> None:
        """记录一次回复时间戳。"""
        ts = self._reply_timestamps.setdefault(group_id, [])
        ts.append(now)
        # 只保留最近 10 条
        if len(ts) > 10:
            ts[:] = ts[-10:]

    # ==========================================
    # 消息评估
    # ==========================================

    #: 「这句该不该接」的状态：近期发言窗口（积压 + 存在感）与空闲退避。
    #: 内存态、重启即失 —— 与调研里各家的同类状态一致（见 docs/GROUP-CHAT-RESPONSE-MECHANISMS.md）。
    _speech: GroupSpeechTracker
    _backoff: IdleBackoff

    async def evaluate(
        self,
        *,
        group_id: str,
        sender_id: str,
        is_at_bot: bool = False,
        message_text: str = "",
        message_id: str = "",
        quoted_message_id: str = "",
        sender_nickname: str = "",
        timestamp: int = 0,
        is_reply_to_bot: bool = False,
    ) -> GateDecision:
        """评估群聊消息：先更新注意力，再做焦点门控，输出跳过原因。

        门控规则（焦点前置）：
        - @bot 直接点名 → 唯一旁路，任何群都强制回复
        - 其余消息（含关键词、回复猫娘的消息）→ 非焦点群一律 block，
          不生成回复，但注意力照常累计，并输出跳过原因
        """
        # 无需注意力的连接（如 QQ 开放平台）：直接回复
        if self.plugin.qq_client and not self.plugin.qq_client.needs_attention:
            return GateDecision("reply", reason="no_attention_needed", force_reply=is_at_bot)

        attention = self.plugin.attention_service
        if not attention or not attention._enabled():
            if self.plugin.permission_mgr and self.plugin.permission_mgr.get_permission_level(sender_id) == "admin":
                return GateDecision("reply", reason="admin_priority")
            if is_at_bot:
                return GateDecision("reply", reason="at_bot_fallback")
            self.plugin._emit_log("INFO", f"[Gate] 群{group_id} 忽略: 注意力未启用")
            return GateDecision("ignore", reason="attention_disabled")

        normalized_group_id = str(group_id or "").strip()

        # 0. 记录消息时间（用于主动发言检测）
        self._touch_group(normalized_group_id)

        # 0.5 焦点门控前置：先捕获「接收时焦点」再更新注意力。
        #    若在 update_on_message() 之后再取焦点，当前群刚被 boost 过，
        #    一个接收前非焦点的群可能因此在步骤 1 变成焦点，同一条非 @ 消息
        #    会被放行进 LLM 而非返回 non_focus——破坏焦点优先规则。
        focus_group = attention.get_focus_group()

        # 1. 消息更新注意力（非焦点群也要累计，等待成为焦点）
        await attention.update_on_message({
            "group_id": normalized_group_id,
            "user_id": sender_id,
            "content": message_text,
            "message_id": message_id,
            "timestamp": timestamp or attention._current_time(),
            "is_at_bot": is_at_bot,
        })

        # 连接层 is_reply_to_bot 优先（含 API 兜底），弱链缓存重算仅作兜底
        is_reply_to_bot = is_reply_to_bot or bool(
            quoted_message_id and self.plugin.qq_client
            and quoted_message_id in getattr(self.plugin.qq_client, "sent_message_ids", {})
        )

        # 1.5 记录到「近期发言窗口」：积压压力与存在感惩罚都吃它。
        self._speech.record(normalized_group_id, now=float(timestamp or attention._current_time()), speaker=sender_id)

        # 2. @bot 且非回复猫娘 → 必定回复（抢焦点 + 注意力 boost）——唯一焦点旁路。
        #    消息同时带「@」和「回复」时按回复处理，走焦点门控（用户确认）。
        if is_at_bot and not is_reply_to_bot:
            # 被 @ = **锁**：期内该群独占焦点，其余群不参与竞争。
            # mark_focus / wake_boost 仍保留（它们管分数与保持线），但独占语义由
            # lock 承担 —— 分数是「我多想聊这个群」，锁是「有人点名叫我」。
            # 见 docs/attention-redesign-draft.md §2「锁与归属是两条并行规则」。
            attention.lock_group(normalized_group_id)
            attention.mark_focus(normalized_group_id)
            attention.wake_boost(normalized_group_id)
            self._backoff.reset(normalized_group_id)   # 被点名 = 她必须回来，退避作废
            return GateDecision("reply", reason="at_bot", force_reply=True)

        # 3. 黑名单 → 不处理
        label_defs = list((self.plugin._qq_settings or {}).get("backlog_labels") or [])
        if QQFeedbackClassifier.is_blacklisted(message_text, label_defs):
            return GateDecision("ignore", reason="blacklist")

        # 4. 焦点门控前置：非焦点群 → block（注意力已在步骤 1 累计），
        #    输出跳过原因。关键词/回复猫娘的消息同样在此被拦下。
        current_score = float(attention.get_state(normalized_group_id).attention_score)
        if focus_group != normalized_group_id:
            self.plugin._emit_log(
                "INFO",
                f"[Gate] 群{normalized_group_id} block: 非焦点群 (focus={focus_group or '无'}, score={current_score:.1f})",
            )
            return GateDecision("ignore", reason=f"non_focus(focus={focus_group or '无'},score={current_score:.1f})")

        # 5. 焦点群：检查注意力是否足够——用「焦点保持线」（低于焦点线）而非
        #    焦点线本身。焦点线是赢得焦点的资格线；发送门控若也用焦点线，焦点
        #    群回一条就跌破线、立刻被门控。低于焦点保持线（默认 2.0）才算过低。
        min_threshold = attention._focus_send_threshold()
        if current_score < min_threshold:
            self.plugin._emit_log("INFO", f"[Gate] 焦点群{normalized_group_id} 注意力过低({current_score:.1f}<{min_threshold}), 忽略")
            return GateDecision("ignore", reason=f"focus_low_attention({current_score:.1f})")

        # 6. 焦点群：关键词 → 必定回复（抢焦点 + 注意力 boost）
        category = QQFeedbackClassifier.classify(message_text, label_defs)
        if category == "mention" and not is_at_bot:
            category = "chat"
        if category and category != "chat":
            attention.mark_focus(normalized_group_id)
            attention.wake_boost(normalized_group_id)
            self._backoff.reset(normalized_group_id)
            return GateDecision("reply", reason=f"keyword:{category}", force_reply=True)

        # 7. 焦点群：回复 bot 的消息 → 等同于被点名，强制回复
        if is_reply_to_bot:
            attention.mark_focus(normalized_group_id)
            attention.wake_boost(normalized_group_id)
            self._backoff.reset(normalized_group_id)
            return GateDecision("reply", reason="reply_to_bot", force_reply=True)

        # 8. 焦点群：回复频率门控：60秒内超过3条回复 → 强制静默
        now_ts = attention._current_time()
        if not is_at_bot and self._check_reply_burst(normalized_group_id, now_ts):
            self.plugin._emit_log("INFO", f"[Gate] 群{normalized_group_id} 回复过于频繁，强制静默")
            return GateDecision("ignore", reason="reply_burst_limit")

        # 8.5 「这句到底该不该接」——只作用于 trusted 群。
        #     normal 群不在这里拦：它们本来就不回复，只按概率转发给主人；
        #     若在这里返回 ignore，转发也会被 dispatcher 一起跳过（那是功能回退）。
        group_level = ""
        permission_mgr = getattr(self.plugin, "group_permission_mgr", None)
        if permission_mgr:
            group_level = str(permission_mgr.get_group_level(normalized_group_id) or "")
        if group_level == "trusted":
            now_ts = float(timestamp or attention._current_time())
            # 先看空闲退避：连续「决定不接」之后她会主动从这个群退开一段时间
            # （指数放长到 300s）。积压到 BYPASS_PENDING 条则绕过退避重新评估——
            # 否则群聊热起来她会因为退避而错过。@ / 引用她已在上面短路，不受退避影响。
            pending_now = self._speech.pending_count(normalized_group_id, now=now_ts)
            delay = self._backoff.delay_seconds(normalized_group_id, now=now_ts, pending_count=pending_now)
            if delay > 0:
                self.plugin._emit_log(
                    "INFO",
                    f"[Gate] 群{normalized_group_id} 空闲退避中（剩余 {delay:.0f}s，积压 {pending_now}）",
                )
                return GateDecision("ignore", reason=f"necessity_backoff({delay:.0f}s)")
            verdict = self._evaluate_necessity(
                group_id=normalized_group_id,
                sender_id=sender_id,
                message_text=message_text,
                is_reply_to_bot=is_reply_to_bot,
                now=now_ts,
            )
            if verdict.decision != "trigger":
                backoff = self._backoff.record_wait(normalized_group_id, now=now_ts)
                self.plugin._emit_log(
                    "INFO",
                    f"[Gate] 群{normalized_group_id} 必要性不足，本轮不接 ({verdict.reason}, 退避 {backoff:.0f}s)",
                )
                return GateDecision("ignore", reason=verdict.reason)
            self._backoff.reset(normalized_group_id)

        # 9. 焦点群普通消息 → LLM 自行判断是否回复
        self._mark_active(normalized_group_id)
        self.plugin._emit_log("INFO", f"[Attention] 焦点群 {normalized_group_id} 消息, LLM自行判断是否回复")
        return GateDecision("reply", reason="focus_group")

    # ==========================================
    # 回复后消耗 + 焦点切换检测
    # ==========================================

    async def on_reply_sent(self, group_id: str) -> None:
        """回复已发送 → 消耗注意力 + 记录活跃 + 频率计数"""
        attention = self.plugin.attention_service
        if attention:
            now = attention._current_time()
            await attention.update_on_reply(group_id)
        else:
            now = int(__import__("time").time())
        self._mark_active(group_id)
        self._record_reply(str(group_id or "").strip(), now)
        # 她说话了 → 存进「近期发言窗口」（存在感惩罚的来源），并清掉空闲退避
        self._speech.record_self(group_id, now=float(now))
        self._backoff.reset(group_id)

    async def check_focus_shift(self) -> FocusShiftResult | None:
        """检测焦点群是否切换"""
        attention = self.plugin.attention_service
        if not attention:
            return None

        new_focus = attention.get_focus_group()
        previous = self._last_focus_group
        if new_focus and new_focus != previous:
            self._last_focus_group = new_focus
            self._logger.info(f"[AttentionGate] 焦点切换: {previous or '无'} → {new_focus}")
            if previous:
                digest_task = asyncio.create_task(self._push_group_digest(previous))
                self._digest_tasks.add(digest_task)
                self.plugin._group_digest_task = digest_task

                def _clear_digest_task(done_task: asyncio.Task) -> None:
                    self._digest_tasks.discard(done_task)
                    if self.plugin._group_digest_task is done_task:
                        self.plugin._group_digest_task = None

                digest_task.add_done_callback(_clear_digest_task)
            return FocusShiftResult(
                previous_focus_group=previous or "",
                new_focus_group=new_focus,
                triggered_at=attention._current_time(),
            )
        if previous and not new_focus:
            # 全局休眠
            self._last_focus_group = ""
            self._logger.info("[AttentionGate] 全局休眠：所有群注意力过低")
        return None

    # ==========================================
    # 回溯补回流程
    # ==========================================

    async def run_retroactive_review(self, group_id: str) -> list[str]:
        """焦点切换到 group_id 后，对忽略消息做回溯补回"""
        async with self._retroactive_lock:
            return await self._run_retroactive_review_locked(group_id)

    async def _run_retroactive_review_locked(self, group_id: str) -> list[str]:
        attention = self.plugin.attention_service
        if not attention:
            return []

        # 1. 从统一 backlog_store 取出上次 focus 以来的未审核消息
        since = attention.get_last_focus_at(group_id)
        if not hasattr(self.plugin, "backlog_store") or not self.plugin.backlog_store:
            self._logger.warning("[RetroReview] backlog_store 不可用，跳过回溯")
            return []
        max_messages = int((self.plugin._qq_settings or {}).get("retroactive_review_max_messages", 30) or 30)
        # 这个键此前是**死键**：默认值/保存/校验/界面全都有，提示词里却写死了"1-2 条"。
        max_reply = max(1, int((self.plugin._qq_settings or {}).get("retroactive_review_max_reply", 5) or 5))
        unreviewed = await self.plugin.backlog_store.get_unreviewed_messages_since(group_id, since_timestamp=since, limit=max_messages)
        if not unreviewed:
            self._logger.info(f"[RetroReview] 群 {group_id} 无未审核消息，跳过回溯")
            count = self._cold_focus_count.get(group_id, 0) + 1
            self._cold_focus_count[group_id] = count
            threshold = int((self.plugin._qq_settings or {}).get("icebreaker_cold_threshold", 3) or 3)
            if threshold > 0 and count >= threshold:
                self._logger.info(f"[Icebreaker] 群 {group_id} 连续 {count} 次冷场切换，尝试破冰")
                await self._try_icebreaker(group_id)
                self._cold_focus_count[group_id] = 0
            try:
                await self.plugin.backlog_service.mark_group_reviewed_payload(group_id)
            except Exception:
                pass
            return []

        # 有未审消息 → 重置冷场计数
        self._cold_focus_count.pop(group_id, None)
        self._logger.info(f"[RetroReview] 群 {group_id} 有 {len(unreviewed)} 条未审核消息，开始回溯")
        # 本次**真正喂给模型**的消息就是"已审"的边界。标记必须收窄到这个集合，
        # 不能整群全标：模型只看得到最新 max_messages 条，超窗的旧消息若被一起
        # 标成已审，就永远不会被补回——用户既没看到、也再没有机会看到。
        reviewed_ids = {
            str(item.get("message_id") or "").strip()
            for item in unreviewed
            if str(item.get("message_id") or "").strip()
        }

        # 2. 复用缓冲链路：构造总结 prompt，让猫娘挑最多 max_reply 条用 <reply> 回应
        summary = self._build_ignored_summary(unreviewed)
        try:
            from .pipeline_models import QQReplyRequest
            request = QQReplyRequest(
                message_text=(
                    f"[系统] 你刚才没有太关注这个群，以下是这段时间群友们聊天的消息摘要。\n"
                    f"每条消息末尾都标了它的消息ID（形如 id=xxx）。请针对其中最多 {max_reply} 条你最感兴趣的，"
                    f"用 `<reply>消息ID</reply>` 引用后自然回应。不要逐条点评。\n\n"
                    f"摘要：\n{summary}"
                ),
                sender_id=self.plugin._admin_qq or "0",
                is_group=True,
                group_id=group_id,
                is_at_bot=True,
                source_kind="retroactive_review",
                group_scene_mode="group_collective",
                fallback_to_text_on_voice_failure=True,
                use_memory_context=False,
                ephemeral_session=False,
            )
            async def _run_retro():
                svc = self.plugin.session_memory_service
                before = svc.session_history_len(f"group:{group_id}")
                try:
                    return await self.plugin.reply_pipeline.run(request)
                finally:
                    svc.record_synthetic_prompt_rows(f"group:{group_id}", before)
            outcome = await self.plugin._run_with_session_lock(
                f"group:{group_id}", _run_retro,
            )
            self.plugin.runtime_service.record_pipeline_outcome(
                source=request.source_kind, request=request, outcome=outcome,
            )
            if outcome.action == "reply" and outcome.reply_text:
                self._logger.info(f"[RetroReview] 回溯回复已发送: {outcome.reply_text[:50]}...")
            else:
                self._logger.info("[RetroReview] LLM 决定不回复回溯摘要")
        except Exception as e:
            self._logger.warning(f"[RetroReview] 回溯总结失败: {e}")

        # 3. 标记已读（只标本次消费掉的那批，见上）
        attention.mark_focus(group_id)
        try:
            await self.plugin.backlog_service.mark_group_reviewed_payload(
                group_id, message_ids=reviewed_ids,
            )
            self._logger.info(f"[RetroReview] 群 {group_id} 已标记 {len(reviewed_ids)} 条为已审阅")
        except Exception as e:
            self._logger.warning(f"[RetroReview] 标记已审阅失败: {e}")
        return []

    # ==========================================
    # 回溯辅助方法
    # ==========================================

    @staticmethod
    def _build_ignored_summary(messages: list[dict[str, Any]]) -> str:
        """把被忽略的消息列表生成 LLM 可读的摘要，每条消息后携带 (id=消息ID) 供 <reply> 引用。"""
        lines: list[str] = []
        for i, msg in enumerate(messages, 1):
            # 发言人显示名收口进 `backlog_sender_label`：backlog 落盘的键是
            # `sender_name`，这里原先硬编码 `sender_nickname` —— 对 backlog 记录
            # 永远取不到，于是**回溯补回的摘要里显示的全是 QQ 号**而不是昵称。
            nickname = backlog_sender_label(msg, default="未知")
            # backlog 存储项来自 QQBacklogMessage.to_dict()，内容键是 text；
            # message_text 是旧键名（无历史数据），保留作兜底。
            text = str(msg.get("text") or msg.get("message_text") or "").strip()
            if len(text) > 100:
                text = text[:97] + "..."
            msg_id = str(msg.get("message_id") or "").strip()
            lines.append(f"[{i}] {nickname}: {text} (id={msg_id})" if msg_id else f"[{i}] {nickname}: {text}")
        return "\n".join(lines)

    async def _push_group_digest(self, group_id: str) -> None:
        """焦点切换时将旧焦点群的完整会话摘要推送到 Memory Server"""
        try:
            if not bool((getattr(self.plugin, "_qq_settings", {}) or {}).get(
                "group_memory_enabled", False,
            )):
                return
            session_key = f"group:{group_id}"
            sessions = getattr(self.plugin, "_user_sessions", {}) or {}
            s = sessions.get(session_key)
            if not isinstance(s, dict):
                return
            async def _push_delta() -> int:
                # 锁内复检：外层 setting 检查通过后可能排队等锁，期间用户
                # 关掉群记忆——opt-out 之后不得再推送 digest。
                if not bool((getattr(self.plugin, "_qq_settings", {}) or {}).get(
                    "group_memory_enabled", False,
                )):
                    return 0
                if (getattr(self.plugin, "_user_sessions", {}) or {}).get(
                    session_key
                ) is not s:
                    # 等锁期间 finalizer/discard 可能已结算并弹出会话：
                    # 陈旧引用继续推会重发已结算历史、推进无主游标。
                    return 0
                if s.get("pending_disable_settle"):
                    # opt-out 结算未完成（快速 re-enable 会让上面的 setting
                    # 检查重新通过）：digest 不碰，交转变任务按 cutoff 结算。
                    return 0
                if s.get("pending_enable_rebase") is not None:
                    # retain 结算后、ON rebase 前的 limbo：游标还停在
                    # opt-out 区间之前，此处推送只剩 nonconsent floor 一道
                    # 防线兜着未授权行。交 rebase 任务先把游标规整过界。
                    return 0
                session = s.get("session")
                if not session or not hasattr(session, "_conversation_history"):
                    return 0
                history = getattr(session, "_conversation_history", []) or []
                if len(history) < 4:
                    return 0
                # 先旧后新分批 + 精确游标（对偶 finalize 的同名修复）：旧写法
                # `[-200:]` 会把超窗中段永久跳过、游标却跳到 len(history)，
                # 之后 finalize 也无从补救。失败即停，游标停在最后一个成功
                # 批，剩余留给下一次 digest/finalize。
                svc = self.plugin.session_memory_service
                start_index = max(0, int(s.get("last_group_digest_index", 0)))
                start_index = max(
                    start_index,
                    int(s.get("nonconsent_history_end", 0) or 0),
                )
                if start_index > len(history):
                    # 历史被重复守卫重置/收缩：钳游标，防新增轮次被
                    # 永久跳过（对偶 finalize 的同名钳制）。
                    start_index = len(history)
                    s["last_group_digest_index"] = start_index
                total_sent = 0
                # 限批：focus-shift 推送持有会话锁，慢 memory server 下
                # 无界批次会让 3 个排水群占满全局 Semaphore(3) 冻结全部
                # 消息处理。每次最多 3 批，游标精确，剩余留给下一次
                # digest / finalize（结算 of record 在 finalize，不丢）。
                remaining_batches = 3
                while remaining_batches > 0:
                    remaining_batches -= 1
                    messages, next_index = svc._slice_group_history_batch(
                        history, start_index, svc.GROUP_HISTORY_MAX_MESSAGES,
                        user_data=s, stop_at_provisional=True,
                    )
                    if not messages:
                        if next_index > start_index:
                            s["last_group_digest_index"] = next_index
                        break
                    # 拿不到群名就不带参（对偶 finalize 的 digest 调用）。
                    digest_extra = {}
                    group_display_name = svc._group_display_name(group_id)
                    if group_display_name:
                        digest_extra["display_name"] = group_display_name
                    await self.plugin.memory_bridge.post_scoped_memory_history(
                        str(s.get("her_name") or "neko"),
                        messages,
                        subject=self.plugin.memory_bridge.group_subject(group_id),
                        timeout=30.0,
                        **digest_extra,
                    )
                    s["last_group_digest_index"] = next_index
                    start_index = next_index
                    total_sent += len(messages)
                return total_sent

            sent_messages = await self.plugin._run_with_session_lock(session_key, _push_delta)
            if sent_messages:
                self._logger.info(f"[Digest] 群 {group_id} 已推送摘要到 Memory Server ({sent_messages}条)")
        except Exception as e:
            self._logger.warning(f"[Digest] 推送失败: {e}")

    # ==========================================
    # 公共查询
    # ==========================================

    def is_global_sleep(self) -> bool:
        attention = self.plugin.attention_service
        if not attention:
            return False
        return attention.is_global_sleep()

    def get_focus_group(self) -> str | None:
        attention = self.plugin.attention_service
        if not attention:
            return None
        return attention.get_focus_group()

    # ==========================================
    # 生命周期
    # ==========================================

    async def shutdown(self) -> None:
        self._last_focus_group = ""
        self._focus_shifting = False
        digest_tasks = list(self._digest_tasks)
        for task in digest_tasks:
            if not task.done():
                task.cancel()
        if digest_tasks:
            await asyncio.gather(*digest_tasks, return_exceptions=True)
        self._digest_tasks.clear()
        self.plugin._group_digest_task = None
        if self.plugin.attention_service:
            await self.plugin.attention_service.stop_decay_loop()
        self._cold_focus_count.clear()
        self._logger.info("[AttentionGate] 已关闭")
