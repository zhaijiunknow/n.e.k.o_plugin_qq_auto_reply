"""写 NapCat 的 OneBot v11 网络配置。

NapCat 把每个登录账号的网络配置放在 ``config/onebot11_<uin>.json`` —— **文件名带 QQ 号**，
而 QQ 号在扫码登录前拿不到。所以"什么时候写"由调用方决定（预置：先问号再启动；或后置：
登录后补写再重启），本模块只负责"写对"。

写入一律**读-合并-写**，且只认领 ``name == CALLBACK_NAME`` 那一条：用户的 http 服务、
其它 WS 条目、插件配置、超时设置都原样保留。一次部署动作不该把用户既有的网络配置抹掉
——那些条目可能正连着他自己的别的服务。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


#: 我们那条连接在 NapCat 配置里的名字。合并按名字认领，所以重复部署是幂等的。
#: 反向条目和正向条目各在自己数组里用这个名字，互不冲突。
CALLBACK_NAME = "neko"

#: 插件反向 WS 服务端监听的路径（与静态引导里给用户的示例一致）。
REVERSE_PATH = "/ws"

#: 反向条目的默认拨号地址：插件默认监听 0.0.0.0:6199，NapCat 要拨回环。
DEFAULT_REVERSE_DIAL = "ws://127.0.0.1:6199/ws"
#: 正向条目的默认监听地址：NapCat 开 WS 服务器等我们拨。
DEFAULT_FORWARD_HOST = "127.0.0.1"
DEFAULT_FORWARD_PORT = 3001

#: 反向配置里 NapCat 作为**客户端**连我们（websocketClients）时用的字段。
_CLIENT_FIELDS = (
    "enable", "name", "url", "reportSelfMessage", "messagePostFormat",
    "token", "debug", "heartInterval", "reconnectInterval", "verifyCertificate",
)
#: 正向配置里 NapCat 开**服务器**等我们连（websocketServers）时用的字段。
_SERVER_FIELDS = (
    "enable", "name", "host", "port", "reportSelfMessage", "enableForcePushEvent",
    "messagePostFormat", "token", "debug", "heartInterval",
)

#: 新建配置文件时的骨架 —— 与 NapCat 自己生成的结构一致，让它看起来像原生配置。
_SKELETON: dict[str, Any] = {
    "network": {
        "httpServers": [],
        "httpSseServers": [],
        "httpClients": [],
        "websocketServers": [],
        "websocketClients": [],
        "plugins": [],
    },
    "musicSignUrl": "",
    "enableLocalFile2Url": False,
    "parseMultMsg": False,
    "imageDownloadProxy": "",
    "timeout": {
        "baseTimeout": 10000,
        "uploadSpeedKBps": 256,
        "downloadSpeedKBps": 256,
        "maxTimeout": 1800000,
    },
}

_ONE_BOT_RE = re.compile(r"^onebot11_(.+)\.json$")


# ── 路径 ────────────────────────────────────────────────────

def config_dir_of(napcat_dir: Path | str) -> Path:
    """NapCat 的配置目录。"""
    return Path(napcat_dir) / "config"


def onebot_config_path(napcat_dir: Path | str, uin: str) -> Path:
    """``config/onebot11_<uin>.json``。"""
    return config_dir_of(napcat_dir) / f"onebot11_{str(uin).strip()}.json"


def list_onebot_configs(napcat_dir: Path | str) -> list[Path]:
    """列出已存在的 OneBot 配置，按 uin 排序。

    给"登录后补写"那条路用：扫码登录成功后 NapCat 会自己建出 ``onebot11_<uin>.json``，
    我们靠扫目录发现新出现的是哪个号。
    """
    d = config_dir_of(napcat_dir)
    if not d.is_dir():
        return []
    found = [p for p in sorted(d.glob("onebot11_*.json")) if p.is_file()]
    return found


def uin_from_path(path: Path | str) -> str:
    """从 ``onebot11_<uin>.json`` 取出 uin；不匹配返回空串。"""
    m = _ONE_BOT_RE.match(Path(path).name)
    return (m.group(1) if m else "").strip()


# ── URL ─────────────────────────────────────────────────────

def host_port_of(url: str, *, default_host: str = DEFAULT_FORWARD_HOST,
                 default_port: int = DEFAULT_FORWARD_PORT) -> tuple[str, int]:
    """从 URL 取出 ``(host, port)``，缺省/不可解析时用默认值。

    正向模式下 ``onebot_url`` 填的就是 NapCat 的服务端地址，正向条目要的正是它的
    host/port 拆解 —— 通配 host 同样换成回环（``0.0.0.0`` 不是可拨/可连的地址）。
    """
    raw = str(url or "").strip()
    if not raw:
        return default_host, default_port
    if "://" not in raw:
        raw = "ws://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return default_host, default_port
    host = (parts.hostname or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = default_host
    try:
        port = int(parts.port or default_port)
    except (TypeError, ValueError):
        port = default_port
    return host, port


def dial_url(listen_url: str, *, default_host: str = "127.0.0.1",
             default_port: int = 6199) -> str:
    """把"监听地址"换算成"NapCat 该拨的地址"。

    插件默认监听 ``ws://0.0.0.0:6199``，但 ``0.0.0.0`` 是通配监听地址、**不可拨号**；
    通配地址与空 host 一律换成回环。路径缺省补 ``/ws``（插件反向服务端的路径）。
    """
    raw = str(listen_url or "").strip()
    if not raw:
        return f"ws://{default_host}:{default_port}{REVERSE_PATH}"
    if "://" not in raw:
        raw = "ws://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = default_host
    port = parts.port or default_port
    path = (parts.path or "").strip()
    if path in ("", "/"):
        path = REVERSE_PATH
    return urlunsplit(("ws", f"{host}:{port}", path, "", ""))


# ── 条目构造 ────────────────────────────────────────────────

def build_client_entry(url: str, token: str = "") -> dict[str, Any]:
    """反向：NapCat 作为 WS **客户端**主动连插件。"""
    return {
        "enable": True,
        "name": CALLBACK_NAME,
        "url": str(url or ""),
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": str(token or ""),
        "debug": False,
        "heartInterval": 30000,
        "reconnectInterval": 30000,
        "verifyCertificate": True,
    }


def build_server_entry(host: str, port: int, token: str = "") -> dict[str, Any]:
    """正向：NapCat 开 WS **服务器**，插件主动拨过去。"""
    return {
        "enable": True,
        "name": CALLBACK_NAME,
        "host": str(host or "127.0.0.1"),
        "port": int(port or 3001),
        "reportSelfMessage": False,
        "enableForcePushEvent": True,
        "messagePostFormat": "array",
        "token": str(token or ""),
        "debug": False,
        "heartInterval": 30000,
    }


# ── 合并与写入 ──────────────────────────────────────────────

def _merge_entry(entries: Iterable[Any], entry: dict[str, Any],
                 fields: tuple[str, ...]) -> list[Any]:
    """按 ``name`` 认领同一条目并就地更新，找不到就追加。

    保留用户在同名条目上多写的键（只覆盖我们关心的字段），也保留其它条目不动。
    """
    out: list[Any] = []
    replaced = False
    for item in entries:
        if isinstance(item, dict) and str(item.get("name") or "") == CALLBACK_NAME:
            if not replaced:
                merged = dict(item)
                for k in fields:
                    if k in entry:
                        merged[k] = entry[k]
                out.append(merged)
                replaced = True
            # 同名重复条目只认领第一条，其余原样留下（用户自己复制的，不该被我们吃掉）
            else:
                out.append(item)
        else:
            out.append(item)
    if not replaced:
        out.append(entry)
    return out


def merge(config: dict[str, Any], *, reverse_url: str, forward_host: str,
          forward_port: int, token: str = "") -> dict[str, Any]:
    """把**反向和正向两条都**写进去，返回新配置（不改原对象）。

    两条都写是有意的：NapCat 同时具备「WS 客户端（连我们）」与「WS 服务器（我们连它）」
    两种角色。两条都配好后，用户在 N.E.K.O 里切模式**不需要重配 NapCat、也不用重启
    它** —— 只写当前模式那一条的话，每次切模式都得把整套部署重跑一遍。

    只认领 ``name == CALLBACK_NAME`` 的条目；用户自己加的其它条目一律原样保留
    （那可能连着他自己的别的服务）。
    """
    import copy

    # 空 dict 也算"没有配置"：``load()`` 对不存在的文件返回 ``{}``，而 ``{}`` 是 dict，
    # 若只判 isinstance 就会拿空对象当底 —— 新建出来的文件只有 network 一个顶层键，
    # 不像 NapCat 自己生成的结构。非空配置则一律原样作底，不注入用户没有的键。
    base = config if (isinstance(config, dict) and config) else _SKELETON
    out = copy.deepcopy(base)
    network = out.get("network")
    if not isinstance(network, dict):
        network = copy.deepcopy(_SKELETON["network"])
        out["network"] = network

    network["websocketClients"] = _merge_entry(
        list(network.get("websocketClients") or []),
        build_client_entry(reverse_url, token), _CLIENT_FIELDS,
    )
    network["websocketServers"] = _merge_entry(
        list(network.get("websocketServers") or []),
        build_server_entry(forward_host, forward_port, token), _SERVER_FIELDS,
    )
    return out


def load(path: Path | str) -> dict[str, Any]:
    """读配置；文件不存在或不可解析时返回空骨架（不抛）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write(path: Path | str, config: dict[str, Any]) -> None:
    """原子写：先写同目录临时文件再 ``os.replace``。

    直接覆盖写的话，正好在此时读配置的 NapCat 会读到半截 JSON 并把它当成损坏配置。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def apply(path: Path | str, *, reverse_url: str, forward_host: str,
          forward_port: int, token: str = "") -> dict[str, Any]:
    """读 → 合并 → 写，返回最终配置。"""
    merged = merge(load(path), reverse_url=reverse_url,
                   forward_host=forward_host, forward_port=forward_port, token=token)
    write(path, merged)
    return merged
