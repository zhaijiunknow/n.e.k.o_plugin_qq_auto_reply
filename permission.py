"""
权限管理模块

根据 QQ 号管理用户权限等级
"""

from typing import Any, Dict, List, Optional


class PermissionManager:
    """权限管理器"""

    #: 用户级别。``blacklist``（2026-09-27 使用者要求）＝**不处理这个人的消息**：
    #: 群消息、戳一戳、入群欢迎、私聊一律在派发层最前面被拦掉，不进 backlog、
    #: 不进注意力、不进 LLM（实现见 ``message_dispatcher.handle_message`` 顶部）。
    VALID_LEVELS = {"admin", "trusted", "normal", "blacklist"}
    NICKNAME_MAX_CHARS = 64
    _NICKNAME_FORBIDDEN_CHARS = frozenset("[]|")
    _NICKNAME_ALLOWED_FORMAT_CHARS = frozenset({"\u200d"})

    def __init__(
        self,
        trusted_users: List[Dict[str, Any]] = None,
    ):
        """
        初始化权限管理器

        Args:
            trusted_users: 信任用户列表，格式: [{"qq": "123456", "level": "admin", "nickname": "小明"}, ...]

        信赖度（speaker_trust）已上移到 memory_server 的全局池，本类只负责
        名册与权限档位：插件的唯一职责是**上报档位**（wire 上的
        `speaker_tier`），分数由服务端按 account 全局计算。磁盘上的
        `speaker_trust_profiles` 键原样保留，只做一次性迁移源。
        """
        self._users: Dict[str, Dict[str, Any]] = {}  # {qq: {level, nickname?, normal_relay_probability?}}
        #: 载入时级别认不出来的条目 ``(QQ 号, 原样写的级别)``，已按最低档收录；
        #: 由调用方写进日志，让人去改配置。
        self.invalid_levels: List[tuple[str, str]] = []
        #: 认领别名的换算（开放平台 openid → 旧账号，见 ``identity_links``）。读名单时，
        #: 这个 ID 自己没有条目才去查它认领的那个旧账号。默认原样返回。
        self.resolve = lambda value: str(value or "").strip()

        if trusted_users:
            for user in trusted_users:
                # 容错：手写配置常见 ["123456"] 字符串条目（真机已炸过一次
                # startup：'str' object has no attribute 'get'）。非空字符串按
                # 默认档位收录，其余坏条目跳过——一条脏数据不该炸整个插件。
                if not isinstance(user, dict):
                    raw = str(user or "").strip()
                    if raw:
                        self._users[raw] = {
                            "level": "trusted",
                            "nickname": "",
                            "normal_relay_probability": None,
                        }
                    continue
                qq = self._normalize_qq(user.get("qq", ""))
                if qq and self.recognise_level(user.get("level")) is None:
                    self.invalid_levels.append((qq, str(user.get("level"))))
                level = self._normalize_level(user.get("level", "trusted"))
                nickname = str(user.get("nickname", "") or "").strip()
                normal_relay_probability = self._normalize_probability(user.get("normal_relay_probability"))
                if qq:
                    self._users[qq] = {
                        "level": level,
                        "nickname": nickname,
                        "normal_relay_probability": normal_relay_probability,
                    }

    @staticmethod
    def _normalize_qq(qq_number: str) -> str:
        return str(qq_number or "").strip()

    def _entry(self, qq_number: str) -> Dict[str, Any]:
        """读名单用：自己的条目优先，没有再看它认领的旧账号。"""
        key = self._normalize_qq(qq_number)
        return self._users.get(key) or self._users.get(self.resolve(key)) or {}

    @classmethod
    def recognise_level(cls, level: Any) -> Optional[str]:
        """没写（None / 空白）→ ``trusted``；认得出来 → 那一档；写了却认不出来 → None。"""
        level_text = str(level if level is not None else "").strip().lower()
        if not level_text:
            return "trusted"
        return level_text if level_text in cls.VALID_LEVELS else None

    @classmethod
    def _normalize_level(cls, level: str) -> str:
        # 写了却认不出来的级别落到 ``normal``（权限最低的有效档），不是 ``trusted``：
        # 以前把 blacklist 写错一个字母，这个人反而成了信任用户。
        return cls.recognise_level(level) or "normal"

    @staticmethod
    def _normalize_probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            normalized = float(value)
        except Exception:
            return None
        if normalized < 0.0 or normalized > 1.0:
            return None
        return normalized

    @classmethod
    def validate_nickname(cls, nickname: str) -> Optional[str]:
        """校验昵称是否可写。合法返回 None；否则返回原因键（``too_long`` /
        ``control_char``），供上层在提交前区分「昵称非法」与其它失败。

        与 ``_normalize_nickname_for_write`` 同一套规则：超长、含控制字符或
        不可见字符（除允许的格式字符）均判非法。
        """
        raw = str(nickname or "")
        normalized = raw.strip()
        if len(normalized) > cls.NICKNAME_MAX_CHARS:
            return "too_long"
        for char in raw:
            if char in cls._NICKNAME_FORBIDDEN_CHARS or (
                not char.isprintable()
                and char not in cls._NICKNAME_ALLOWED_FORMAT_CHARS
            ):
                return "control_char"
        return None

    def _normalize_nickname_for_write(cls, nickname: str) -> Optional[str]:
        raw = str(nickname or "")
        normalized = raw.strip()
        if len(normalized) > cls.NICKNAME_MAX_CHARS:
            return None
        if any(
            char in cls._NICKNAME_FORBIDDEN_CHARS
            or (
                not char.isprintable()
                and char not in cls._NICKNAME_ALLOWED_FORMAT_CHARS
            )
            for char in raw
        ):
            return None
        return normalized

    def add_user(self, qq_number: str, level: str = "trusted", nickname: str = "", normal_relay_probability: Any = None):
        """
        添加用户

        Args:
            qq_number: QQ 号
            level: 权限等级 (admin, trusted, normal)
            nickname: 用户昵称（可选）
        """
        qq_str = self._normalize_qq(qq_number)
        if not qq_str:
            return False
        normalized_level = self.recognise_level(level)
        if normalized_level is None:
            # 认不出来的级别：拒绝，名单不动（不悄悄存成别的档）。
            return False
        normalized_nickname = (
            "" if normalized_level == "admin"
            else self._normalize_nickname_for_write(nickname)
        )
        if normalized_nickname is None:
            return False
        self._users[qq_str] = {
            "level": normalized_level,
            "nickname": normalized_nickname,
            "normal_relay_probability": self._normalize_probability(normal_relay_probability),
        }
        return True

    def remove_user(self, qq_number: str):
        """移除用户"""
        qq_str = self._normalize_qq(qq_number)
        if qq_str in self._users:
            del self._users[qq_str]

    def get_permission_level(self, qq_number: str) -> str:
        """
        获取用户权限等级

        Args:
            qq_number: QQ 号

        Returns:
            权限等级: admin, trusted, normal, none
        """
        return str(self._entry(qq_number).get("level") or "none")

    def list_users(self) -> List[Dict[str, Any]]:
        """列出所有用户"""
        result = []
        for qq, user in self._users.items():
            user_info: Dict[str, Any] = {"qq": qq, "level": user.get("level", "trusted")}
            nickname = str(user.get("nickname") or "").strip()
            if nickname:
                user_info["nickname"] = nickname
            probability = self._normalize_probability(user.get("normal_relay_probability"))
            if probability is not None:
                user_info["normal_relay_probability"] = probability
            result.append(user_info)
        return result

    def get_nickname(self, qq_number: str) -> Optional[str]:
        """获取用户昵称"""
        user = self._entry(qq_number)
        nickname = str(user.get("nickname") or "").strip()
        return nickname or None

    def get_normal_relay_probability(self, qq_number: str) -> Optional[float]:
        user = self._entry(qq_number)
        return self._normalize_probability(user.get("normal_relay_probability"))

    def set_nickname(self, qq_number: str, nickname: str):
        """设置用户昵称"""
        qq_str = self._normalize_qq(qq_number)
        if qq_str not in self._users:
            return False
        normalized = self._normalize_nickname_for_write(nickname)
        if normalized is None:
            return False
        self._users[qq_str]["nickname"] = normalized
        return True

    def find_users_by_nickname(self, nickname: str) -> List[Dict[str, Any]]:
        """按配置昵称查找用户（精确匹配）"""
        target = str(nickname or "").strip()
        if not target:
            return []
        result = []
        for qq, user in self._users.items():
            saved_nickname = str(user.get("nickname") or "").strip()
            if saved_nickname == target:
                user_info: Dict[str, Any] = {"qq": qq, "level": user.get("level", "trusted"), "nickname": saved_nickname}
                probability = self._normalize_probability(user.get("normal_relay_probability"))
                if probability is not None:
                    user_info["normal_relay_probability"] = probability
                result.append(user_info)
        return result

    def is_admin(self, qq_number: str) -> bool:
        """检查是否是管理员"""
        return self.get_permission_level(qq_number) == "admin"

    def is_blacklisted(self, qq_number: str) -> bool:
        """检查是否被拉黑（级别 ``blacklist``）。

        拉黑的人**不该有任何特权**：``is_trusted`` / ``is_admin`` 对它都为 False。
        """
        return self.get_permission_level(qq_number) == "blacklist"

    def is_trusted(self, qq_number: str) -> bool:
        """检查是否是信任用户（包括管理员）"""
        level = self.get_permission_level(qq_number)
        return level in ["admin", "trusted"]
