# -*- coding: utf-8 -*-
"""群聊「这句到底该不该接」的状态化判定。

**为什么要它**：原先这里是 `message_dispatcher._looks_like_human_followup` —— 一个只看
文本长度/前缀/问号的布尔启发式。调研（见 `docs/GROUP-CHAT-RESPONSE-MECHANISMS.md`）
给出了两条硬结论：

1. 我们那条规则**正好是 addressee 文献里最弱的基线**（「下一个说话人 = 上一个说话人」）：
   短会话 p@1 63.5%，长会话 Acc 只剩 **13.08%**（Le et al. 2019, D19-1199 Table 3），
   而我们的群恰在「人多话密」那一端。
2. CHI 2025 的 *Inner Thoughts* 论证：在**没人被点名**的场景，next-speaker prediction
   **本质上不适定**；可行做法是按若干维度给候选发言打分、过阈值才说。
   （8 条启发式里 Relevance 提及 77 次、Information Gap 33 次、Expected Impact 23 次。）

**分值配方**取自 MaiBot `src/maisaka/reply_necessity.py`（常量逐字对照，可考据），
并按我们的语境做了两处调整，都在下面常量处注明：

- 相关性强相关项用我们已有的信号（@ / 引用她 / focus / 私聊）；
- 「存在感惩罚」吃的是**近 300 秒内她自己发言占比**——这是全项目里唯一
  「说太多 → 降低再次发言意愿」的量化机制，也是我们最缺的那类自适应。

本模块**纯逻辑**（不碰 plugin / 网络 / 文件），因此可以被单测穷举。
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field

# ── 打分常量（MaiBot reply_necessity 对照；注释标出处）─────────────────────────
AT_SCORE = 100                 # MaiBot: has_at → 100
MENTION_SCORE = 80             # MaiBot: has_mention → 80
FOCUS_SCORE = 40               # MaiBot: focus_active → 40
PRIVATE_SCORE = 40             # MaiBot: 私聊 → 40
PLAIN_SCORE = 0                # MaiBot: 普通（没人叫）→ 0

QUESTION_SCORE = 15            # 问题
REQUEST_SCORE = 20             # 请求
OPINION_SCORE = 20             # 征询意见
LONG_LENGTH_40 = 5             # 总长 ≥40
LONG_LENGTH_120 = 10           # 总长 ≥120
SHORT_REACTION_PENALTY = -25   # 纯短反应批次（"哈哈/草/666" 之类）

PRESSURE_STANDARD_SCORE = 50.0  # 积压压力：ratio ≤ 1 时的满分
PRESSURE_MAX_SCORE = 100.0      # 上限
PRESSURE_FULL_RATIO = 4.0       # ratio > 1 时 log 曲线的饱和点
PRESSURE_IDLE_BONUS = 15        # 有积压且已到平均间隔 → 补分

SELF_RATIO_FREE = 0.25          # 近窗口内自己发言占比 ≤ 此值不罚
SELF_RATIO_FULL = 0.60          # 占比 ≥ 此值罚满
SELF_PENALTY_MAX = 25.0         # 存在感惩罚上限

#: 判定阈值默认值。
#:
#: ⚠️ 这个数是**行为旋钮**，不是照抄来的：MaiBot 用 80（它这一关同时承担「攒够几条
#: 才值得思考」），而我们把这一关放在**焦点群内部**（群间竞争已由注意力系统解决），
#: 所以要比 80 低、但必须高于「focus 的 40」——否则焦点群里的普通消息光靠 focus 分
#: 就能通过，这一关等于不存在（接线时被集成测试抓到的正是这个）。
#:
#: **默认值由真机回放定**：把 `backlog_state.json` 里 244 条真实群消息按时间回放
#: （模拟「判定通过 = 她回了一句」，因此存在感会像真实一样累积），得到
#:
#:     阈值  30 → 抑制 25.8%   40 → 49.6%   50 → 76.7%   60 → 86.0%   70 → 90.7%
#:
#: 60 会让她在真实群里近乎静音（只有被 @ 或积压巨大时才开口）——太狠，故取 **40**：
#: 「参加但不抢话」。这一档的实际效果是：
#:   · 被 @ / 引用她            → 100 / 80，**必过**
#:   · 群很忙（积压到阈值）     → 40 + 50 = 90，过
#:   · 有人提问（哪怕群里安静） → 40 + 15 + ≈1 = 56，过
#:   · 安静时的普通闲聊         → 40 + ≈1 = 41，过（**仍然搭话**，不像 60 那样一律静音）
#:   · 纯短反应（"哈哈"）       → 16，不过
#:   · 她刚说过一阵子（占比高） → 再扣最多 25 → 普通闲聊 20 上下，不过 ← 主力机制
#:   · 连续不接 → 空闲退避接管，指数放长到 300s
#: 取 0 = 关闭这一关（回到「交给 LLM 自判」的老行为）；想更安静就往上调。
DEFAULT_TRIGGER_SCORE = 40.0

#: 短反应词表（MaiBot 逐字）：整条消息 ≤8 字符且全部落在集合里才算「纯短反应批次」。
SHORT_REACTIONS = frozenset({"哈哈", "哈哈哈", "草", "笑死", "好", "嗯", "啊", "哦", "6", "666", "？", "?"})

#: 「这话是对另一个 AI 说的」——MaiBot 里有硬编码词表，我们**不硬编码 bot 名字**，
#: 只保留第三方助手词表（那是它在源码里唯一的 addressee 显式处理），
#: 自己名字的匹配一律走 ``is_at_bot`` / ``is_reply_to_bot``。
OTHER_ASSISTANT_PATTERN = re.compile(
    r"^(?:DeepSeek|ChatGPT|Grok|豆包|千问|元宝|通义|Kimi|Claude)[，,、\s]",
    re.IGNORECASE,
)

_NOISE_PATTERNS = (
    re.compile(r"\[CQ:reply[^\]]*\]"),
    re.compile(r"\[reply\]"),
    re.compile(r"\[回复了.+?的消息: .+?\]"),
    re.compile(r"\[回复消息\]"),
    re.compile(r"@<[^>]+>"),
    re.compile(r"@\S+"),
)

_QUESTION_HINTS = ("?", "？", "吗", "呢", "怎么", "如何", "为什么", "为啥", "多少", "哪个", "哪儿")
_REQUEST_HINTS = ("帮我", "请帮", "能不能", "可以帮", "来一个", "给我", "麻烦你", "麻烦帮")
_OPINION_HINTS = ("怎么看", "意见", "建议", "觉得呢", "你认为", "你说呢", "评价一下")


def strip_noise(text: str) -> str:
    """剥掉回复/@ 等噪声，只留正文（MaiBot ``strip_reply_necessity_noise`` 的对应物）。"""
    out = str(text or "")
    for pattern in _NOISE_PATTERNS:
        out = pattern.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def is_short_reaction_batch(text: str) -> bool:
    """纯短反应批次：整条 ≤8 字符且全落在 ``SHORT_REACTIONS`` 里。"""
    compact = strip_noise(text).replace(" ", "")
    if not compact or len(compact) > 8:
        return False
    # 允许多个短反应连写（"哈哈好"）：逐字符窗口核验太严，这里按集合成员切分。
    rest = compact
    while rest:
        matched = next((word for word in sorted(SHORT_REACTIONS, key=len, reverse=True) if rest.startswith(word)), "")
        if not matched:
            return False
        rest = rest[len(matched):]
    return True


#: 接话前缀（原 ``_looks_like_human_followup`` 的内联表，逐字保留）。
FOLLOWUP_PREFIXES = (
    "不是", "对", "行", "那", "所以", "为啥", "为什么", "你这", "他这", "她这",
    "这样", "那你", "那他", "那她", "可是", "但是",
)

#: 原内联规则的长度阈值（逐字保留，行为不变）。
FOLLOWUP_LONG_TEXT = 36
FOLLOWUP_SHORT_TEXT = 12
FOLLOWUP_QUESTION_MAX = 24


@dataclass(frozen=True, slots=True)
class AddresseeVerdict:
    """一条消息「大概在跟谁说话」的结构化判断。

    比原先那个布尔多出来的就是 ``kind`` 与 ``reason``：排查时能看出**为什么**判成接话，
    而不是只看到一个 True/False。
    """

    kind: str          # human_followup | short_reaction | question | long_text | plain
    is_likely_human_followup: bool
    reason: str


def classify_addressee(text: str) -> AddresseeVerdict:
    """判断这条消息是否「像是群友在跟别人接话」（而不是在对她说）。

    行为与替换前的 ``_looks_like_human_followup`` **逐条一致**（长度阈值、前缀表、
    问号结尾规则都照搬），只是把结论结构化了。调研结论已说明这条规则本身是弱基线
    （长会话 Acc 13%，见模块 docstring），所以它现在只作为**信号之一**，
    真正的判定交给 ``score_necessity``。
    """
    normalized = strip_noise(text)
    if not normalized:
        return AddresseeVerdict("plain", False, "空消息")
    compact = normalized.replace(" ", "")
    if compact.startswith(FOLLOWUP_PREFIXES):
        return AddresseeVerdict("human_followup", True, "接话前缀")
    if is_short_reaction_batch(compact):
        return AddresseeVerdict("short_reaction", True, "短反应")
    if compact.endswith(("?", "？", "!", "！")) and len(compact) <= FOLLOWUP_QUESTION_MAX:
        return AddresseeVerdict("question", True, "短问句")
    if len(compact) >= FOLLOWUP_LONG_TEXT:
        return AddresseeVerdict("long_text", False, "长文")
    if len(compact) <= FOLLOWUP_SHORT_TEXT:
        return AddresseeVerdict("human_followup", True, "短句")
    return AddresseeVerdict("plain", False, "普通长度")


@dataclass(frozen=True, slots=True)
class NecessitySignals:
    """判定输入。全部由调用方提供，模块本身不取任何数据。"""

    message_text: str = ""
    is_at_bot: bool = False
    is_reply_to_bot: bool = False
    is_group: bool = True
    focus_active: bool = False
    #: 该群近期未处理消息数（含当前这条）与阈值——压力分来源
    pending_count: int = 1
    pending_threshold: int = 3
    #: 近窗口内她自己发言占比（0~1）——存在感惩罚来源
    self_ratio: float = 0.0
    #: 已到平均消息间隔却仍无积压 → 补一点分（MaiBot 的 idle_reached_average）
    idle_reached_average: bool = False


@dataclass(frozen=True, slots=True)
class NecessityBreakdown:
    relevance: int = 0
    content: int = 0
    pressure: int = 0
    presence: int = 0
    frequency_factor: float = 1.0
    raw: float = 0.0
    score: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class NecessityVerdict:
    decision: str            # "trigger" | "wait"
    score: int
    breakdown: NecessityBreakdown

    @property
    def reason(self) -> str:
        """给门控日志用的一行原因（可诊断，不含隐私文本）。"""
        return f"necessity_{self.decision}({self.score}:{'+'.join(self.breakdown.reasons) or '—'})"


def _relevance(signals: NecessitySignals) -> tuple[int, str]:
    if signals.is_at_bot:
        return AT_SCORE, "@"
    if signals.is_reply_to_bot:
        return MENTION_SCORE, "引用她"
    if not signals.is_group:
        return PRIVATE_SCORE, "私聊"
    if signals.focus_active:
        return FOCUS_SCORE, "focus"
    return PLAIN_SCORE, "普通"


def _content(signals: NecessitySignals) -> tuple[int, str]:
    text = strip_noise(signals.message_text)
    if not text:
        return 0, ""
    if is_short_reaction_batch(text):
        return SHORT_REACTION_PENALTY, "短反应"
    score = 0
    labels: list[str] = []
    if any(hint in text for hint in _QUESTION_HINTS):
        score += QUESTION_SCORE
        labels.append("提问")
    # 开头点名的是**别的助手**（"DeepSeek，帮我看看"）→ 请求/征询类加分作废：
    # 这话不是冲我们来的。（MaiBot 同款词表处理；我们不复刻它硬编码自己名字的坑。）
    addressed_elsewhere = bool(OTHER_ASSISTANT_PATTERN.match(text))
    if any(hint in text for hint in _REQUEST_HINTS) and not addressed_elsewhere:
        score += REQUEST_SCORE
        labels.append("请求")
    if any(hint in text for hint in _OPINION_HINTS) and not addressed_elsewhere:
        score += OPINION_SCORE
        labels.append("征询")
    if len(text) >= 120:
        score += LONG_LENGTH_120
        labels.append("长文120+")
    elif len(text) >= 40:
        score += LONG_LENGTH_40
        labels.append("长文40+")
    return score, "+".join(labels)


def _pressure(signals: NecessitySignals) -> tuple[int, str]:
    threshold = max(1, int(signals.pending_threshold or 1))
    pending = max(0, int(signals.pending_count or 0))
    if pending <= 0:
        return 0, ""
    ratio = pending / threshold
    if ratio <= 1.0:
        score = PRESSURE_STANDARD_SCORE * ratio * ratio          # 二次增长：1 条时几乎不推
        if signals.idle_reached_average:
            score += PRESSURE_IDLE_BONUS
        return min(int(round(score)), int(PRESSURE_STANDARD_SCORE)), "积压"
    overflow = ratio - 1.0
    # 对数曲线（MaiBot 原式：`50 + round(50 * log1p(overflow)/log1p(4.0))`）。
    # ⚠️ 乘子必须是 STANDARD(50) 而不是 MAX(100)：用 100 时 overflow≈1.16 就顶到上限，
    # 曲线退化成阶跃（这个错误被单测抓过）。
    score = PRESSURE_STANDARD_SCORE + PRESSURE_STANDARD_SCORE * math.log1p(overflow) / math.log1p(PRESSURE_FULL_RATIO)
    return min(int(round(score)), int(PRESSURE_MAX_SCORE)), "积压多"


def _presence(signals: NecessitySignals) -> tuple[int, str]:
    ratio = max(0.0, min(1.0, float(signals.self_ratio or 0.0)))
    if ratio <= SELF_RATIO_FREE:
        return 0, ""
    span = max(1e-6, SELF_RATIO_FULL - SELF_RATIO_FREE)
    penalty = SELF_PENALTY_MAX * min(1.0, (ratio - SELF_RATIO_FREE) / span)
    return -int(round(penalty)), "存在感"


def score_necessity(
    signals: NecessitySignals,
    *,
    threshold: float = DEFAULT_TRIGGER_SCORE,
    frequency: float = 1.0,
) -> NecessityVerdict:
    """给一条消息打分并给出 trigger / wait。

    ``threshold <= 0`` ⇒ 恒 trigger（关闭这一关，回到「交给 LLM 自判」）。
    """
    relevance, rel_reason = _relevance(signals)
    content, content_reason = _content(signals)
    pressure, pressure_reason = _pressure(signals)
    presence, presence_reason = _presence(signals)

    reasons = tuple(r for r in (rel_reason, content_reason, pressure_reason, presence_reason) if r)
    raw = float(relevance + content + pressure + presence)
    # 频率因子：把「这个群我本来就不怎么说话」乘进总分（MaiBot 同款 0.5~1.0）。
    factor = 0.5 + 0.5 * max(0.0, min(1.0, float(frequency or 0.0)))
    score = max(0, int(round(raw * factor)))

    limit = float(threshold or 0.0)
    decision = "trigger" if limit <= 0 or score >= limit else "wait"
    breakdown = NecessityBreakdown(
        relevance=relevance,
        content=content,
        pressure=pressure,
        presence=presence,
        frequency_factor=factor,
        raw=raw,
        score=score,
        reasons=reasons,
    )
    return NecessityVerdict(decision=decision, score=score, breakdown=breakdown)


# ── 状态：近期发言窗口 + 空闲退避 ────────────────────────────────────────────

SELF = "__self__"


class GroupSpeechTracker:
    """每群近期发言窗口（**内存态**，重启即失，与 MaiBot 的同类状态一致）。

    它提供两个量：

    - ``pending_count``：她上次发言之后又来了几条（积压压力）
    - ``self_ratio``：窗口内她自己发言占比（存在感惩罚）
    """

    WINDOW_SECONDS = 300.0
    MAX_EVENTS = 200

    def __init__(self, *, window_seconds: float = WINDOW_SECONDS) -> None:
        self.window_seconds = float(window_seconds)
        self._events: dict[str, deque[tuple[float, str]]] = {}

    def _prune(self, group_id: str, now: float) -> deque[tuple[float, str]]:
        events = self._events.get(group_id)
        if events is None:
            events = deque(maxlen=self.MAX_EVENTS)
            self._events[group_id] = events
        cutoff = now - self.window_seconds
        while events and events[0][0] < cutoff:
            events.popleft()
        return events

    def record(self, group_id: str, *, now: float | None = None, speaker: str = "") -> None:
        key = str(group_id or "").strip()
        if not key:
            return
        ts = float(now if now is not None else time.time())
        self._prune(key, ts).append((ts, str(speaker or "")))

    def record_self(self, group_id: str, *, now: float | None = None) -> None:
        self.record(group_id, now=now, speaker=SELF)

    def pending_count(self, group_id: str, *, now: float | None = None) -> int:
        """她上次发言之后的他消息条数（没有她的发言时 = 窗口内全部）。"""
        key = str(group_id or "").strip()
        ts = float(now if now is not None else time.time())
        events = self._prune(key, ts)
        count = 0
        for _, speaker in reversed(events):
            if speaker == SELF:
                break
            count += 1
        return count

    def self_ratio(self, group_id: str, *, now: float | None = None) -> float:
        key = str(group_id or "").strip()
        ts = float(now if now is not None else time.time())
        events = self._prune(key, ts)
        if not events:
            return 0.0
        mine = sum(1 for _, speaker in events if speaker == SELF)
        return mine / len(events)

    def last_gap_seconds(self, group_id: str, *, now: float | None = None) -> float:
        """距窗口内最后一条消息过去了多久（没有任何消息时返回一个很大的值）。

        用途：MaiBot 的 ``idle_reached_average`` —— 「群里已经静了一会儿但仍有积压」
        属于值得接的信号，压力分给一点补偿。
        """
        key = str(group_id or "").strip()
        ts = float(now if now is not None else time.time())
        events = self._prune(key, ts)
        if not events:
            return float("inf")
        return max(0.0, ts - events[-1][0])

    def forget(self, group_id: str) -> None:
        self._events.pop(str(group_id or "").strip(), None)


class IdleBackoff:
    """空闲退避：连续「决定不接」之后把再次评估的间隔指数放长。

    常量取自 MaiBot（``no_action_backoff_*``）：15s 起、上限 300s、连续 2 次开始、
    积压 ≥6 条**绕过**退避。任何 @ / 引用她 / 她的新发言都会重置。
    """

    BASE_SECONDS = 15.0
    CAP_SECONDS = 300.0
    START_COUNT = 2
    BYPASS_PENDING = 6

    def __init__(self) -> None:
        self._count: dict[str, int] = {}
        self._until: dict[str, float] = {}

    def delay_seconds(self, group_id: str, *, now: float | None = None, pending_count: int = 0) -> float:
        """还需等待多少秒（0 = 可以立即评估）。积压够多直接绕过。"""
        key = str(group_id or "").strip()
        if pending_count >= self.BYPASS_PENDING:
            return 0.0
        ts = float(now if now is not None else time.time())
        remaining = float(self._until.get(key, 0.0)) - ts
        return max(0.0, remaining)

    def record_wait(self, group_id: str, *, now: float | None = None) -> float:
        """记一次「这轮不接」，返回本次设定的退避秒数。"""
        key = str(group_id or "").strip()
        if not key:
            return 0.0
        ts = float(now if now is not None else time.time())
        count = self._count.get(key, 0) + 1
        self._count[key] = count
        if count < self.START_COUNT:
            return 0.0
        exponent = max(0, count - self.START_COUNT)
        seconds = min(self.CAP_SECONDS, self.BASE_SECONDS * (2 ** exponent))
        self._until[key] = ts + seconds
        return seconds

    def reset(self, group_id: str) -> None:
        key = str(group_id or "").strip()
        self._count.pop(key, None)
        self._until.pop(key, None)
