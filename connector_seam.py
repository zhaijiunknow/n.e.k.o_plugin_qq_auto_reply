"""QQ 传输连接器的唯一解析点：优先宿主第一方包，回退插件内置副本。

连接器分两半，**各自解析**，判据都是能力（属性探测），不是版本号：

1. **OneBot 那半**（``OneBotClient`` / 基类 / 协议 / 工厂）：宿主 ``utils.connection.onebot``
   （PR #2996）优先；拿不到时用 ``_vendor/connection_onebot`` 里的副本。
2. **开放平台那半**（``QQOpenPlatformConnection``）：宿主 ``utils.connection.qq.open_platform``
   **带 API v2 那套接口（#3709）**才用宿主的；宿主那份还是旧版（已发布的包停在 #3709 之前：
   模块在、新接口不在），或者根本没有时，用内置的暂时适配器
   ``_vendor/connection_onebot/open_platform.py``（#3709 那两个文件的副本）。

所以会出现"OneBot 用宿主的、开放平台用内置的"这种组合 —— 已发布的宿主上就是这个形态：
OneBot 用户不受影响，开放平台用户不用等宿主发版就能用上新接口。宿主发版带上 #3709 之后
自动切回宿主 —— 不需要再发一次版。出处与退役条件见
``_vendor/connection_onebot/PROVENANCE.md``。

**只在需要连接对象时导入本模块**（``_make_qq_connection`` 里）：解析会连带拉进
``websockets`` / ``httpx``，顶层导入会拖慢插件进程启动握手。类型注解走
``TYPE_CHECKING``，运行时零成本。

TODO(#2996, #3709): 插件声明的最低支持宿主版本都带这两半之后，删掉 ``_vendor/``，
把本模块收敛成一行 re-export（或直接改回 ``from utils.connection.qq import …``）。
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

#: 宿主第一方包（PR #2996 落地于此）：OneBot 传输 + 拆分前的兼容名字。
HOST_PACKAGE = "utils.connection.onebot"

#: 宿主的开放平台适配器（拆分后的新家；#3709 把它改成了 API v2 那套接口）。
HOST_OPEN_PLATFORM_MODULE = "utils.connection.qq.open_platform"

#: 插件内置副本。
VENDORED_PACKAGE = f"{__package__}._vendor.connection_onebot"

#: 内置的暂时适配器：宿主 #3709 那份开放平台适配器的副本。
VENDORED_OPEN_PLATFORM_MODULE = f"{VENDORED_PACKAGE}.open_platform"

#: 认定"宿主真的提供了连接器"所必需的属性 —— 4 个导出 + 3 个子模块。
#: 开放平台那两个名字（``QQOpenPlatformConnection`` / ``qq_open_plat``）不向这一半要：
#: 它们由 ``_load_open_platform`` 单独解析，判据也不同（要的是新接口，不是名字在不在）。
_REQUIRED_ATTRS = (
    "OneBotClient",
    "OneBotConnectionBase",
    "OneBotConnector",
    "create_onebot_connection",
    "factory",
    "onebot_client",
    "onebot_connection",
)

#: 认定"这份开放平台适配器是 API v2 那版（#3709）"所必需的名字：模块上的 + 连接类上的。
#: 全部是 #3709 才出现的（``upload_image`` / ``send_private_image`` 来自媒体 mixin，
#: 早一版就有，一并要求是因为新适配器的发送路径建在它上面）。光看模块导不导得进来不行：
#: 已发布宿主的 ``utils.connection.qq.open_platform`` 是存在的，只是还是旧接口。
_OPEN_PLATFORM_MODULE_ATTRS = (
    "QQOpenPlatformConnection",
    "QQOpenPlatformAPIError",
    "QQOpenPlatformUnsupportedError",
    "MESSAGE_EVENTS",
    "NOTICE_EVENTS",
)
_OPEN_PLATFORM_CLASS_ATTRS = (
    "upload_image",
    "send_private_image",
    "add_event_sink",
    "group_receives_all_messages",
    "supports_card_messages",
)


def _provides_connector(module: ModuleType) -> bool:
    """模块是否**真的**提供了连接器。

    不能只看 import 成没成功：切分支留下的 ``__pycache__`` 会让
    ``utils/connection/onebot/`` 这种"目录还在、``.py`` 源码没了"的路径变成**空命名空间包**
    —— 导入成功、属性一个没有。只看导入结果的话，这里会误判成宿主可用，
    然后在取属性时炸掉，回退根本没机会生效。
    """
    return all(hasattr(module, name) for name in _REQUIRED_ATTRS)


def _provides_open_platform(module: ModuleType) -> bool:
    """模块里的开放平台适配器是不是 API v2 那版（#3709）。"""
    connection_class = getattr(module, "QQOpenPlatformConnection", None)
    return all(hasattr(module, name) for name in _OPEN_PLATFORM_MODULE_ATTRS) and all(
        hasattr(connection_class, name) for name in _OPEN_PLATFORM_CLASS_ATTRS
    )


def _load(host_package: str = HOST_PACKAGE) -> tuple[ModuleType, str]:
    """返回 ``(连接器包模块, 来源)``，来源是 ``"host"`` 或 ``"vendored"``。

    ``host_package`` 可覆写，测试靠它注入一个假的宿主包来验证"宿主优先"这条分支。
    """
    try:
        module = importlib.import_module(host_package)
    except (ImportError, ModuleNotFoundError):
        pass
    else:
        if _provides_connector(module):
            return module, "host"
    return importlib.import_module(VENDORED_PACKAGE), "vendored"


def _load_open_platform(host_module: str | None = HOST_OPEN_PLATFORM_MODULE) -> tuple[ModuleType, str]:
    """返回 ``(开放平台适配器模块, 来源)``，来源是 ``"host"`` 或 ``"vendored"``。

    宿主那份带新接口才用它，否则用内置的暂时适配器。``host_module`` 可覆写（测试注入）；
    传 ``None`` 表示不问宿主。
    """
    if host_module:
        try:
            module = importlib.import_module(host_module)
        except ImportError:
            pass
        else:
            if _provides_open_platform(module):
                return module, "host"
    return importlib.import_module(VENDORED_OPEN_PLATFORM_MODULE), "vendored"


_provider, CONNECTOR_SOURCE = _load()

# 宿主连 OneBot 那半都没有时不去问它要开放平台：宿主的 ``utils.connection.qq`` 建在
# ``utils.connection.onebot`` 之上，前者能用而后者不能用的宿主不存在。这样两半的组合只有三种
# （宿主+宿主 / 宿主+内置 / 内置+内置），工厂分流不必处理第四种。
open_platform, OPEN_PLATFORM_SOURCE = _load_open_platform(
    HOST_OPEN_PLATFORM_MODULE if CONNECTOR_SOURCE == "host" else None
)

#: 解析到的连接的模块全名。``CONNECTOR_SOURCE`` 只说"宿主还是副本"，这个说"具体是谁" ——
#: PR #2996 合并后用它确认拿到的是 ``utils.connection.onebot`` 而不是残留的副本。
CONNECTOR_MODULE = _provider.__name__

#: 解析到的开放平台适配器的模块全名：宿主带 #3709 时是 ``HOST_OPEN_PLATFORM_MODULE``，
#: 否则是 ``VENDORED_OPEN_PLATFORM_MODULE``。启动日志里和上面那个并列打出来。
OPEN_PLATFORM_MODULE = open_platform.__name__

# 子模块对象一并透出：调用方与测试会摸到 onebot_client / qq_open_plat 这类更深的名字。
factory = _provider.factory
onebot_client = _provider.onebot_client
onebot_connection = _provider.onebot_connection
#: 旧名字（拆分前开放平台实现住在 ``onebot/qq_open_plat.py``），就是上面解析到的那个模块。
qq_open_plat = open_platform

OneBotClient = _provider.OneBotClient
OneBotConnectionBase = _provider.OneBotConnectionBase
OneBotConnector = _provider.OneBotConnector
QQOpenPlatformConnection = open_platform.QQOpenPlatformConnection


def _connection_mode(settings_or_reader: Any) -> str:
    """``qq_connection_mode``，读法与连接器工厂一致（dict 或零参 callable）。"""
    settings = (settings_or_reader() if callable(settings_or_reader) else settings_or_reader) or {}
    return str(settings.get("qq_connection_mode", "napcat") or "napcat").strip()


def create_onebot_connection(settings_or_reader: Any, *, logger: Any = None, emit_log: Any = None) -> Any:
    """按连接模式造连接；签名与连接器工厂一致。

    两半同源时就是那一半的工厂。只有"OneBot 用宿主的、开放平台用内置的"这个组合要分流：
    宿主工厂造出来的是它自己那份旧适配器，所以开放平台模式改交给内置副本的工厂 ——
    它造的正是 ``VENDORED_OPEN_PLATFORM_MODULE`` 里那个类。
    """
    provider = _provider
    if OPEN_PLATFORM_SOURCE == "vendored" and _connection_mode(settings_or_reader) == "open_platform":
        provider = importlib.import_module(VENDORED_PACKAGE)
    return provider.create_onebot_connection(settings_or_reader, logger=logger, emit_log=emit_log)


# 墓碑（2026-09-28）：本模块以前还负责解析富媒体模块
# （``{CONNECTOR_MODULE}.qq_open_platform_media``，拿不到就回退 ``_vendor``）。那时富媒体
# 只能靠"以连接对象为第一参数的自由函数"，所以它得跟着连接器来源走。
#
# 现在宿主的连接对象自己混入了媒体 mixin（``upload_image`` 等），富媒体变成**连接对象的能力**，
# 与"连接器来自宿主还是副本"无关 —— 判据换成能力探测，整件事搬到了 ``media_seam.py``
# （那里也是 ``_vendor`` 唯一的富媒体引用点，退役时删那一个模块就够）。

__all__ = [
    "CONNECTOR_MODULE",
    "CONNECTOR_SOURCE",
    "HOST_OPEN_PLATFORM_MODULE",
    "HOST_PACKAGE",
    "OPEN_PLATFORM_MODULE",
    "OPEN_PLATFORM_SOURCE",
    "VENDORED_OPEN_PLATFORM_MODULE",
    "VENDORED_PACKAGE",
    "OneBotClient",
    "OneBotConnectionBase",
    "OneBotConnector",
    "QQOpenPlatformConnection",
    "create_onebot_connection",
    "factory",
    "onebot_client",
    "onebot_connection",
    "open_platform",
    "qq_open_plat",
]
