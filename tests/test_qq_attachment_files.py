"""开放平台入站**非图片附件**：以前直接掉在地上，现在接到文件渲染链路。

图片那半有去处（`prompting._queue_attachment_images` 把 URL 下载成多模态图），
文件那半没有任何消费方 —— `_collect_image_attachments` 只认 `image`/`image_url`。
表现是"对方发了个文件，她只看到空气"，而且**不报错**，只能靠读代码发现。

这组测试钉三件事：

1. `_attachment_files` 的取用规则（只取 file、名字优先平台给的、否则 URL 尾部，
   带 query / 百分号编码都要能还原）；
2. 真的走到 `_fetch_file_content` 那条渲染链路上（文本解码 / 二进制标记）；
3. 渲染出的内容命中黑名单时，消息要像其它路径一样被拦下（不是解析完照发）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply import enrichment as enrichment_mod
from plugin.plugins.qq_auto_reply.enrichment import QQMessageEnricher
from plugin.plugins.qq_auto_reply.message_dispatcher import QQMessageDispatcher


def _enricher() -> QQMessageEnricher:
    return QQMessageEnricher(SimpleNamespace())


# ── 1. 取用规则 ─────────────────────────────────────────────────────

def test_a_file_attachment_becomes_a_renderable_file_entry():
    files = _enricher()._attachment_files({
        "attachments": [
            {"type": "file", "url": "https://cdn.example/a.txt", "name": "报告.txt"},
        ],
    })

    assert files == [{"file_id": "", "name": "报告.txt", "url": "https://cdn.example/a.txt", "busid": 0}]


def test_the_name_falls_back_to_the_url_tail_with_query_and_encoding():
    files = _enricher()._attachment_files({
        "attachments": [{
            "type": "file",
            "url": "https://cdn.example/dir/%E6%97%A5%E5%BF%97.log?sign=abc&t=1",
        }],
    })

    assert len(files) == 1
    # 尾部名要还原百分号编码、去掉 query —— 否则提示词里会出现一串 %E6%97%A5…
    assert files[0]["name"] == "日志.log"


def test_images_are_left_to_the_multimodal_path():
    """图片归多模态附件那条；两边都收就成了"同一张图既进文本又进图片队列"。"""
    files = _enricher()._attachment_files({
        "attachments": [
            {"type": "image", "url": "https://cdn.example/a.png"},
            {"type": "file", "url": "https://cdn.example/b.pdf"},
        ],
    })

    assert [f["url"] for f in files] == ["https://cdn.example/b.pdf"]


@pytest.mark.parametrize(
    "attachments",
    [
        None,
        [],
        "not-a-list",
        [{"type": "file"}],                     # 没 url
        [{"type": "file", "url": "   "}],       # 空 url
        ["not-a-dict"],
        [{"url": "https://cdn.example/c.txt"}],  # 没 type：不猜
    ],
)
def test_nothing_renderable_yields_an_empty_list(attachments):
    assert _enricher()._attachment_files({"attachments": attachments}) == []


def test_a_message_without_attachments_is_a_noop():
    assert _enricher()._attachment_files({}) == []


# ── 2. 真的走渲染链路 ───────────────────────────────────────────────

class _FakeStreamResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status_code = status

    async def aiter_bytes(self):
        yield self._payload


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeHTTPClient:
    """`async with httpx.AsyncClient(...) as cl:` —— 所以要支持异步上下文协议。"""

    def __init__(self, payload: bytes = b"", status: int = 200):
        self._payload = payload
        self._status = status
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        self.urls.append(str(url))
        return _FakeStreamCtx(_FakeStreamResponse(self._payload, self._status))


@pytest.fixture
def fake_download(monkeypatch):
    """把 `_fetch_file_content` 里那个 httpx.AsyncClient 换成假的（不联网）。"""
    holder = {}

    def _install(payload: bytes = b"", status: int = 200):
        client = _FakeHTTPClient(payload, status)
        holder["client"] = client
        monkeypatch.setattr(enrichment_mod.httpx, "AsyncClient", lambda **kw: client)
        return client

    return _install


def test_a_text_file_attachment_lands_in_the_message_content(fake_download):
    http = fake_download("这是一份日志的内容\n第二行".encode("utf-8"))
    enricher = _enricher()
    message = {"content": "看看这个", "raw_message": "看看这个", "message_type": "private"}

    files = enricher._attachment_files({
        "attachments": [{"type": "file", "url": "https://cdn.example/a.txt", "name": "a.txt"}],
    })
    asyncio.run(enricher._fetch_file_content(message, files))

    assert http.urls == ["https://cdn.example/a.txt"]
    assert "[文件 a.txt]" in message["content"]
    assert "这是一份日志的内容" in message["content"]
    assert message["content"].startswith("看看这个"), "原话不能被附件顶掉"


def test_a_binary_file_attachment_is_marked_not_decoded(fake_download):
    fake_download(b"\x00\x01\x02binary")
    enricher = _enricher()
    message = {"content": "给你", "raw_message": "给你", "message_type": "private"}

    asyncio.run(enricher._fetch_file_content(
        message,
        enricher._attachment_files({"attachments": [{"type": "file", "url": "https://cdn.example/x.bin"}]}),
    ))

    assert "二进制" in message["content"]
    assert "\x00" not in message["content"]


# ── 3. 派发层：接上 + 黑名单复核 ────────────────────────────────────

class _RecordingEnricher:
    def __init__(self, *, content_after: str):
        self.calls: list[list[dict]] = []
        self._content_after = content_after

    def _attachment_files(self, message):
        return [{"file_id": "", "name": "a.txt", "url": "https://cdn.example/a.txt", "busid": 0}]

    async def _fetch_file_content(self, message, files):
        self.calls.append(files)
        message["content"] = self._content_after


def _dispatcher(enricher, *, labels=None, emitted=None):
    # 注意别写 `(emitted or [])`：空列表是 falsy，会**新建**一个列表，
    # 于是测试拿到永远为空的记录（这个坑真踩过一次）。
    log_sink = [] if emitted is None else emitted
    plugin = SimpleNamespace(
        enricher=enricher,
        # needs_attention=False → 走"开放平台"那条分支
        qq_client=SimpleNamespace(needs_attention=False),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        _emit_log=lambda level, msg: log_sink.append((level, msg)),
        _qq_settings={"backlog_labels": labels or []},
    )
    return QQMessageDispatcher(plugin)


def test_the_dispatcher_renders_attachment_files():
    enricher = _RecordingEnricher(content_after="看看这个 [文件 a.txt]\n内容")
    dispatcher = _dispatcher(enricher)
    message = {"content": "看看这个"}

    filtered = asyncio.run(dispatcher.enrich_open_platform_attachments(
        message, label_defs=[], raw_content="看看这个",
    ))

    assert filtered is False
    assert len(enricher.calls) == 1
    assert "内容" in message["content"]


def test_rendered_attachment_content_can_be_blacklisted():
    """解析出来的内容也要过黑名单：附件不是绕过滤器的旁路。"""
    enricher = _RecordingEnricher(content_after="看看这个 [文件 a.txt]\n崩溃了")
    emitted: list = []
    dispatcher = _dispatcher(enricher, emitted=emitted)
    message = {"content": "看看这个"}
    labels = [{"id": "blacklist", "priority": -10, "keywords": ["崩溃"]}]

    filtered = asyncio.run(dispatcher.enrich_open_platform_attachments(
        message, label_defs=labels, raw_content="看看这个",
    ))

    assert filtered is True, "命中黑名单必须拦下整条消息"
    assert any("附件" in msg for _level, msg in emitted)


def test_unchanged_content_is_not_reported_as_parsed():
    """渲染没改动内容时不许谎报"已解析 1 个附件"（失败要看得出来）。"""
    enricher = _RecordingEnricher(content_after="看看这个")
    emitted: list = []
    dispatcher = _dispatcher(enricher, emitted=emitted)

    filtered = asyncio.run(dispatcher.enrich_open_platform_attachments(
        {"content": "看看这个"}, label_defs=[], raw_content="看看这个",
    ))

    assert filtered is False
    assert emitted == []


def test_a_plugin_without_attachments_support_is_a_noop():
    """老 enricher（没有 `_attachment_files`）不许把消息管线带下去。"""
    dispatcher = _dispatcher(SimpleNamespace())
    assert asyncio.run(dispatcher.enrich_open_platform_attachments(
        {"content": "hi"}, label_defs=[], raw_content="hi",
    )) is False


# ── 4. 接线：`handle_message` 里那一段真的被走到 ──────────────────────
#
# 上面几条测的是 `enrich_open_platform_attachments` 本身。**接线**得单独钉：
# 把 `handle_message` 里那个 `elif` 分支删掉，上面全绿 —— 附件又变回"掉在地上"，
# 而这是同一类静默失效的复发。这条是变异测试逼出来的（见
# `verify_open_platform_media_fail_to_pass.py` 的 "派发层不再处理…" 那一项，
# 补这条之前它是全绿）。

def test_handle_message_reaches_the_attachment_rendering_branch():
    """走真实入口：渲染完的文件内容命中黑名单 → 整条消息被拦下且**留下痕迹**。

    用一个"渲染后才命中黑名单"的消息，是为了让 `handle_message` 在附件那一段
    之后立刻 return —— 这样测试只依赖到那一段为止的插件能力，不用把整条管线
    都搭出来（后面还有 backlog / 注意力 / LLM，与这条无关）。
    """
    enricher = _RecordingEnricher(content_after="看看这个 [文件 a.txt]\n崩溃了")
    emitted: list = []
    labels = [{"id": "blacklist", "priority": -10, "keywords": ["崩溃"]}]
    dispatcher = _dispatcher(enricher, labels=labels, emitted=emitted)
    message = {
        "message_type": "group",
        "group_id": "G1",
        "user_id": "U1",
        "content": "看看这个",
        "raw_message": "看看这个",
        "attachments": [{"type": "file", "url": "https://cdn.example/a.txt", "name": "a.txt"}],
    }

    asyncio.run(dispatcher.handle_message(message))

    assert len(enricher.calls) == 1, "handle_message 没有走到附件渲染那一段"
    assert any("黑名单过滤(附件解析后)" in msg for _level, msg in emitted), (
        f"渲染后的内容命中黑名单却没被拦下: {emitted!r}"
    )
