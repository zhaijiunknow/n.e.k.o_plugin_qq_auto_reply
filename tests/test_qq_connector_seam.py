"""``connector_seam`` 的解析行为与漂移守卫。

连接器在两种宿主上都要能跑：有 PR #2996 的宿主走 ``utils.connection.onebot``，
没有的走 ``_vendor`` 里的副本。这里盯住三件事：

1. 宿主真的提供了连接器时，**宿主优先**；
2. 宿主没提供（缺包，**或者**只剩一个空目录）时，回退副本；
3. 两边都在时，``OneBotConnector`` 协议的成员集合必须一致 —— 副本与宿主漂移了就要红。
   这是 PR 合并后拆掉 ``_vendor/`` 时的安全网。
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest
from plugin.plugins.qq_auto_reply import connector_seam

#: 保证导入不出来的宿主包名（没有对应的父包）。
_MISSING_HOST = "no_such_host_package_xyz.connection"


def _fake_connector_package() -> ModuleType:
    """一个"看起来像"宿主连接器包的假模块：9 个必需属性齐全。"""
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
