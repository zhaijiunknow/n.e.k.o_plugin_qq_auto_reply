"""A private video link is a request to watch, even when the model calls no tools."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply import video_tool_service as video
from plugin.plugins.qq_auto_reply.companion_context_service import consent_enabled
from plugin.plugins.qq_auto_reply.pipeline_models import SYNTHETIC_SOURCE_KINDS
from plugin.plugins.qq_auto_reply.reply_generation_service import AIMessage, QQReplyGenerationService, SystemMessage
from plugin.plugins.qq_auto_reply.video_preflight import (
    ensure_private_video_analysis,
    private_video_source,
    video_preflight_instruction,
)
from test_qq_plugin_tool_bridge import _candidate, _plugin, _plugins_payload, _refresh

URL = "https://www.bilibili.com/video/BV1xx411c7mD"
OVERVIEW = "已分析 0–20 秒的抽样画面：一只猫在沙发上玩耍；没有分析声音。"


def context(**kw):
    args = dict(message=URL, prompt_message=URL, sender_id="u1", permission_level="admin",
                is_group=False, group_facing=False, group_id=None, source_kind="incoming_private",
                use_memory_context=False, attachments=None, system_prompt="CORE", recalled_memory_text="",
                core_memory_text="", consent_snapshot={}, traces=[])
    args.update(kw)
    return NS(**args)


@pytest.fixture
async def setup(monkeypatch):
    p = _plugin(tiers={"video_reader": "admin"},
                plugins_result=_plugins_payload(_candidate("video_reader", ("prepare_for_qq",))))
    await _refresh(p.plugin_tool_service)
    adapter = AsyncMock(return_value=OVERVIEW)
    monkeypatch.setattr(video, "analyze_qq_video", adapter)
    p._queue_attachment_images = AsyncMock(return_value=0)
    p._wait_session_response_complete = AsyncMock(return_value=True)
    p._ai_turn_timeout_seconds = 1
    service = QQReplyGenerationService(p)
    service._arm_turn_tools = AsyncMock(return_value=(False, False, False))
    return p, adapter, service


@pytest.mark.parametrize("message,expected", [
    (URL, URL), (f"[分享视频]({URL})", URL), (f"这个好好笑！{URL}。", URL),
    ("【标题】 https://b23.tv/AbCdE 复制后打开哔哩哔哩", "https://b23.tv/AbCdE"),
    ("https://youtu.be/abc123", "https://youtu.be/abc123"),
    ("https://www.youtube.com/watch?v=abc123", "https://www.youtube.com/watch?v=abc123"),
    ("https://v.douyin.com/AbCd/", "https://v.douyin.com/AbCd/"),
    ("https://cdn.example.com/cat.mp4?download=1", "https://cdn.example.com/cat.mp4?download=1"),
    (f"{URL} https://b23.tv/second", URL),
    ("https://www.bilibili.com/read/cv123", ""), ("https://www.bilibili.com/", ""),
    ("https://www.youtube.com/@someone", ""), ("https://example.com/article", ""),
    ("https://bilibili.com.evil.example/video/123", ""),
    ("https://www.bilibili.com@evil.example/video/123", ""),
    ("http://127.0.0.1/cat.mp4", ""), ("http://192.168.1.1/cat.mp4", ""),
    ("http://localhost/cat.mp4", ""), ("file:///C:/cat.mp4", ""),
    (f"先别看这个视频，帮我保存链接：{URL}", ""),
    (f"Don't watch this yet: {URL}", ""), ("早上好", ""),
])
def test_video_link_detection(message, expected):
    assert private_video_source(message) == expected


async def test_bare_link_executes_once_and_suppresses_model_retry(setup):
    p, adapter, service = setup
    c = context()
    await ensure_private_video_analysis(p, c)
    await ensure_private_video_analysis(p, c)
    adapter.assert_awaited_once()
    assert adapter.call_args.args[1]["source"] == URL
    assert c.video_preflight_status == "complete"
    assert OVERVIEW in video_preflight_instruction(p, c)
    assert await service._build_bridge_tools(c) == ([], [])


@pytest.mark.parametrize("change", ["group", "group_facing", "history_only", *sorted(SYNTHETIC_SOURCE_KINDS)])
async def test_only_current_incoming_private_message_can_trigger(setup, change):
    p, adapter, _ = setup
    c = context()
    if change in {"group", "group_facing"}:
        setattr(c, "is_group" if change == "group" else "group_facing", True)
    elif change == "history_only":
        c.message = "你好"
        c.prompt_message = "引用历史视频：" + URL
    else:
        c.source_kind = change
    await ensure_private_video_analysis(p, c)
    adapter.assert_not_awaited()


@pytest.mark.parametrize("gate", ["whitelist", "speaker", "initial_permission", "stopped", "old_version", "channel"])
async def test_authorization_and_availability_fail_closed(setup, gate):
    p, adapter, _ = setup
    c = context()
    if gate == "whitelist":
        p._qq_settings["qq_open_plugin_tools"] = {}
    elif gate == "speaker":
        p.permission_mgr.get_permission_level = lambda _: "blacklist"
    elif gate == "initial_permission":
        c.permission_level = "normal"
    elif gate == "channel":
        p.qq_client = None
    else:
        rows = p.plugin_tool_service._candidates_cache[1]
        if gate == "stopped":
            rows[0]["running"] = False
        else:
            rows[0]["entries"] = ["analyze"]
    await ensure_private_video_analysis(p, c)
    adapter.assert_not_awaited()
    assert "没有生成观看结论" in video_preflight_instruction(p, c)


@pytest.mark.parametrize("mode", ["napcat", "open_platform"])
async def test_all_tier_allows_trusted_private_sender(setup, mode):
    p, adapter, _ = setup
    p._qq_settings["qq_open_plugin_tools"] = {"video_reader": "all"}
    p.permission_mgr.get_permission_level = lambda _: "trusted"
    p.qq_client = _plugin(mode=mode).qq_client
    await ensure_private_video_analysis(p, context(permission_level="trusted"))
    adapter.assert_awaited_once()


@pytest.mark.parametrize("cross_context", [False, True])
async def test_zero_tool_model_receives_analysis_before_generation_without_history_pollution(setup, cross_context):
    p, adapter, service = setup
    p._qq_settings["allow_cross_group_context"] = cross_context
    c = context()
    session = NS(_conversation_history=[SystemMessage(content="original")], _instructions="original")
    chunks = []
    async def stream(message):
        adapter.assert_awaited_once()
        assert OVERVIEW in session._instructions
        assert message == URL
        chunks.append("视频里有只猫在玩耍，我只看了抽样画面。")
        session._conversation_history.append(AIMessage(content=chunks[-1]))
    session.stream_text = stream
    result = await service._run_session_generation(context=c, session_key="private:u1",
        user_data={"lock": asyncio.Lock()}, user_session=session, reply_chunks=chunks)
    assert "猫" in result
    assert session._instructions == session._conversation_history[0].content == "original"
    assert c.consent_snapshot[c.video_preflight_token]
    # A following ordinary message must not replay the result or start another download.
    async def next_stream(message):
        assert OVERVIEW not in session._instructions
        chunks.append("早上好")
    session.stream_text = next_stream
    await service._run_session_generation(context=context(message="早上好", prompt_message="早上好"),
        session_key="private:u1", user_data={"lock": asyncio.Lock()}, user_session=session, reply_chunks=chunks)
    adapter.assert_awaited_once()


@pytest.mark.parametrize("stage", ["download", "generation", "delivery"])
async def test_revocation_blocks_analysis_and_generated_delivery(setup, stage):
    p, adapter, service = setup
    c = context()
    def revoke():
        p._qq_settings["qq_open_plugin_tools"] = {}
    if stage == "download":
        async def analyze(*args):
            revoke()
            return OVERVIEW
        adapter.side_effect = analyze
        await ensure_private_video_analysis(p, c)
        assert OVERVIEW not in video_preflight_instruction(p, c)
        assert not c.video_preflight_result
        return
    if stage == "delivery":
        await ensure_private_video_analysis(p, c)
        assert consent_enabled(p, c.video_preflight_token)
        revoke()
        assert not consent_enabled(p, c.video_preflight_token)
        assert OVERVIEW not in video_preflight_instruction(p, c)
        return
    session = NS(_conversation_history=[SystemMessage(content="original")], _instructions="original")
    chunks = []
    async def stream(message):
        assert OVERVIEW in session._instructions
        revoke()
        chunks.append("SECRET")
        session._conversation_history.append(AIMessage(content="SECRET"))
    session.stream_text = stream
    assert await service._run_session_generation(context=c, session_key="private:u1",
        user_data={"lock": asyncio.Lock()}, user_session=session, reply_chunks=chunks) == ""
    assert len(session._conversation_history) == 1


@pytest.mark.parametrize("failure", ["error", "failure", "cancel"])
async def test_failure_is_not_retried_or_claimed_as_success(setup, failure):
    p, adapter, _ = setup
    c = context()
    if failure == "error":
        adapter.side_effect = RuntimeError("secret token")
    elif failure == "cancel":
        adapter.side_effect = asyncio.CancelledError
    else:
        adapter.return_value = video.FAILURE
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await ensure_private_video_analysis(p, c)
    else:
        await ensure_private_video_analysis(p, c)
    await ensure_private_video_analysis(p, c)
    adapter.assert_awaited_once()
    assert c.video_preflight_status == "failed"
    assert "没有生成观看结论" in video_preflight_instruction(p, c)
    assert "secret" not in str(p.logged)


@pytest.mark.parametrize("primary_first", [False, True])
async def test_fallback_uses_same_analysis_without_repeat(setup, monkeypatch, primary_first):
    import plugin.plugins.qq_auto_reply.reply_generation_service as g
    import utils.config_manager as config
    p, adapter, service = setup
    c = context()
    p._should_skip_direct_llm_fallback_for_images = lambda **kw: False
    llm = NS(ainvoke=AsyncMock(return_value=NS(content="视频里有猫")), aclose=AsyncMock())
    monkeypatch.setattr(config, "get_config_manager", lambda: NS(
        get_model_api_config=lambda _: {"model": "test", "base_url": "http://unused.invalid", "api_key": ""}))
    monkeypatch.setattr(g, "create_chat_llm_async", AsyncMock(return_value=llm))
    if primary_first:
        await ensure_private_video_analysis(p, c)
    assert await service.generate_reply_fallback_direct_llm(context=c) == "视频里有猫"
    assert OVERVIEW in llm.ainvoke.call_args.args[0][0]["content"]
    adapter.assert_awaited_once()


async def test_total_analysis_timeout_is_bounded_and_not_retried(setup, monkeypatch):
    p, adapter, _ = setup
    cancelled = asyncio.Event()
    async def slow(*args):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
    adapter.side_effect = slow
    monkeypatch.setattr(video, "VIDEO_TURN_BUDGET_SECONDS", -0.99)
    c = context()
    await ensure_private_video_analysis(p, c)
    assert cancelled.is_set() and c.video_preflight_status == "failed"
    await ensure_private_video_analysis(p, c)
    adapter.assert_awaited_once()


async def test_revocation_while_mounting_tools_skips_model_and_restores_prompt(setup):
    p, adapter, service = setup
    async def arm(**kwargs):
        p._qq_settings["qq_open_plugin_tools"] = {}
        return False, False, False
    service._arm_turn_tools.side_effect = arm
    c = context()
    session = NS(_conversation_history=[SystemMessage(content="original")], _instructions="original",
                 stream_text=AsyncMock())
    assert await service._run_session_generation(context=c, session_key="private:u1",
        user_data={"lock": asyncio.Lock()}, user_session=session, reply_chunks=[]) == ""
    session.stream_text.assert_not_awaited()
    assert session._instructions == "original"
    assert c.video_preflight_result == ""


@pytest.mark.parametrize("revoke", ["whitelist", "sender", "channel"])
async def test_direct_and_buffered_delivery_recheck_video_consent(setup, revoke):
    from plugin.plugins.qq_auto_reply.reply_buffer_service import QQReplyBufferService
    from plugin.plugins.qq_auto_reply.reply_pipeline import QQReplyPipelineRunner
    p, _, service = setup
    c = context()
    await ensure_private_video_analysis(p, c)
    service._store_consent_snapshot(c, service._consent_dependency_snapshot(c))
    runner = QQReplyPipelineRunner(p)
    buffer = object.__new__(QQReplyBufferService)
    buffer.plugin = p
    assert not runner._consent_revoked_before_send(c)
    assert not buffer._consent_revoked_since(c)
    if revoke == "whitelist":
        p._qq_settings["qq_open_plugin_tools"] = {}
    elif revoke == "sender":
        p.permission_mgr.get_permission_level = lambda _: "blacklist"
    else:
        p.qq_client = None
    assert runner._consent_revoked_before_send(c)
    assert buffer._consent_revoked_since(c)
