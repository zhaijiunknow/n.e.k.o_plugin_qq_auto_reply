"""两条 WS 连接的 ``name`` 必须是**不同的** id。

依据（NapCat 本体 ``NapCat.Shell/napcat.mjs``）::

    registerAdapter(e) { this.adapters.set(e.name, e); }
    findSomeAdapter(e) { return this.adapters.get(e); }

    async InitOneBot() {
      ...
      for (const i of n.network.websocketServers)
        i.enable && this.networkManager.registerAdapter(new gO(i.name, i, ...));
      for (const i of n.network.websocketClients)
        i.enable && this.networkManager.registerAdapter(new pO(i.name, i, ...));
      await this.networkManager.openAllAdapters();
    }

``name`` 不是标签，是 **adapter id**：所有 adapter 类型（httpServers / httpSseServers /
httpClients / websocketServers / websocketClients / plugin_manager）共用**同一个 Map**，
key 就是它。而 ``InitOneBot`` 先注册 servers、再注册 clients —— 两条同名时**后注册的
把先注册的顶掉**，被顶掉的那个连 ``open()`` 都不会被调用（``openAllAdapters()`` 只遍历
Map 里的幸存者）。

老版本两条都写 ``"neko"``，注释还写着"各在自己数组里，互不冲突"。于是正向那条 WS
服务器（默认 ``127.0.0.1:3001``）从部署那一刻起就没被打开过；因为默认模式走反向、
反向恰好是后注册的那个，所以故障一直藏着，直到有人真的切正向模式。

**这是源码级断言**，不是真起一个 NapCat —— 起不划算，而契约本身是纯字符串。
"""

from __future__ import annotations

from plugin.plugins.qq_auto_reply import napcat_onebot_config as cfg

KW = dict(
    reverse_url="ws://127.0.0.1:6199/ws",
    forward_host="127.0.0.1",
    forward_port=3001,
    token="test-token",
)


def _names(entry_list):
    return [str(e.get("name") or "") for e in entry_list]


def test_the_two_names_are_different():
    """常量层面就得分开，否则下面所有写入都会撞。"""
    assert cfg.CALLBACK_NAME
    assert cfg.FORWARD_NAME
    assert cfg.CALLBACK_NAME != cfg.FORWARD_NAME


def test_fresh_config_gives_napcat_two_distinct_adapter_ids():
    merged = cfg.merge({}, **KW)
    network = merged["network"]
    servers = _names(network["websocketServers"])
    clients = _names(network["websocketClients"])

    assert servers == [cfg.FORWARD_NAME]
    assert clients == [cfg.CALLBACK_NAME]
    # NapCat 的 Map 是所有 adapter 类型共用的，id 必须在**两数组之间**也唯一
    assert len(set(servers + clients)) == len(servers) + len(clients)


def test_legacy_same_name_config_is_healed_in_place():
    """存量安装：两边都叫 CALLBACK_NAME → 服务器条目就地改名，不新增一条。"""
    legacy = {"network": {
        "websocketServers": [
            {"enable": True, "name": cfg.CALLBACK_NAME, "host": "10.0.0.9", "port": 6000},
        ],
        "websocketClients": [
            {"enable": True, "name": cfg.CALLBACK_NAME, "url": "ws://old:1"},
        ],
    }}

    merged = cfg.merge(legacy, **KW)
    network = merged["network"]

    assert _names(network["websocketServers"]) == [cfg.FORWARD_NAME]
    assert _names(network["websocketClients"]) == [cfg.CALLBACK_NAME]
    # 我们关心的字段被刷新成当前设置
    assert network["websocketServers"][0]["port"] == 3001
    assert network["websocketServers"][0]["host"] == "127.0.0.1"


def test_merge_does_not_mutate_the_input():
    legacy = {"network": {"websocketServers": [{"name": cfg.CALLBACK_NAME}],
                          "websocketClients": []}}
    merged = cfg.merge(legacy, **KW)
    assert merged is not legacy
    assert legacy["network"]["websocketServers"][0]["name"] == cfg.CALLBACK_NAME


def test_our_duplicate_entries_collapse_to_one():
    """我们自己的同名重复条目必须塌缩 —— 留一条等于把 id 又撞回去。"""
    config = {"network": {
        "websocketServers": [
            {"name": cfg.CALLBACK_NAME},
            {"name": cfg.CALLBACK_NAME},
            {"name": cfg.FORWARD_NAME},
            {"name": "user-own-server"},
        ],
        "websocketClients": [
            {"name": cfg.CALLBACK_NAME},
            {"name": cfg.CALLBACK_NAME},
        ],
    }}

    merged = cfg.merge(config, **KW)

    assert _names(merged["network"]["websocketServers"]) == [
        cfg.FORWARD_NAME, "user-own-server",
    ]
    assert _names(merged["network"]["websocketClients"]) == [cfg.CALLBACK_NAME]


def test_other_entries_are_left_alone():
    """用户自己的条目（哪怕和我们的端口冲突）一律不碰。"""
    config = {"network": {
        "websocketServers": [{"name": "mine", "host": "h", "port": 1, "keep": "yes"}],
        "websocketClients": [{"name": "mine2", "url": "ws://u"}],
        "httpServers": [{"name": "h", "port": 8080}],
    }}

    merged = cfg.merge(config, **KW)

    assert {"mine", cfg.FORWARD_NAME} == set(_names(merged["network"]["websocketServers"]))
    assert {"mine2", cfg.CALLBACK_NAME} == set(_names(merged["network"]["websocketClients"]))
    mine = [e for e in merged["network"]["websocketServers"] if e["name"] == "mine"][0]
    assert mine == {"name": "mine", "host": "h", "port": 1, "keep": "yes"}
    assert merged["network"]["httpServers"] == [{"name": "h", "port": 8080}]


def test_merge_is_idempotent():
    """重复部署（每次启动都会重写）不得越写越多。"""
    once = cfg.merge({}, **KW)
    twice = cfg.merge(once, **KW)
    assert twice == once


def test_round_trip_through_disk_keeps_the_ids_apart(tmp_path):
    path = cfg.onebot_config_path(tmp_path, "10001")
    cfg.apply(path, **KW)
    reloaded = cfg.load(path)
    cfg.apply(path, **KW)

    assert _names(reloaded["network"]["websocketServers"]) == [cfg.FORWARD_NAME]
    assert _names(reloaded["network"]["websocketClients"]) == [cfg.CALLBACK_NAME]
    assert cfg.load(path) == reloaded
