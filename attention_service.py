"""注意力服务（周期模型）

每个群一份独立注意力标量（0~10），按相位机推进：

- rise: 随时间增长（基础速率 + 消息/@/提问加成），情绪/疲劳调制速率
- fall: 随时间回落（夺冠蜜月结束、发言消耗、让位回落）

焦点 = 所有 attention >= 焦点线的群中最高者；发言消耗注意力；情绪可抢/让焦点。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .feedback_classifier import QQFeedbackClassifier

# ── 情绪 → 注意力速率偏移（rise 加速 / fall 减速）──
#
# 这张表是情绪的**唯一真源**：倍率符号决定该情绪属于上升侧还是回落侧，
# `_EMOTION_DECAY_ORDER` 必须按倍率单调递减排列（从最猛到最丧，中间是 calm）。
# `tests/test_qq_emotion_vocabulary.py` 强制这三条不变量，并强制它和
# `settings_schema.DEFAULT_EMOTION_MULTIPLIERS` 逐键一致。
_EMOTION_MULTIPLIER: dict[str, float] = {
    "arguing": 1.2,      # 上头死磕，涨得快跌得慢
    "proud": 0.8,        # 赢了要炫耀，猛拉注意力
    "annoyed": 0.5,      # 不爽，比正常更专注
    "playful": 0.3,      # 玩闹中，微微挂住
    "curious": 0.2,      # 被勾起兴趣
    "calm": 0.0,         # 正常
    "sad": -0.4,         # 难过，不太想说话
    "embarrassed": -0.6,  # 尴尬想溜
    "bored": -0.7,       # 没兴趣的话题，主动掉注意力去找别的群
    "sulking": -0.9,     # 赌气——基本清零，主动让出焦点
}
# 强情绪直接触发焦点切换。
#
# ⚠️ 这两个集合与 `_EMOTION_MULTIPLIER` 是**分开维护**的：往倍率表加新情绪不会
# 自动让它获得抢/让焦点行为。新增情绪时必须同时决定它属于哪一档（或都不属于），
# 否则该情绪会「能配置但无行为」。`tests/test_qq_emotion_vocabulary.py` 盯着这条
# 一致性 —— 新情绪若既不在抢焦点也不在让焦点集合里，必须显式登记到该测试的
# `_NEUTRAL_EMOTIONS` 白名单，逼作者做一次有意识的选择。
_EMOTION_FORCE_FOCUS = {"arguing", "proud"}     # 立刻抢焦点
_EMOTION_DROP_FOCUS = {"sulking", "embarrassed", "bored"}  # 立刻让出焦点
# 情绪降温阶梯：30 秒无新情绪就朝 calm 方向走一级。顺序 = 倍率从高到低，
# 于是「上升侧 = calm 之前」「回落侧 = calm 之后」与倍率符号自动一致。
_EMOTION_DECAY_ORDER = [
    "arguing", "proud", "annoyed", "playful", "curious",
    "calm",
    "sad", "embarrassed", "bored", "sulking",
]
_EMOTION_DECAY_SECONDS = 30


@dataclass(slots=True)
class QQGroupAttentionState:
    """单群注意力状态（周期模型）。

    ``attention_score`` 是唯一标量（0~10），按相位机推进：
      - rise: 随时间增长（消息/@ 加速；情绪/疲劳调制速率）
      - fall: 随时间回落（发言消耗、夺冠蜜月结束后自然回落）
    焦点 = 所有 attention_score >= 焦点线的群中最高者。
    """

    group_id: str
    attention_score: float = 0.0      # 唯一标量 0~10
    phase: str = "rise"               # rise | fall
    phase_started_at: int = 0         # 当前相位起始时间（fall 回落计时 / 蜜月判断用）
    # ── 时间戳 ──
    last_decay_at: int = 0            # 上次相位推进时刻（幂等推进用）
    last_message_at: int = 0
    last_reply_at: int = 0
    last_boost_at: int = 0
    last_focus_at: int = 0
    focus_acquired_at: int = 0        # 最近夺冠时刻（夺冠身份，不参与蜜月计时）
    #: 首次「稳住焦点线」的时刻 —— **蜜月计时用它，不用 focus_acquired_at**。
    #:
    #: 两者必须分开：从 0 自然涨到焦点线的群一到线就夺冠，若蜜月也从那一刻起算，
    #: 它还没积累任何余量就被判「蜜月结束」转入 fall，使用者要的「能一直聊很久」
    #: 在结构上不可能。稳线时刻给了夺冠之后一段纯积累期。
    steady_since: int = 0
    #: 锁到期时刻（`@猫娘` / 唤醒词触发）。期内该群独占焦点，其余群不参与竞争。
    #:
    #: 与分数是**两个独立信号**：分数表达「没人叫我时我自己看哪」，
    #: 锁表达「有人点名，我必须回头应对」。合成一个数就会互相污染参数
    #: （这正是本模块此前调不明白的原因，见 docs/attention-redesign-draft.md §4）。
    lock_until: int = 0
    last_focus_reason: str = ""
    total_interactions: int = 0
    # ── 情绪 ──
    emotion: str = "calm"             # 取值见 _EMOTION_MULTIPLIER（唯一真源）
    emotion_updated_at: int = 0
    emotion_display: str = "calm"     # 前端展示用标签，衰减比 logic emotion 慢
    emotion_display_until: int = 0

    def recompute_score(self) -> float:
        """兼容接口：注意力即标量，无需加权计算。"""
        return self.attention_score

    def dimension_dict(self) -> dict[str, float]:
        """展示用：标量 + 相位 + 情绪是否活跃。"""
        return {
            "attention": float(self.attention_score),
            "phase": 1.0 if self.phase == "rise" else 0.0,
            "emotion": 1.0 if self.emotion != "calm" else 0.0,
        }

    def dimension_label(self, key: str) -> str:
        return {"attention": "注意力", "phase": "相位", "emotion": "情绪"}.get(key, key)

    def dominant_dimension(self) -> str:
        """用于解释焦点原因：当前相位。"""
        return self.phase

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "attention_score": float(self.attention_score),
            "phase": str(self.phase),
            "phase_started_at": int(self.phase_started_at),
            "last_decay_at": int(self.last_decay_at),
            "last_message_at": int(self.last_message_at),
            "last_reply_at": int(self.last_reply_at),
            "last_boost_at": int(self.last_boost_at),
            "last_focus_at": int(self.last_focus_at),
            "focus_acquired_at": int(self.focus_acquired_at),
            "steady_since": int(self.steady_since),
            "lock_until": int(self.lock_until),
            "last_focus_reason": str(self.last_focus_reason or ""),
            "total_interactions": int(self.total_interactions),
            "emotion": str(self.emotion or "calm"),
            "emotion_updated_at": int(self.emotion_updated_at),
            "emotion_display": str(self.emotion_display or "calm"),
            "emotion_display_until": int(self.emotion_display_until),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None, *, group_id: str) -> "QQGroupAttentionState":
        data = dict(payload or {})
        st = cls(
            group_id=group_id,
            attention_score=float(data.get("attention_score") or 0.0),
            phase=str(data.get("phase") or "rise"),
            phase_started_at=int(data.get("phase_started_at") or 0),
            last_decay_at=int(data.get("last_decay_at") or 0),
            last_message_at=int(data.get("last_message_at") or 0),
            last_reply_at=int(data.get("last_reply_at") or 0),
            last_boost_at=int(data.get("last_boost_at") or 0),
            last_focus_at=int(data.get("last_focus_at") or 0),
            focus_acquired_at=int(data.get("focus_acquired_at") or data.get("last_focus_at") or 0),
            # 旧存档没有这个键 → 0。回落到 focus_acquired_at 会让老数据沿用旧的
            # 「一到线就开始蜜月」行为，反而把要修的场景重新引入；取 0 表示
            # 「尚未稳线」，下一轮 _advance_phase 会在分数到线时补记。
            steady_since=int(data.get("steady_since") or 0),
            lock_until=int(data.get("lock_until") or 0),
            last_focus_reason=str(data.get("last_focus_reason") or ""),
            total_interactions=int(data.get("total_interactions") or 0),
            emotion=str(data.get("emotion") or "calm"),
            emotion_updated_at=int(data.get("emotion_updated_at") or 0),
            emotion_display=str(data.get("emotion_display") or "calm"),
            emotion_display_until=int(data.get("emotion_display_until") or 0),
        )
        # 旧维度模型迁移：旧 attention_score 是四维加权分，与周期模型标量语义不同——
        # 直接按新模型从当前值开始重新积累，相位默认 rise。
        if not data.get("phase"):
            st.phase = "rise"
        return st


class QQAttentionService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._cache: dict[str, dict[str, Any]] = {}

    async def load_cached_state(self) -> None:
        if not getattr(self.plugin, "backlog_store", None):
            self._cache = {}
            return
        state = await self.plugin.backlog_store.load()
        attention_state = state.get("group_attention_state")
        self._cache = dict(attention_state) if isinstance(attention_state, dict) else {}
        self.cleanup_stale_cache()
        # 重启后重置相位时钟：last_decay_at/phase_started_at 是停机前的旧值，
        # 直接用停机时长推进会把注意力一步推到极端（rise 顶满 / fall 归零）。
        # 重置为当前时刻，让周期从干净状态重新开始。
        now = self._current_time()
        for payload in self._cache.values():
            if isinstance(payload, dict):
                payload["last_decay_at"] = now
                payload["phase_started_at"] = now
                # `steady_since` 是后加的键：旧存档没有它。若不补，那些状态会
                # **永远不进入 fall** —— `_advance_phase` 的回落判定要求它非零，
                # 而它只在「分数从线下站上焦点线」时才被写入，一个本来就高于
                # 焦点线的群永远不会走到那个分支。
                #
                # 迁移策略：分数已在线上的，把稳线时刻定义为**此刻**（重启后的
                # 新起点）。这既给了它一个完整的蜜月积累期，也不会因为沿用旧
                # 时间戳而立刻判 fall。
                if not int(payload.get("steady_since") or 0):
                    try:
                        score = float(payload.get("attention_score") or 0.0)
                    except (TypeError, ValueError):
                        score = 0.0
                    if score >= self._focus_threshold():
                        payload["steady_since"] = now

    def _current_time(self) -> int:
        return int(__import__("time").time())

    def _normalized_groups(self) -> list[str]:
        """只读：返回信任列表 + 缓存中已有的群（不含清理逻辑）。"""
        groups: set[str] = set()
        if self.plugin.group_permission_mgr:
            for item in self.plugin.group_permission_mgr.list_groups():
                gid = item.get("group_id", "") if isinstance(item, dict) else str(item or "")
                normalized = str(gid or "").strip()
                if normalized:
                    groups.add(normalized)
        for group_id in list(self._cache.keys()):
            if isinstance(group_id, str) and group_id.startswith("{"):
                continue
            normalized = str(group_id or "").strip()
            if normalized and not normalized.startswith("{"):
                groups.add(normalized)
        return sorted(groups)

    def cleanup_stale_cache(self) -> int:
        """显式清理不在信任列表中的缓存群，返回清理数。调用时机：load_cached_state / persist。"""
        trust_groups: set[str] = set()
        if self.plugin.group_permission_mgr:
            for item in self.plugin.group_permission_mgr.list_groups():
                gid = item.get("group_id", "") if isinstance(item, dict) else str(item or "")
                normalized = str(gid or "").strip()
                if normalized:
                    trust_groups.add(normalized)
        removed = 0
        for group_id in list(self._cache.keys()):
            if isinstance(group_id, str) and group_id.startswith("{"):
                del self._cache[group_id]
                removed += 1
                continue
            normalized = str(group_id or "").strip()
            if normalized and not normalized.startswith("{") and normalized not in trust_groups:
                del self._cache[group_id]
                removed += 1
        return removed

    def _load_state(self, group_id: str) -> QQGroupAttentionState:
        attention_state = self._cache.get(group_id)
        state = QQGroupAttentionState.from_dict(
            attention_state if isinstance(attention_state, dict) else None, group_id=group_id
        )
        # 新群：启动相位时钟，从 0 开始随时间爬升
        if not isinstance(attention_state, dict):
            now = self._current_time()
            state.last_decay_at = now
            state.phase_started_at = now
        return state

    def get_state(self, group_id: str) -> QQGroupAttentionState:
        return self._load_state(str(group_id or "").strip())

    def _write_state(self, state: QQGroupAttentionState) -> None:
        self._cache[state.group_id] = state.to_dict()

    def _enabled(self) -> bool:
        """注意力账本是否运行。**没有独立开关**。

        以前这里读配置键 ``enable_group_attention``，但它是个**假旋钮**：唯一让它为真
        的模式（``neko_dynamic``，也是出厂默认）下 ``_enforce_attention_for_dynamic_mode``
        会把它强制设回 True；而在 ``neko_scene`` 下门控根本不跑
        （``message_dispatcher`` 只在 neko_dynamic 下调 ``attention_gate_service``）。
        也就是说那个开关"能关的模式里不需要关、需要关的模式里关不着"。

        已删除该配置。账本恒开 —— **是否门控**由调用方按策略模式决定，不在这里。
        （老配置里残留的 ``enable_group_attention`` 键无害：它只是被原样加载与回写。）
        """
        return True

    # ── 周期模型参数（可配）──

    def _setting(self, key: str, default: Any) -> Any:
        """读配置，尊重 0 值：缺失（未设置/None）才回退 default。

        旧写法 ``get(key, default) or default`` 会把保存的 0 当成 falsy 回退
        默认——例如 attention_consume_ratio=0（禁用回复消耗）被读回 0.10，
        attention_fall_rate=0 被读回 0.015，dashboard 报保存成功但运行时
        行为不变。
        """
        value = (self.plugin._qq_settings or {}).get(key)
        return default if value is None else value

    def _rise_rate(self) -> float:
        """rise 相位基础增速（/秒）。"""
        return max(0.0, float(self._setting("attention_base_rise_rate", 0.02)))

    def _message_boost(self) -> float:
        """单条消息对注意力的加成。"""
        return max(0.0, float(self._setting("attention_message_boost", 0.15)))

    def _keyword_boost_ratio(self) -> float:
        """分类命中（mention/关键词）时的额外加成倍率。"""
        return max(0.0, float(self._setting("attention_keyword_boost_ratio", 1.8)))

    def _message_gain(self) -> float:
        """批量消息计数时每条消息的注意力增益。"""
        return max(0.0, float(self._setting("attention_batch_message_gain", 0.25)))

    def _honeymoon_seconds(self) -> int:
        """夺冠后继续上升的蜜月窗口（秒）。"""
        return max(0, int(self._setting("attention_honeymoon_seconds", 60)))

    def _fall_seconds(self) -> int:
        """进入 fall 相位至少持续多久才允许回升（秒）。"""
        return max(0, int(self._setting("attention_fall_seconds", 30)))

    def _fall_rate(self) -> float:
        """fall 相位回落速率（/秒）。"""
        return max(0.0, float(self._setting("attention_fall_rate", 0.015)))

    def _consume_ratio(self) -> float:
        """猫娘每次发言消耗注意力的比例（0~1）。"""
        return min(1.0, max(0.0, float(self._setting("attention_consume_ratio", 0.10))))

    def _max_attention(self) -> float:
        return float(self._setting("attention_max_score", 10.0))

    def _focus_threshold(self) -> float:
        return float(self._setting("attention_focus_threshold", 4.0))

    def _focus_send_threshold(self) -> float:
        """焦点群的发送门控线（默认 2.0）：低于焦点线、高于最低线。

        焦点线（_focus_threshold，默认 4.0）是「赢得焦点」的资格线。一旦成为
        焦点群，注意力会随回复消耗（_consume_ratio）和时间衰减；若发送门控
        也用焦点线，焦点群回一条就跌破线、立刻被门控——焦点形同虚设。这里用
        更低的「焦点保持线」作为发送门控，让焦点群在合理注意力水平上继续回应。
        """
        return float(self._setting("attention_focus_hold_threshold", 2.0))

    def _minimum_threshold(self) -> float:
        return float(self._setting("attention_min_threshold", 1.0))

    def _fall_boost_attenuation(self) -> float:
        """fall 相位里消息加成的衰减系数（0~1）：正在让位的群不会因刷屏而赖着不走。"""
        return min(1.0, max(0.0, float(self._setting("attention_fall_boost_attenuation", 0.3))))

    def _at_bot_boost(self) -> float:
        """被 @ 时消息加成的倍率（最强的一路加成）。"""
        return max(0.0, float(self._setting("attention_at_bot_boost", 3.0)))

    def _question_boost(self) -> float:
        """消息是提问时的加成倍率。"""
        return max(0.0, float(self._setting("attention_question_boost", 1.5)))

    def _wake_boost_ratio(self) -> float:
        """唤醒时把分数垫到「焦点线 × 此比例」（0~1）。"""
        return min(1.0, max(0.0, float(self._setting("attention_wake_boost_ratio", 0.75))))

    def _decay_interval(self) -> float:
        """注意力衰减循环的 tick 间隔（秒）。"""
        return max(0.1, float(self._setting("attention_decay_interval_seconds", 5.0)))

    def _frequency_target_gap(self) -> float:
        """发言频率的目标间隔（秒）：恰好这个节奏时增速为基准 1.0×。"""
        return max(1.0, float(self._setting("attention_frequency_target_gap", 30.0)))

    def _frequency_min_multiplier(self) -> float:
        """冷群增速下限（0~1）。不为 0：冷群该涨得慢，但不该被彻底冻结。"""
        return min(1.0, max(0.0, float(self._setting("attention_frequency_min_multiplier", 0.15))))

    def _frequency_max_multiplier(self) -> float:
        """热群增速上限（>=1）。

        兜底值必须与 ``settings_schema`` 声明的默认值一致（1.8）。这里曾是 3.0 ——
        早先真源默认值也是 3.0，后来降到 1.8 时只改了真源，读取端漏改。生产里
        键恒在（`_qq_settings` 由 `default_config()` 播种）所以线上看不出，
        但**任何只塞部分 settings 的调用方（测试、debug 脚本）拿到的是 3.0**:
        用 `business_config.json` 的真实参数构造出来却是 3.0× 封顶，
        等于在验证产品不使用的行为。
        """
        return max(1.0, float(self._setting("attention_frequency_max_multiplier", 1.8)))

    def _frequency_scale(self, state: QQGroupAttentionState, now: int) -> float:
        """按本群的发言节奏缩放自然增速：``目标间隔 / 实际间隔``，再钳到 [min, max]。

        热群（间隔小于目标）涨得快，冷群（间隔大于目标）涨得慢。只用
        ``last_message_at`` 这一个既有字段算间隔 —— 没有滑动窗口、没有计数器，
        因此无状态、可复现，也不需要额外的老化机制。

        调用点必须保证 ``now`` 早于写入本条消息的 ``last_message_at``
        （``_advance_phase`` 由 ``_apply_decay`` 在 ``update_on_message`` 更新该
        字段之前调用），否则间隔恒为 0、所有群都会拿到上限倍率。
        """
        last = int(state.last_message_at or 0)
        gap = float(now - last) if last > 0 else float("inf")
        if gap <= 0.0:
            # 同一秒内连发（或时钟未前进）：按最热处理。
            return self._frequency_max_multiplier()
        scale = self._frequency_target_gap() / max(gap, 1.0)
        return min(max(scale, self._frequency_min_multiplier()), self._frequency_max_multiplier())

    def _emotion_multipliers(self) -> dict[str, float]:
        """情绪 → 升降速率倍率表（配置表 = **覆盖表**）。

        内置 ``_EMOTION_MULTIPLIER`` 是基准，用户配置只覆盖它写到的键。没写的键
        回落到内置默认值 —— 这一条是必需的，不是宽容：老配置是在旧版本存的快照，
        新版本往默认表加的**新情绪在老配置里没有键**。如果按「表里没有就 0.0」处理，
        新情绪对老用户会彻底静默失效（``set_emotion`` 会直接 return），
        表现为「升级后新情绪标记毫无反应」，而且没有任何日志。

        想关掉某个情绪的影响，请显式写 ``0.0``；删掉键表示「跟随内置默认值」。

        写入侧由 ``config_store.normalize_emotion_multipliers`` 保证合法；这里再做一次
        防御性校验，非法就整份回退内置默认 —— 这张表参与涨跌计算，半份坏数据比没有更难查。
        """
        raw = self._setting("attention_emotion_multipliers", None)
        if not isinstance(raw, dict) or not raw:
            return dict(_EMOTION_MULTIPLIER)
        out: dict[str, float] = dict(_EMOTION_MULTIPLIER)
        for key, value in raw.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                return dict(_EMOTION_MULTIPLIER)
        return out

    def _emotion_multiplier(self, emotion: Any) -> float:
        """单个情绪的倍率；表外情绪按 0.0（即不影响速率）。"""
        return float(self._emotion_multipliers().get(str(emotion or "calm"), 0.0))

    # ── 相位推进 ──

    def _advance_phase(self, state: QQGroupAttentionState, now: int) -> None:
        """按当前相位推进注意力，处理相位切换。幂等：基于 last_decay_at 差分。"""
        last = int(state.last_decay_at or state.last_message_at or state.last_boost_at or now)
        dt = max(0, now - last)
        if dt <= 0:
            return
        state.last_decay_at = now
        emo = self._emotion_multiplier(state.emotion)

        if state.phase == "fall":
            # 回落：正向情绪跌得慢
            rate = self._fall_rate() * max(0.05, 1.0 - emo)
            state.attention_score = max(0.0, state.attention_score - rate * dt)
            # 回落满 T2 → 回升
            if now - state.phase_started_at >= self._fall_seconds():
                state.phase = "rise"
                state.phase_started_at = now
        else:
            # 上升：正向情绪涨得快。
            #
            # 自然增长的上限是 **max_score 而不是焦点线**。
            #
            # 旧写法 `min(focus_threshold, …)` 把增长钉死在焦点线上，意味着夺冠
            # 那一刻分数**恰好等于门槛、零余量**；而每次回复都要消耗一部分，于是
            # 任何一次回复都会把群打到线下——使用者要的"能一直聊很久"在结构上
            # 不可能。同时它让"分数高于焦点线"这件事只能由消息/@ 一次性 boost
            # 造成，无法随时间积累。
            #
            # 改成 max_score 之后，焦点线恢复它本来的语义：**夺冠资格线**，不是
            # 分数天花板。余量从夺冠后继续增长的时间里来。
            #
            # 注意：高于焦点线的分数**绝不砍掉**（旧注释保留这条约束）：
            # min(焦点线, 高分) 会把 @bot 抢来的高注意力瞬间蒸发。
            rate = self._rise_rate() * self._frequency_scale(state, now) * (1.0 + emo)
            if state.attention_score < self._max_attention():
                state.attention_score = min(
                    self._max_attention(), state.attention_score + rate * dt,
                )
            # 分数不低于焦点线且夺冠计时未记录 → 记录夺冠时刻（蜜月窗口从此刻起算）。
            # 覆盖「从低涨到线」和「本来就高于线」两种情况——旧条件 before < th
            # 在分数本来就高于 th 时永远不成立，导致 focus_acquired_at 记不上。
            if state.attention_score >= self._focus_threshold() and int(state.focus_acquired_at or 0) <= 0:
                state.focus_acquired_at = now
                # 蜜月**从「稳住」开始算，不是从「刚到线」开始**。
                #
                # 旧写法用同一个 focus_acquired_at 兼作蜜月起点，于是从 0 自然涨到
                # 焦点线的群一到线就开始倒计时蜜月——它还没积累任何余量，60 秒后
                # 就被判「蜜月结束」转入 fall。使用者要的「能一直聊很久」需要
                # 夺冠后有一段**纯积累**的时间。
                #
                # 这里单独记「稳线时刻」（首次到线或跌破后再站上都算），蜜月从它算，
                # 最少留出蜜月窗口那么长的积累期。_advance_phase 的 fall 判定读它。
                state.steady_since = int(state.steady_since or 0) or now
            # 到线夺冠后蜜月结束 → 回落；未到线的群继续上升不回落。
            #
            # 读 steady_since（稳线时刻）而不是 focus_acquired_at（夺冠身份）：
            # 后者在第一次到线的瞬间就盖章，会让蜜月从「刚到线」起算。
            if (
                state.attention_score >= self._focus_threshold()
                and state.steady_since
                and now - state.steady_since >= self._honeymoon_seconds()
            ):
                state.phase = "fall"
                state.phase_started_at = now
            elif state.attention_score < self._focus_threshold():
                # 跌破焦点线 → 不再算「稳住」，下次站上时重新开始积累。
                # 不回退 focus_acquired_at：夺冠身份要留着（焦点保持逻辑依赖它）。
                state.steady_since = 0

    # ── 焦点选择 ──

    def _current_focus_group_id(self, states: list[QQGroupAttentionState]) -> str:
        # 焦点候选资格用焦点线 _focus_threshold()（默认 4.0）而非最低线 1.0：
        # 低于焦点线的群不参与焦点竞争，避免 1.1 分的群被当成焦点绕过门控。
        focused = [
            state for state in states
            if int(state.focus_acquired_at or 0) > 0 and float(state.attention_score) >= self._focus_threshold()
        ]
        if not focused:
            return ""
        return max(
            focused,
            key=lambda item: (
                int(item.focus_acquired_at or 0),
                int(item.last_focus_at or 0),
                float(item.attention_score),
            ),
        ).group_id

    def _top_candidate_group_id(self, states: list[QQGroupAttentionState], now: int) -> str:
        candidate = self._top_candidate_state(states, now)
        return candidate.group_id if candidate else ""

    def _top_candidate_state(self, states: list[QQGroupAttentionState], now: int) -> QQGroupAttentionState | None:
        # 焦点候选资格用焦点线 _focus_threshold()（默认 4.0）而非最低线 1.0，
        # 与「焦点 = 所有 attention ≥ 焦点线的群中最高者」的语义一致。
        eligible = [state for state in states if float(state.attention_score) >= self._focus_threshold()]
        if not eligible:
            return None
        return max(
            eligible,
            key=lambda item: (
                float(item.attention_score),
                int(item.last_message_at or 0),
                int(item.focus_acquired_at or 0),
            ),
        )

    def _held_focus_state(self, states: list[QQGroupAttentionState]) -> QQGroupAttentionState | None:
        """当前被**保持**的焦点：曾夺得焦点（focus_acquired_at>0）且分数仍 >= 发送保持线
        （``_focus_send_threshold``，默认 2.0）的群。

        焦点线（``_focus_threshold``，默认 4.0）是「赢得焦点」的资格线；一旦赢得，
        只要分数不掉破发送保持线（2.0）就继续持有——否则焦点群回复一次（分数被
        消耗到 2.x）就跌出焦点线、下一条消息被当非焦点拦下，发送门控形同虚设。
        多个曾夺得焦点的群取最近夺得的那个。无则返回 None。
        """
        held = [
            state for state in states
            if int(state.focus_acquired_at or 0) > 0
            and float(state.attention_score) >= self._focus_send_threshold()
        ]
        if not held:
            return None
        return max(
            held,
            key=lambda item: (
                int(item.last_focus_at or 0),
                int(item.focus_acquired_at or 0),
                float(item.attention_score),
            ),
        )

    def _choose_focus_state(
        self,
        states: list[QQGroupAttentionState],
        now: int,
        *,
        stamp_transition: bool = False,
    ) -> QQGroupAttentionState | None:
        if not states:
            return None
        # ── 优先级 1：锁（`@猫娘` / 唤醒词）────────────────────────────
        # 锁内该群独占焦点，其余群不参与竞争。这是「有人点名叫我，我必须回头
        # 应对」；分数则表达「没人叫我时我自己看哪」。两者是独立信号，锁必须
        # 先判——否则一个刚被 @ 的群会因为分数还没涨上来而被别的群顶掉。
        for state in states:
            if int(state.lock_until or 0) > now:
                return state
        # ── 优先级 2：分数仲裁（无锁时的连续归属）──────────────────────
        # 归属每 tick 重算 ⇒ 「看一眼新群，没兴趣就回旧群」是免费的：旧群只要
        # 还是最有意思的，下一个 tick 自动回去，不需要等相位走完一轮。
        #
        # 新焦点候选：必须达到焦点线（_focus_threshold，默认 4.0）
        candidate = self._top_candidate_state(states, now)
        # 焦点保持：曾夺得焦点的群只要分数 >= 发送保持线（2.0）就继续持有，
        # 即使低于焦点线；只有更高分的挑战者（>= 焦点线）才能抢走。
        held = self._held_focus_state(states)
        if held is not None:
            if candidate is None or held.attention_score >= candidate.attention_score:
                return held
        # 无持有焦点：恢复「最高分群」回退（与 get_snapshot 的 states[0] 一致）。
        # 否则低于焦点线且无持有群时 _choose_focus_state 返回 None，而 get_snapshot
        # 却把最高分群报为焦点——分裂导致 _get_top_group_id 等其它调用者拿不到焦点，
        # 门控把最高分群的后续消息判成 non_focus。
        if candidate is None:
            return max(states, key=lambda s: float(s.attention_score))
        if stamp_transition:
            candidate.focus_acquired_at = now
            candidate.last_focus_reason = "highest_attention"
        return candidate

    def _normalize_state(self, state: QQGroupAttentionState) -> QQGroupAttentionState:
        max_attention = self._max_attention()
        state.attention_score = max(0.0, min(max_attention, float(state.attention_score)))
        return state

    # ── 核心：消息更新 ──

    @staticmethod
    def _detect_question(text: str) -> bool:
        """检测消息是否为问题（问号结尾或疑问词开头）。"""
        t = str(text or "").strip()
        if not t:
            return False
        if t.endswith(("?", "？")):
            return True
        question_prefixes = ("为什么", "怎么", "什么", "如何", "能不能", "可以", "有没有", "谁知道", "请问")
        return t.startswith(question_prefixes) or any(p in t for p in ("吗？", "吗?", "么？", "么?"))

    async def update_on_message(self, message: dict[str, Any]) -> dict[str, Any]:
        if not self._enabled():
            return self.get_snapshot()
        group_id = str(message.get("group_id") or "").strip()
        if not group_id:
            return self.get_snapshot()
        focus_group_id = self.get_focus_group_id()
        now = int(message.get("timestamp") or self._current_time())
        text = str(message.get("content") or message.get("text") or "").strip()
        is_at_bot = bool(message.get("is_at_bot"))

        state = self._apply_decay(self._load_state(group_id), now, is_focus=(group_id == focus_group_id))
        state.last_message_at = now
        state.total_interactions = min(99999, int(state.total_interactions or 0) + 1)

        # 消息加速增长：@ 最强，问题次之，普通消息基础加成
        boost = self._message_boost()
        if is_at_bot:
            boost *= self._at_bot_boost()
        elif self._detect_question(text):
            boost *= self._question_boost()
        # 分类命中（mention/关键词）额外加成
        category = str(message.get("category") or "").strip()
        if not category and text:
            try:
                category = QQFeedbackClassifier.classify(
                    text, list((self.plugin._qq_settings or {}).get("backlog_labels") or [])
                )
            except Exception:
                category = ""
        if category and category != "chat":
            boost *= self._keyword_boost_ratio()
            state.last_focus_reason = category
        # **消息加成不再按相位衰减**（旧写法：fall 相位乘 attention_fall_boost_attenuation）。
        #
        # 那个衰减的意图是「正在让位的群不会因继续刷屏而赖着不走」，但它的量级算错了：
        # fall 衰减是 0.015/秒，即 30 秒掉 0.45；而被压到 0.3 的加成只有
        # 0.15 × 0.3 = 0.045/条 —— 就算群友 30 秒发一条，净增速恒为 −0.36/30s。
        # 于是**任何群一旦进入 fall 就必然一路跌到底**，回血通道实际不存在，
        # 「切去别的群看看再切回来」也就无从发生。
        #
        # 去掉之后 fall 仍然在退潮（时间衰减还在，净增速约 −0.3/30s），只是不再是
        # 「致命抽干」。让位的压力交给时间衰减与焦点竞争，而不是把回血掐断。
        state.attention_score = min(self._max_attention(), state.attention_score + boost)
        state.last_boost_at = now

        self._write_state(self._normalize_state(state))
        await self._persist()
        getattr(self.plugin, "_maybe_push_status_event", lambda: None)()  # 注意力变更 → SSE 通知前端
        return self.get_snapshot()

    async def update_on_message_count(self, group_id: str, *, message_count: int = 1) -> dict[str, Any]:
        if not self._enabled():
            return self.get_snapshot()
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return self.get_snapshot()
        focus_group_id = self.get_focus_group_id()
        now = self._current_time()
        state = self._apply_decay(self._load_state(normalized_group_id), now, is_focus=(normalized_group_id == focus_group_id))
        gain = max(0, int(message_count or 0)) * self._message_gain()
        state.attention_score = min(self._max_attention(), state.attention_score + gain)
        state.last_focus_reason = "message_recovery"
        state.last_boost_at = now
        self._write_state(self._normalize_state(state))
        await self._persist()
        return self.get_snapshot()

    # ── 回复消耗 ──

    async def update_on_reply(self, group_id: str, *, reply_message_id: str = "", at_user_id: str = "") -> dict[str, Any]:
        if not self._enabled():
            return self.get_snapshot()
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return self.get_snapshot()
        focus_group_id = self.get_focus_group_id()
        now = self._current_time()
        state = self._apply_decay(self._load_state(normalized_group_id), now, is_focus=(normalized_group_id == focus_group_id))
        state.last_reply_at = now
        # 猫娘发言消耗注意力：回复一次扣掉一个**绝对量**。
        #
        # **不用乘性消耗**（旧写法 `score *= 1 - ratio`）。乘性意味着扣掉的绝对量
        # 与当前分数成正比：4.0 扣 0.4、8.0 扣 0.8 —— 越是聊得起劲的群被罚得越重，
        # 与使用者要的「能一直聊很久」正好相反。改成一个常数，代价可预期。
        #
        # 数值口径：`consume_ratio` 仍按「max_score 的比例」解释
        # （0.1 × 10.0 = 1.0 分/条），所以 UI 上「回复消耗比例」的标签依然成立，
        # 且与分数高低解耦。
        #
        # **不在这里改相位**（见下方原注释保留的语义）：回落是「蜜月结束」或
        # 「被别的群抢走焦点」的结果，由 `_advance_phase` 依时间线判定，不该由
        # 「猫娘说了句话」触发。此前这里无条件 `phase = "fall"` 并覆盖
        # `phase_started_at`，等于每次回复都把蜜月掐断、逼这个群重新熬满
        # `attention_fall_seconds`（用户配置 240s→30s）。
        cost = max(0.0, self._max_attention() * self._consume_ratio())
        state.attention_score = max(0.0, state.attention_score - cost)
        state.last_focus_reason = "reply_consume"
        self._write_state(self._normalize_state(state))
        await self._persist()
        return self.get_snapshot()

    # ── 相位推进（幂等）──

    def _apply_decay(self, state: QQGroupAttentionState, now: int, *, is_focus: bool = False) -> QQGroupAttentionState:
        if now <= 0:
            now = self._current_time()
        self._advance_phase(state, now)
        return self._normalize_state(state)

    # ── 排序 ──

    def _sort_states(self, states: list[QQGroupAttentionState], now: int, *, focus_group_id: str = "") -> list[QQGroupAttentionState]:
        return sorted(
            states,
            key=lambda item: (
                1 if focus_group_id and item.group_id == focus_group_id else 0,
                float(item.attention_score),
                int(item.last_message_at or 0),
            ),
            reverse=True,
        )

    # ── Snapshot ──

    def _default_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled(),
            "focus_group_id": "",
            "focus_score": 0.0,
            "focus_reason": "",
            "dominant_dimension": "",
            "dimensions": {},
            "groups": [],
        }

    def get_snapshot(self) -> dict[str, Any]:
        now = self._current_time()
        # 第一步：对所有群做相位推进（此时还不知道谁是焦点，is_focus 统一用 False）
        states: list[QQGroupAttentionState] = []
        for group_id in self._normalized_groups():
            state = self._apply_decay(self._load_state(group_id), now, is_focus=False)
            self._write_state(state)
            states.append(state)
        # 第二步：选焦点（比注意力分）
        focus_state = self._choose_focus_state(states, now, stamp_transition=True)
        focus_group_id = focus_state.group_id if focus_state else ""
        if focus_state:
            self._write_state(focus_state)
        # 排序
        states = self._sort_states(states, now, focus_group_id=focus_group_id)
        if not states:
            return self._default_snapshot()
        focus = next((state for state in states if state.group_id == focus_group_id), states[0])
        return {
            "enabled": self._enabled(),
            "focus_group_id": focus.group_id,
            "focus_score": float(focus.attention_score),
            "focus_reason": focus.last_focus_reason,
            "dominant_dimension": focus.dominant_dimension(),
            "dimensions": focus.dimension_dict(),
            "groups": [state.to_dict() for state in states],
        }

    # ── 注意力上下文注入（供 LLM prompt 使用）──

    def get_attention_context(self, group_id: str) -> str:
        """生成注意力上下文文本，注入到系统提示中。"""
        snapshot = self.get_snapshot()
        is_focus = (snapshot.get("focus_group_id") == str(group_id))
        states = snapshot.get("groups") or []
        this_state = None
        for s in states:
            if str(s.get("group_id") or "") == str(group_id):
                this_state = s
                break

        parts: list[str] = []
        parts.append("## 当前群聊注意力状态")

        if is_focus:
            parts.append(f"这是你当前关注的焦点群（注意力 {snapshot.get('focus_score', 0):.1f}，相位 {snapshot.get('dominant_dimension', 'rise')}）")
            parts.append(f"主要原因: {snapshot.get('focus_reason', '') or '注意力最高'}")
        elif this_state:
            parts.append(f"这不是你当前关注的群（注意力 {float(this_state.get('attention_score', 0)):.1f}，相位 {this_state.get('phase', 'rise')}）")
        else:
            parts.append("此群暂无注意力数据。")

        emo = (this_state or {}).get("emotion", "calm") if this_state else "calm"
        if emo and emo != "calm":
            parts.append(f"当前情绪: {emo}")

        return "\n".join(parts)

    # ── 兼容旧接口 ──

    def get_focus_group_id(self) -> str:
        return str(self.get_snapshot().get("focus_group_id") or "")

    def get_focus_score(self) -> float:
        try:
            snapshot = self.get_snapshot()
            focus_id = str(snapshot.get("focus_group_id") or "")
            if not focus_id:
                return 0.0
            now = self._current_time()
            state = self._apply_decay(self._load_state(focus_id), now, is_focus=True)
            return float(state.attention_score)
        except Exception:
            return 0.0

    def _effective_focus_score(self, state: QQGroupAttentionState | None, now: int) -> float:
        if state is None:
            return 0.0
        return float(state.attention_score)

    def get_group_multiplier(self, group_id: str) -> float:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id or not self._enabled():
            return 1.0
        focus_group_id = self.get_focus_group_id()
        now = self._current_time()
        state = self._apply_decay(self._load_state(normalized_group_id), now, is_focus=(normalized_group_id == focus_group_id))
        focus_state = self._apply_decay(self._load_state(focus_group_id), now, is_focus=True) if focus_group_id else None
        focus_score = self._effective_focus_score(focus_state, now)
        group_score = self._effective_focus_score(state, now) if normalized_group_id == focus_group_id else float(state.attention_score)
        if focus_group_id and focus_group_id != normalized_group_id:
            gap = max(0.0, focus_score - group_score)
            if gap >= self._focus_threshold():
                return 0.0
            return max(0.35, 1.0 - min(0.6, gap / max(self._focus_threshold(), 1.0)))
        if group_score >= self._focus_threshold():
            return min(1.65, 1.0 + min(0.65, group_score / max(self._focus_threshold(), 1.0) * 0.25))
        if group_score <= self._minimum_threshold():
            return 0.8
        emo = state.emotion or "calm"
        return max(0.05, 1.0 + self._emotion_multiplier(emo))

    def should_focus_group(self, group_id: str) -> bool:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id or not self._enabled():
            return True
        focus_group_id = self.get_focus_group_id()
        if not focus_group_id or focus_group_id == normalized_group_id:
            return True
        now = self._current_time()
        state = self._apply_decay(self._load_state(normalized_group_id), now, is_focus=False)
        focus_state = self._apply_decay(self._load_state(focus_group_id), now, is_focus=True)
        focus_score = self._effective_focus_score(focus_state, now)
        return float(state.attention_score) + self._minimum_threshold() >= focus_score

    def mark_focus(self, group_id: str) -> None:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return
        state = self._load_state(normalized_group_id)
        now = self._current_time()
        current_id = self._current_focus_group_id([self._load_state(gid) for gid in self._normalized_groups()])
        state.last_focus_at = now
        if current_id != normalized_group_id or int(state.focus_acquired_at or 0) <= 0:
            state.focus_acquired_at = now
            state.phase = "rise"
            state.phase_started_at = now
        self._write_state(state)
        getattr(self.plugin, "_maybe_push_status_event", lambda: None)()  # 焦点变更 → SSE 通知前端

    def wake_boost(self, group_id: str) -> None:
        """叫醒时给一个注意力启动值，确保能突破焦点阈值。"""
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return
        state = self._load_state(normalized_group_id)
        if state.attention_score < self._focus_threshold():
            state.attention_score = max(
                state.attention_score, self._focus_threshold() * self._wake_boost_ratio(),
            )
            state.phase = "rise"
            state.phase_started_at = self._current_time()
            self._write_state(state)
            self.plugin._emit_log("INFO", f"[Attention] 唤醒 boost: 群{normalized_group_id} score={state.attention_score:.1f}")
            getattr(self.plugin, "_maybe_push_status_event", lambda: None)()  # 注意力唤醒 → SSE 通知前端

    # ── 锁（@ / 唤醒词）───────────────────────────────────────────────
    #
    # 设计意图（见 docs/attention-redesign-draft.md §2）：
    #   无锁时归属 = 分数最高的群，**随时可变** —— 这就是「看一眼新群、没兴趣
    #   就回旧群」的实现：旧群只要还是最有意思的，下一个 tick 就自动回去。
    #   有锁时该群独占，其余群不参与竞争。
    #
    # 与「让位」的分工：锁表达「有人点名叫我，我必须回头应对」；分数表达
    # 「没人叫我，我自己按兴趣看哪」。两者是不同信号，不该合成一个数。

    def lock_group(self, group_id: str) -> None:
        """把注意力锁在某个群一段时间（`@猫娘` / 唤醒词触发）。

        锁内 `get_focus_group()` 直接返回该群，其余群不参与竞争；到期自动解除。
        重复锁会**重置**计时（再叫一次就重新锁满）—— 这与人对重复召唤的直觉一致。
        """
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return
        now = self._current_time()
        state = self._load_state(normalized_group_id)
        state.lock_until = now + max(0, self._lock_seconds())
        state.last_focus_reason = "lock"
        self._write_state(state)
        self.plugin._emit_log(
            "INFO",
            f"[Attention] 群{normalized_group_id} 上锁 {self._lock_seconds()}s"
            f"（@/唤醒词），期内独占焦点",
        )
        getattr(self.plugin, "_maybe_push_status_event", lambda: None)()

    def release_lock(self, group_id: str) -> None:
        """提前解除锁（如锁群被移除信任）。"""
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return
        state = self._load_state(normalized_group_id)
        if int(state.lock_until or 0):
            state.lock_until = 0
            self._write_state(state)
            self.plugin._emit_log("INFO", f"[Attention] 群{normalized_group_id} 解锁")

    def locked_group_id(self, *, now: int | None = None) -> str:
        """当前仍在锁内的群（无 / 已过期 → 空串）。"""
        moment = self._current_time() if now is None else now
        best: tuple[int, str] = (0, "")
        for group_id in self._normalized_groups():
            state = self._load_state(group_id)
            until = int(state.lock_until or 0)
            if until > moment and until > best[0]:
                best = (until, state.group_id)
        return best[1]

    def _lock_seconds(self) -> int:
        """锁的时长（秒）。0 = 不锁（回到纯分数仲裁）。"""
        return max(0, int(self._setting("attention_lock_seconds", 90)))

    def get_last_focus_at(self, group_id: str) -> int:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return 0
        return int(self._load_state(normalized_group_id).last_focus_at)

    def _get_top_group_id(self) -> str:
        now = self._current_time()
        states = [self._load_state(gid) for gid in self._normalized_groups()]
        focus = self._choose_focus_state(states, now, stamp_transition=False)
        return focus.group_id if focus else ""

    # ── 全局休眠判定 ──

    def is_global_sleep(self) -> bool:
        focus_group_id = self.get_focus_group_id()
        now = self._current_time()
        for group_id in self._normalized_groups():
            state = self._apply_decay(self._load_state(group_id), now, is_focus=(group_id == focus_group_id))
            if float(state.attention_score) >= self._minimum_threshold():
                return False
        if not self._normalized_groups():
            return False
        return True

    def get_focus_group(self) -> str | None:
        if self.is_global_sleep():
            return None
        snapshot = self.get_snapshot()
        focus_id = str(snapshot.get("focus_group_id") or "")
        return focus_id if focus_id else None

    # ── 情绪 ──

    async def set_emotion(self, group_id: str, emotion: str) -> None:
        """LLM 回复中的 <feeling> 标签更新情绪状态。

        强情绪直接触发焦点切换：``_EMOTION_FORCE_FOCUS`` 抢焦点、
        ``_EMOTION_DROP_FOCUS`` 让焦点（名字见那两个集合，不在此处重抄）。
        """
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return
        if emotion not in self._emotion_multipliers():
            return
        state = self._load_state(normalized_group_id)
        now = self._current_time()
        state.emotion = emotion
        state.emotion_updated_at = now
        # emotion_display 供前端展示，停留 120s 比逻辑衰减更久
        state.emotion_display = emotion
        state.emotion_display_until = now + 120

        if emotion in _EMOTION_FORCE_FOCUS:
            # 抢焦点：把注意力抬到焦点线之上并进入蜜月上升
            state.last_focus_at = now
            state.focus_acquired_at = now
            state.last_focus_reason = f"emotion:{emotion}"
            state.attention_score = max(state.attention_score, self._focus_threshold())
            state.phase = "rise"
            state.phase_started_at = now
            self.plugin._emit_log("INFO", f"[Emotion] 群{normalized_group_id} 抢焦点: {emotion} score={state.attention_score:.1f}")
        elif emotion in _EMOTION_DROP_FOCUS:
            # 让焦点：把注意力压到目标线下并进入回落
            floor = self._minimum_threshold() if emotion == "sulking" else self._focus_threshold()
            state.attention_score = min(state.attention_score, floor)
            state.phase = "fall"
            state.phase_started_at = now
            self.plugin._emit_log("INFO", f"[Emotion] 群{normalized_group_id} 让焦点: {emotion} score={state.attention_score:.1f}")

        self._write_state(state)
        await self._persist()
        self.plugin._emit_log("INFO", f"[Emotion] 群{normalized_group_id} 情绪: {emotion}")
        getattr(self.plugin, "_maybe_push_status_event", lambda: None)()  # 情绪变更 → SSE 通知前端

    def _decay_emotion(self, state: QQGroupAttentionState, now: int) -> None:
        """情绪自然衰减：30秒无新情绪则向 calm 方向降温一级。

        上升侧/回落侧的判定来自 ``_EMOTION_MULTIPLIER`` 的符号，不另抄一份名单 ——
        以前这里硬编码了 ``("arguing", "annoyed", "playful", "curious")``，导致任何
        新加的正倍率情绪都会走回落侧分支、越衰减越强。
        """
        if state.emotion == "calm":
            return
        elapsed = now - state.emotion_updated_at
        if elapsed < _EMOTION_DECAY_SECONDS:
            return
        order = _EMOTION_DECAY_ORDER
        calm_idx = order.index("calm")
        idx = order.index(state.emotion) if state.emotion in order else -1
        if idx < 0:
            # 表外情绪（LLM 可能自造标签）直接归位到 calm
            state.emotion = "calm"
        elif _EMOTION_MULTIPLIER.get(state.emotion, 0.0) > 0:
            # 上升侧：朝 calm 走一级，不越界
            state.emotion = order[idx + 1] if idx + 1 < calm_idx else "calm"
        else:
            # 回落侧：同样朝 calm 走一级
            state.emotion = order[idx - 1] if idx - 1 > calm_idx else "calm"
        state.emotion_updated_at = now
        state.emotion_display = state.emotion

    # ── 手动增减注意力 ──

    async def boost_attention(self, group_id: str, amount: float, reason: str = "") -> None:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id or amount <= 0:
            return
        focus_group_id = self.get_focus_group_id()
        now = self._current_time()
        state = self._apply_decay(self._load_state(normalized_group_id), now, is_focus=(normalized_group_id == focus_group_id))
        state.attention_score = min(self._max_attention(), state.attention_score + amount)
        state.last_boost_at = now
        state.last_focus_reason = reason or "manual_boost"
        self._write_state(self._normalize_state(state))
        await self._persist()

    async def consume_attention(self, group_id: str, amount: float, reason: str = "") -> None:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id or amount <= 0:
            return
        focus_group_id = self.get_focus_group_id()
        now = self._current_time()
        state = self._apply_decay(self._load_state(normalized_group_id), now, is_focus=(normalized_group_id == focus_group_id))
        state.attention_score = max(0.0, state.attention_score - amount)
        state.last_reply_at = now
        state.last_focus_reason = reason or "manual_consume"
        self._write_state(self._normalize_state(state))
        await self._persist()

    # ── 后台衰减循环 ──

    async def start_decay_loop(self, interval_seconds: float | None = None) -> None:
        """启动衰减循环。``interval_seconds=None`` 时读配置（attention_decay_interval_seconds）。"""
        if interval_seconds is None:
            interval_seconds = self._decay_interval()
        self._decay_task = asyncio.create_task(self._decay_loop(interval_seconds))

    async def stop_decay_loop(self) -> None:
        task = getattr(self, "_decay_task", None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _decay_loop(self, interval_seconds: float) -> None:
        """注意力衰减驱动。

        ``decay_all`` 会在 try **外面**执行——它碰磁盘（``_persist`` 读改写
        backlog_state.json），一次坏 JSON / 磁盘错误 / 目录被撤权就会
        直接杀掉这个循环，而注意力推进与焦点释放随之中止：UI 仍显示旧值、
        没有任何报错，只能手动停止再启动插件。异常必须在这里就地吞掉并
        记录，让下一轮继续推进。
        """
        while True:
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break
            try:
                await self.decay_all()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.plugin.logger:
                    self.plugin.logger.warning(f"[Attention] 衰减轮次异常，已跳过本轮: {e}")

    async def decay_all(self) -> None:
        if not self._enabled():
            return
        now = self._current_time()
        old_focus_id = self._get_top_group_id()
        for group_id in self._normalized_groups():
            state = self._load_state(group_id)
            # emotion_display 到期 → 重置为 calm
            if now > state.emotion_display_until and state.emotion_display != "calm":
                state.emotion_display = "calm"
            state = self._apply_decay(state, now)
            self._decay_emotion(state, now)
            self._write_state(state)
        # 检查焦点是否变化，自动设置 focus_acquired_at（蜜月计时起点）
        new_focus_id = self._get_top_group_id()
        if new_focus_id and new_focus_id != old_focus_id:
            new_state = self._load_state(new_focus_id)
            new_state.focus_acquired_at = now
            new_state.phase = "rise"
            new_state.phase_started_at = now
            self._write_state(new_state)
        await self._persist()

    async def _persist(self) -> None:
        """把内存缓存里的注意力状态落盘。

        落盘走 ``backlog_store.update_group_attention_state``（锁内读改写）而
        不是自己 ``load→save``：后者与 ``append_message`` 交错时会互相覆盖
        —— 要么丢刚 append 的群消息，要么丢刚推进的注意力分数。
        """
        store = getattr(self.plugin, "backlog_store", None)
        if not store:
            # 早退语义必须保留：``cleanup_stale_cache`` 在 group_permission_mgr
            # 缺失时会把**所有**群从缓存里删掉（它按"不在信任列表里"清理），
            # 而 ``update_on_message`` 每轮都调 ``_persist``（``_KwPlugin`` 这类
            # 桩把两个依赖都设为 None）。先清理会把刚算出来的分数一起抹掉。
            return
        self.cleanup_stale_cache()
        await store.update_group_attention_state(dict(self._cache))
