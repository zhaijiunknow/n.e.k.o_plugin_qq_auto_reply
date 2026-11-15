"""``connector_seam`` 的解析行为与漂移守卫。

连接器在两种宿主上都要能跑：有 PR #2996 的宿主走 ``utils.connection.onebot``，
没有的走 ``_vendor`` 里的副本。这里盯住三件事：

1. 宿主真的提供了连接器时，**宿主优先**；
2. 宿主没提供（缺包，**或者**只剩一个空目录）时，回退副本；
3. 两边都在时，``OneBotConnector`` 协议的成员集合必须一致 —— 副本与宿主漂移了就要红。
   这是 PR 合并后拆掉 ``_vendor/`` 时的安全网。

开放平台那半**单独解析**（2026-11-15）：宿主的 ``utils.connection.qq.open_platform`` 带
API v2 那套接口（#3709）才用宿主的，否则用内置的暂时适配器 —— 已发布的宿主停在 #3709 之前，
模块在、新接口不在，所以判据是接口齐不齐，不是模块导不导得进来。
"""
from __future__ import annotations

import importlib
import pathlib
import sys
from types import ModuleType

import pytest
from plugin.plugins.qq_auto_reply import connector_seam

#: 保证导入不出来的宿主包名（没有对应的父包）。
_MISSING_HOST = "no_such_host_package_xyz.connection"


def _fake_connector_package() -> ModuleType:
    """一个"看起来像"宿主连接器包的假模块：必需属性齐全。"""
    mod = ModuleType("fake_host_connector")
    for name in connector_seam._REQUIRED_ATTRS:
        setattr(mod, name, object())
    return mod


def test_host_is_preferred_when_it_really_provides_the_connector(monkeypatch):
    fake = _fake_connector_package()
    monkeypatch.setitem(sys.modules, "fake_host_pkg", fake)

    provider, source = connector_seam._load("fake_host_pkg")

    assert source == "host"
    assert provider is fake


def test_falls_back_when_host_package_is_missing():
    provider, source = connector_seam._load(_MISSING_HOST)

    assert source == "vendored"
    assert connector_seam._provides_connector(provider)
    assert provider.__name__ == connector_seam.VENDORED_PACKAGE


def test_falls_back_when_host_is_an_empty_namespace_package(monkeypatch):
    """空目录也算"宿主没提供"。

    真机上踩过：切分支会把 ``utils/connection/onebot/*.py`` 删掉、却留下 ``__pycache__``，
    那个目录于是变成一个**空命名空间包** —— ``import`` 成功、属性一个没有。
    只看导入成没成功的话这里会误判成宿主可用，回退根本没机会生效。
    """
    empty = ModuleType("empty_namespace_pkg")  # 无任何属性
    monkeypatch.setitem(sys.modules, "empty_host_pkg", empty)

    provider, source = connector_seam._load("empty_host_pkg")

    assert source == "vendored"
    assert connector_seam._provides_connector(provider)


def test_vendored_provider_builds_a_working_connection():
    """回退那条路必须真能用 —— 工厂造得出连接，且符合既有契约。"""
    conn = connector_seam.create_onebot_connection({"qq_connection_mode": "napcat"})

    assert hasattr(conn, "set_inbound_sink")
    assert conn.inbound_sink is None
    assert conn.mode == "napcat"
    assert conn.direction == "reverse"


def test_vendored_matches_host_protocol_surface():
    """副本与宿主的协议成员集合必须一致；宿主不在时本轮无标的，跳过。"""
    host, source = connector_seam._load()
    if source != "host":
        pytest.skip("宿主未提供 utils.connection.onebot，漂移守卫本轮无标的")

    vendored = importlib.import_module(connector_seam.VENDORED_PACKAGE)

    def _public_members(obj) -> set[str]:
        return {name for name in dir(obj) if not name.startswith("_")}

    assert _public_members(host.OneBotConnector) == _public_members(vendored.OneBotConnector), (
        "宿主与副本的 OneBotConnector 协议已经漂移 —— 对齐后（或合并后拆掉 _vendor/）再放行"
    )


# ── 开放平台那半：宿主带新接口（#3709）才用宿主的，否则用内置的暂时适配器 ──────


def _fake_open_platform_module(*, without: str = "") -> ModuleType:
    """一个"看起来像"宿主开放平台适配器的假模块；``without`` 指定缺哪一个名字。"""
    mod = ModuleType("fake_host_open_platform")
    members = {name: (lambda self: None) for name in connector_seam._OPEN_PLATFORM_CLASS_ATTRS if name != without}
    for name in connector_seam._OPEN_PLATFORM_MODULE_ATTRS:
        if name != without:
            setattr(mod, name, object())
    if without != "QQOpenPlatformConnection":
        mod.QQOpenPlatformConnection = type("QQOpenPlatformConnection", (), members)
    return mod


def test_host_open_platform_is_preferred_when_it_has_the_new_api(monkeypatch):
    fake = _fake_open_platform_module()
    monkeypatch.setitem(sys.modules, "fake_host_open_platform", fake)

    module, source = connector_seam._load_open_platform("fake_host_open_platform")

    assert source == "host"
    assert module is fake


@pytest.mark.parametrize(
    "missing",
    [*connector_seam._OPEN_PLATFORM_MODULE_ATTRS, *connector_seam._OPEN_PLATFORM_CLASS_ATTRS],
)
def test_builtin_adapter_is_used_when_the_host_one_predates_the_new_api(monkeypatch, missing):
    """已发布宿主的形态：模块导得进来，但新接口缺了哪怕一个 —— 那就不是新版，用内置的。"""
    monkeypatch.setitem(sys.modules, "fake_host_open_platform", _fake_open_platform_module(without=missing))

    module, source = connector_seam._load_open_platform("fake_host_open_platform")

    assert source == "vendored"
    assert module.__name__ == connector_seam.VENDORED_OPEN_PLATFORM_MODULE


@pytest.mark.parametrize("host_module", [_MISSING_HOST, None])
def test_builtin_adapter_is_used_when_the_host_has_no_open_platform_module(host_module):
    module, source = connector_seam._load_open_platform(host_module)

    assert source == "vendored"
    assert module.__name__ == connector_seam.VENDORED_OPEN_PLATFORM_MODULE


def test_builtin_adapter_is_the_new_api_and_builds():
    """内置适配器自己必须过得了同一条判据，而且真能造出连接（不发任何网络请求）。"""
    module, _ = connector_seam._load_open_platform(None)

    assert connector_seam._provides_open_platform(module)
    conn = module.QQOpenPlatformConnection(app_id="a", client_secret="b")
    assert conn.CHANNEL == "open" and conn.mode == "open_platform"
    assert hasattr(conn, "set_inbound_sink") and conn.inbound_sink is None


def test_resolved_names_point_at_the_resolved_open_platform_module():
    """不管这次解析到宿主还是内置，透出的名字必须是同一份、并且是新接口。"""
    expected = {
        "host": connector_seam.HOST_OPEN_PLATFORM_MODULE,
        "vendored": connector_seam.VENDORED_OPEN_PLATFORM_MODULE,
    }[connector_seam.OPEN_PLATFORM_SOURCE]

    assert connector_seam.OPEN_PLATFORM_MODULE == expected
    assert connector_seam.qq_open_plat is connector_seam.open_platform
    assert connector_seam.QQOpenPlatformConnection is connector_seam.open_platform.QQOpenPlatformConnection
    assert connector_seam._provides_open_platform(connector_seam.open_platform)


class _RecordingProvider:
    """只记录调用的假连接器包：用来看工厂把哪种模式交给了它。"""

    def __init__(self):
        self.modes: list[str] = []

    def create_onebot_connection(self, settings_or_reader, *, logger=None, emit_log=None):
        self.modes.append(connector_seam._connection_mode(settings_or_reader))
        return "from-provider"


_OPEN_PLATFORM_SETTINGS = {
    "qq_connection_mode": "open_platform",
    "qq_open_app_id": "a",
    "qq_open_client_secret": "b",
}


@pytest.mark.parametrize("as_reader", [False, True])
def test_factory_builds_the_builtin_adapter_when_the_host_one_is_old(monkeypatch, as_reader):
    """组合"OneBot 用宿主的、开放平台用内置的"：宿主工厂造的是旧适配器，不能让它造。"""
    host = _RecordingProvider()
    monkeypatch.setattr(connector_seam, "_provider", host)
    monkeypatch.setattr(connector_seam, "OPEN_PLATFORM_SOURCE", "vendored")
    builtin = importlib.import_module(connector_seam.VENDORED_OPEN_PLATFORM_MODULE)
    settings = (lambda: _OPEN_PLATFORM_SETTINGS) if as_reader else _OPEN_PLATFORM_SETTINGS

    conn = connector_seam.create_onebot_connection(settings)

    assert type(conn) is builtin.QQOpenPlatformConnection
    assert host.modes == []


def test_factory_leaves_onebot_modes_to_the_host_when_only_open_platform_is_builtin(monkeypatch):
    host = _RecordingProvider()
    monkeypatch.setattr(connector_seam, "_provider", host)
    monkeypatch.setattr(connector_seam, "OPEN_PLATFORM_SOURCE", "vendored")

    for mode in ("napcat", "napcat_forward", ""):
        assert connector_seam.create_onebot_connection({"qq_connection_mode": mode}) == "from-provider"

    assert host.modes == ["napcat", "napcat_forward", "napcat"]


def test_factory_leaves_open_platform_to_the_host_when_it_has_the_new_api(monkeypatch):
    host = _RecordingProvider()
    monkeypatch.setattr(connector_seam, "_provider", host)
    monkeypatch.setattr(connector_seam, "OPEN_PLATFORM_SOURCE", "host")

    assert connector_seam.create_onebot_connection(_OPEN_PLATFORM_SETTINGS) == "from-provider"
    assert host.modes == ["open_platform"]


# ── 副本里的功能性本地改动必须留痕 ─────────────────────────────────
#
# 2026-09-26 的 4302a9ea 改了副本里的 qq_open_plat.py（5 个 hunk）并新增
# qq_open_platform_media.py，但**没有更新 PROVENANCE.md** —— 于是"副本不是逐字一致"
# 在文档里只剩 lint 那一半，功能性差异零痕迹。重新同步上游时这些改动会**静默**消失
# （不报错）：单聊发图退回只发 `[图片]`、群图上传退回只试旧式直传；
# 而如果连 qq_open_platform_media.py 一起漏拷，`from . import qq_open_platform_media`
# 会让整个副本包 import 失败（宿主没带连接器时插件直接起不来）。
#
# 这三条守卫把"标记"钉成可执行的：标记在、接线在、文件名清单与 PROVENANCE 对得上。

_VENDOR_DIR = pathlib.Path(connector_seam.__file__).resolve().parent / "_vendor" / "connection_onebot"
_PROVENANCE = _VENDOR_DIR / "PROVENANCE.md"

#: 带功能性本地改动的文件 → (标记里必须出现的 commit 短号, 必须还在的接线片段)
LOCAL_PATCHES: dict[str, tuple[str, tuple[str, ...]]] = {
    "qq_open_plat.py": ("4302a9ea", (
        "from . import qq_open_platform_media",
        "qq_open_platform_media.send_private_image(",
        "qq_open_platform_media.upload_image(",
    )),
    "qq_open_platform_media.py": ("4302a9ea", (
        "def upload_image(",
        "def send_private_image(",
    )),
}

#: 标记只认文件头这一段：贴到文件末尾等于没标。
_MARKER_HEAD_LINES = 40


def _vendored_text(name: str) -> str:
    path = _VENDOR_DIR / name
    if not path.is_file():
        pytest.skip(f"_vendor/ 已按 PROVENANCE 的计划拆掉（缺 {name}），本组守卫随之退休")
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(LOCAL_PATCHES))
def test_vendored_local_patches_are_marked_and_kept(name: str):
    commit, anchors = LOCAL_PATCHES[name]
    text = _vendored_text(name)

    head = "\n".join(text.splitlines()[:_MARKER_HEAD_LINES])
    assert "LOCAL-PATCH:" in head, (
        f"{name} 丢了文件头的 LOCAL-PATCH 标记 —— 重新同步上游时这些功能改动会被静默抹掉"
    )
    assert commit in head, f"{name} 的标记里缺 commit 短号 {commit}"

    for anchor in anchors:
        assert anchor in text, (
            f"{name} 里的本地接线不见了: {anchor!r} —— 上游副本覆盖回来时就是这样丢的"
        )


def _fenced_blocks(text: str) -> str:
    """把 ``` 围栏里的内容拼起来 —— 文件名清单写在这里面。"""
    inside = False
    collected: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            collected.append(line)
    return "\n".join(collected)


def test_provenance_lists_every_vendored_file():
    """文件名清单必须与实际一致（当初就是漏了 qq_open_platform_media.py）。

    只在正文里提一句不算：清单是**围栏代码块**里的那张表，核对的是那个。
    """
    text = _PROVENANCE.read_text(encoding="utf-8")
    actual = sorted(p.name for p in _VENDOR_DIR.glob("*.py"))
    listed = _fenced_blocks(text)

    assert actual, "副本目录里一个 .py 都没有 —— 路径解析错了"
    missing = [name for name in actual if name not in listed]
    assert not missing, f"PROVENANCE.md 的清单没登记这些副本文件: {missing}"


def test_provenance_documents_the_local_patch():
    text = _PROVENANCE.read_text(encoding="utf-8")

    assert "LOCAL-PATCH" in text, "PROVENANCE.md 里没有标记约定的说明"
    assert "4302a9ea" in text, "PROVENANCE.md 没记这次功能性改动的出处 commit"
    assert "qq_open_platform_media.py" in text, "PROVENANCE.md 没登记自撰的 media 模块"


def test_provenance_records_which_patches_are_live():
    """差异表必须写出「真机是否生效」+ 真机证据。

    为什么单钉这一条：副本里 6 处改动**只有 media 模块在真机路径上**（运行时连的是宿主
    那份连接器，`reply_delivery_node._send_sticker` 又刻意直调 media 自由函数）。不写清楚
    的话，下一个读这份文档的人会以为 `qq_open_plat.py` 里那些转发是活的，从而在
    "优化掉用不上的代码"时把唯一在役的那部分一起删掉。
    """
    text = _PROVENANCE.read_text(encoding="utf-8")

    assert "真机是否生效" in text, "PROVENANCE.md 的差异表缺少「真机是否生效」一列"
    assert "图片直传上传未拿到 file_info" in text, "没记真机证据：旧式直传已失效"
    assert "图片上传成功(分片)" in text, "没记真机证据：分片上传是活的"
    assert "connector_seam" in text and "host" in text, "没写清运行时装的是宿主那份连接器"
