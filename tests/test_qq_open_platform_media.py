"""QQ 开放平台富媒体：图片上传（两条协议）与单聊发图。

这轮补的三件事之一。以前开放平台**只有群聊能发图**，单聊那条路把图写成
`[图片]` 三个字发出去 —— 而官方 v2 明确有「单聊富媒体上传」
（`POST /v2/users/{user_openid}/files`），也就是"两边能力差一截"纯粹是本仓库
只实现了群那一半。

两条协议并存这件事是**官方文档现状**逼出来的：

* 本仓库群聊用的是**旧式直传**（`{file_type, file_name, file_size, mime_type}`
  → `upload_url` → PUT → `file_info`）；
* 当前 wiki 只列 **URL 上传**（给平台一个 http 地址）与 **分片上传**
  （`upload_prepare` → 逐片 PUT + `upload_part_finish` → 带 `upload_id` 合并）。

旧式那条到底还算不算数，本地没有开放平台凭据**验证不了**，所以实现是"两条都试、
谁成谁赢、日志写清哪条成的"。这些测试钉的就是这个顺序与各条请求的**形状**：
形状错了，真机上只会得到一句"上传未拿到 file_info"，而那是静默降级的开始。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply import connector_seam

MEDIA = connector_seam.open_platform_media


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        return None


class _FakeHTTP:
    """按 ``responder(method, url, body)`` 出响应，并记录每一次调用。"""

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[tuple[str, str, object]] = []

    async def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json))
        return _Response(self._responder("POST", url, json))

    async def put(self, url, content=None, headers=None):
        self.calls.append(("PUT", url, content))
        return _Response(self._responder("PUT", url, content))

    def posts(self):
        return [(url, body) for method, url, body in self.calls if method == "POST"]

    def puts(self):
        return [(url, body) for method, url, body in self.calls if method == "PUT"]


class _Conn:
    """只带富媒体函数需要的那几个成员（与漂移守卫测的集合一致）。"""

    CHANNEL = "open"
    mode = "open_platform"

    def __init__(self, responder):
        self._http = _FakeHTTP(responder)
        self._API_BASE = "https://api.example"
        self.logger = SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None,
        )
        self.token_calls = 0
        self.recorded: list[str] = []

    async def _ensure_token(self):
        self.token_calls += 1

    def _auth_headers(self):
        return {"Authorization": "QQBot fake"}

    def record_sent_message_id(self, mid):
        self.recorded.append(str(mid))


def _legacy_ok(**_):
    """旧式直传成功。"""
    return {"upload_url": "https://cos.example/put/1", "file_info": "FI-legacy"}


def _legacy_then_nothing(method, url, body):
    """旧式直传拿不到 upload_url；分片那条一切正常（两片，覆盖整个 16 字节文件）。"""
    if method == "PUT":
        return {"file_info": "FI-legacy"}
    if url.endswith("/upload_prepare"):
        return {
            "upload_id": "upload_1",
            "block_size": "8",
            "parts": [
                {"index": 0, "presigned_url": "https://cos.example/part/0", "block_size": "8"},
                {"index": 1, "presigned_url": "https://cos.example/part/1", "block_size": "8"},
            ],
        }
    if url.endswith("/upload_part_finish"):
        return {}
    if url.endswith("/files") and body.get("upload_id"):
        return {"file_info": "FI-chunked"}
    if url.endswith("/files"):
        return {}  # 旧式直传：不吐 upload_url
    return {}


def _documented_chunked(method, url, body):
    if method == "POST" and url.endswith("/upload_prepare"):
        return {
            "upload_id": "upload_1",
            "block_size": "8",
            "parts": [{"index": 0, "presigned_url": "https://cos.example/part/0", "block_size": "8"}],
        }
    if method == "POST" and url.endswith("/upload_part_finish"):
        return {}
    if method == "PUT":
        return {}
    if method == "POST" and url.endswith("/files"):
        assert body.get("upload_id") == "upload_1", "合并那一步必须带 upload_id"
        return {"file_info": "FI-chunked"}
    return {}


# ── URL 上传：平台自己去下载，本地不碰字节 ──────────────────────────────

def test_a_remote_url_goes_through_the_documented_url_upload():
    def responder(method, url, body):
        if url.endswith("/messages"):
            return {"id": "MID-1"}
        return {"file_info": "FI-url"}

    conn = _Conn(responder)

    message_id = _run(MEDIA.send_private_image(
        conn, "USER1", "https://cdn.example/a.png", content="给你看",
    ))

    posts = conn._http.posts()
    assert posts[0] == (
        "https://api.example/v2/users/USER1/files",
        {"file_type": 1, "url": "https://cdn.example/a.png", "srv_send_msg": False},
    ), f"URL 上传的请求体不对: {posts[0]!r}"
    assert posts[1][0] == "https://api.example/v2/users/USER1/messages"
    assert posts[1][1] == {"msg_type": 7, "media": {"file_info": "FI-url"}, "content": "给你看"}
    # 没有字节要传：URL 上传不该出现 PUT
    assert conn._http.puts() == []
    assert message_id == "MID-1"
    # 回执要记账：上层用 record_sent_message_id 认"这条是我发的"（引用/反应靠它）
    assert conn.recorded == ["MID-1"]


def test_the_scope_is_users_for_private_and_groups_for_group():
    """平台把两个上传入口分开：单聊接口传的只能发单聊（文档原话），反之亦然。"""
    private_conn = _Conn(lambda method, url, body: {"file_info": "FI"})
    _run(MEDIA.send_private_image(private_conn, "U1", "https://cdn.example/a.png"))
    assert private_conn._http.posts()[0][0].startswith("https://api.example/v2/users/U1/")

    group_conn = _Conn(lambda method, url, body: {"file_info": "FI"})
    _run(MEDIA.upload_image(group_conn, scope="groups", owner_id="G1", source="https://cdn.example/a.png"))
    assert group_conn._http.posts()[0][0].startswith("https://api.example/v2/groups/G1/")


# ── 本地文件：旧式直传优先，失败再走文档的分片 ──────────────────────────

def test_a_local_file_tries_the_legacy_direct_upload_first(tmp_path):
    sticker = tmp_path / "a.png"
    sticker.write_bytes(b"x" * 32)
    conn = _Conn(lambda method, url, body: _legacy_ok() if method == "POST" else {"file_info": "FI-legacy"})

    file_info = _run(MEDIA.upload_image(conn, scope="groups", owner_id="G1", source=str(sticker)))

    assert file_info == "FI-legacy"
    urls = [url for url, _ in conn._http.posts()]
    assert urls[0].endswith("/v2/groups/G1/files")
    assert not any(u.endswith("/upload_prepare") for u in urls), "旧式直传成功时不该再试分片"
    assert conn._http.puts()[0][0] == "https://cos.example/put/1"


def test_a_local_file_falls_back_to_the_documented_chunked_upload(tmp_path):
    sticker = tmp_path / "a.png"
    sticker.write_bytes(b"y" * 16)
    conn = _Conn(_legacy_then_nothing)

    file_info = _run(MEDIA.upload_image(conn, scope="users", owner_id="U1", source=str(sticker)))

    assert file_info == "FI-chunked", "旧式直传拿不到 file_info 时必须走分片那条"
    urls = [url for url, _ in conn._http.posts()]
    assert urls[0].endswith("/v2/users/U1/files")           # 旧式直传
    assert urls[1].endswith("/v2/users/U1/upload_prepare")  # 分片第一步
    assert urls[2].endswith("/v2/users/U1/upload_part_finish")
    assert urls[3].endswith("/v2/users/U1/upload_part_finish")
    assert urls[4].endswith("/v2/users/U1/files")           # 带 upload_id 合并
    # 预上传要的四个校验值都在（文档列的是 file_size/md5/sha1/md5_10m）
    prepare = conn._http.posts()[1][1]
    assert set(prepare) == {"file_type", "file_size", "file_name", "md5", "sha1", "md5_10m"}
    assert prepare["file_size"] == "16"
    # 每片 PUT 的字节数要与 part_finish 报的 block_size 一致（服务端按这个校验），
    # 且两片加满整个文件 —— 少传一片就是"上传了一个残缺的文件还说成功"。
    put_sizes = [len(body) for _, body in conn._http.puts()]
    finish_sizes = [
        body["block_size"] for url, body in conn._http.posts() if url.endswith("/upload_part_finish")
    ]
    assert put_sizes == [8, 8], f"分片大小不对: {put_sizes!r}"
    assert finish_sizes == ["8", "8"] == [str(n) for n in put_sizes]


def test_a_partial_part_list_is_refused_instead_of_merging_a_truncated_file(tmp_path):
    """分片列表没覆盖完整个文件时**不许合并**。

    服务端的 ``parts`` 是它按我们报的 ``file_size`` 算的，正常不会缺片；但一旦缺了，
    合并上去就是一个残缺文件，而返回的 ``file_info`` 看起来完全正常 —— 静默数据损坏。
    宁可这次不发图。
    """
    sticker = tmp_path / "a.png"
    sticker.write_bytes(b"y" * 16)
    conn = _Conn(lambda method, url, body: _truncated_parts(method, url, body))

    assert _run(MEDIA.upload_image(conn, scope="users", owner_id="U1", source=str(sticker))) == ""
    assert not any(
        url.endswith("/files") and body.get("upload_id") for url, body in conn._http.posts()
    ), "缺片时不该走到合并那一步"


def _truncated_parts(method, url, body):
    if method == "PUT":
        return {"file_info": "FI-legacy"}
    if url.endswith("/upload_prepare"):
        return {
            "upload_id": "upload_1",
            "block_size": "8",
            "parts": [{"index": 0, "presigned_url": "https://cos.example/part/0", "block_size": "8"}],
        }
    if url.endswith("/upload_part_finish"):
        return {}
    return {}


# ── 失败与拒绝：一律返回空，让调用方降级 ────────────────────────────────

def test_upload_failure_returns_none_so_the_caller_can_fall_back_to_text(tmp_path):
    sticker = tmp_path / "a.png"
    sticker.write_bytes(b"z" * 8)
    conn = _Conn(lambda method, url, body: {})

    assert _run(MEDIA.send_private_image(conn, "U1", str(sticker))) is None
    # 上传没成，就不该往 /messages 发那条 msg_type=7（否则会发出一条空消息）
    assert not any(url.endswith("/messages") for url, _ in conn._http.posts())


def test_a_missing_local_file_never_touches_the_network(tmp_path):
    conn = _Conn(lambda method, url, body: {"file_info": "FI"})

    assert _run(MEDIA.send_private_image(conn, "U1", str(tmp_path / "nope.png"))) is None
    assert conn._http.calls == []
    assert conn.token_calls == 0


def test_an_oversized_image_is_refused_before_uploading(tmp_path, monkeypatch):
    """超过图片软限制就不传：平台会把"图片"降级成"文件"类型，那是偷偷改语义。"""
    sticker = tmp_path / "big.png"
    sticker.write_bytes(b"b" * 64)
    monkeypatch.setattr(MEDIA, "MAX_IMAGE_BYTES", 32)
    conn = _Conn(lambda method, url, body: {"file_info": "FI"})

    assert _run(MEDIA.upload_image(conn, scope="groups", owner_id="G1", source=str(sticker))) == ""
    assert conn._http.calls == []


def test_a_token_failure_does_not_raise(tmp_path):
    sticker = tmp_path / "a.png"
    sticker.write_bytes(b"c" * 8)

    class _Boom(_Conn):
        async def _ensure_token(self):
            raise RuntimeError("token 服务不可用")

    conn = _Boom(lambda method, url, body: {"file_info": "FI"})
    assert _run(MEDIA.upload_image(conn, scope="groups", owner_id="G1", source=str(sticker))) == ""


# ── 漂移守卫：这些自由函数依赖的连接成员 ────────────────────────────────

def test_the_media_helpers_members_exist_on_the_resolved_connector():
    """富媒体流程只依赖这几个成员，而它要同时服务宿主副本与插件副本。

    宿主改了名字（或某一版没有这些成员）时，这里必须红 —— 否则症状是
    "开放平台突然发不出图了"，而日志里只有一句上传失败。

    ``_http`` 是**实例属性**（``__init__`` 里赋值），所以只查类属性会漏掉它 ——
    这里连实例一起查。构造一个真实例不发任何网络请求。
    """
    required = ("_http", "_API_BASE", "_ensure_token", "_auth_headers", "record_sent_message_id")
    cls = connector_seam.QQOpenPlatformConnection
    instance = cls(app_id="a", client_secret="b")
    missing = [name for name in required if not hasattr(instance, name)]
    assert not missing, f"{connector_seam.CONNECTOR_MODULE} 的连接缺了这些成员: {missing}"


def test_the_media_module_is_resolved_from_the_seam():
    assert hasattr(MEDIA, "is_open_platform") and hasattr(MEDIA, "send_private_image")
    # 只认开放平台：OneBot 连接不能被这条流程接管
    assert MEDIA.is_open_platform(SimpleNamespace(CHANNEL="open"))
    assert MEDIA.is_open_platform(SimpleNamespace(mode="open_platform"))
    assert not MEDIA.is_open_platform(SimpleNamespace(CHANNEL="onebot", mode="napcat"))
    assert not MEDIA.is_open_platform(object())


def _run(coro):
    """跑一遍协程；所有 ``{...}/messages`` 的 POST 都回一个消息 id。"""
    import asyncio

    return asyncio.run(coro)
