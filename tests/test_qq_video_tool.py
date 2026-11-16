import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from plugin.plugins.qq_auto_reply import video_tool_service as video
from test_qq_plugin_tool_bridge import (
    _candidate,
    _context,
    _generation_service,
    _handler,
    _plugin,
    _plugins_payload,
    _refresh,
    _Session,
)


def envelope(count=2):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(buf, format="JPEG")
    return {
        "output": {"status": "images_ready", "instruction": "0-5秒；S1=0秒，S2=5秒；无音频。"},
        "images": [{"mime": "image/jpeg", "data_b64": base64.b64encode(buf.getvalue()).decode()} for _ in range(count)],
    }


def prepared_plugin():
    plugin = _plugin(tiers={"video_reader": "admin"})
    plugin.plugins = SimpleNamespace(call_entry=AsyncMock(return_value=SimpleNamespace(value=envelope(), is_err=lambda: False)))
    return plugin


def call(entry="prepare_for_qq"):
    return SimpleNamespace(call_id="c1", name="plugin_video_reader", arguments={"entry_id": entry, "params": {"source": "https://example.com/v.mp4"}})


def mounted():
    return [({"plugin_id": "video_reader", "entries": ["analyze", "watch_video", "prepare_for_qq"], "running": True}, "admin")]


def test_only_safe_entry_is_mounted_and_old_video_version_is_not():
    service = _plugin(tiers={"video_reader": "admin"}).plugin_tool_service
    selected = service.select_mounted([mounted()[0][0]], allowed_tiers={"admin"})
    assert selected[0][0]["entries"] == ["prepare_for_qq"]
    assert mounted()[0][0]["entries"] == ["analyze", "watch_video", "prepare_for_qq"]
    assert not service.select_mounted([mounted()[0][0]], allowed_tiers={"all"})
    old = {**mounted()[0][0], "entries": ["analyze"]}
    assert not service.select_mounted([old], allowed_tiers={"admin"})
    definition = service.build_tool_definition(mounted()[0][0], "admin")
    assert definition.parameters["properties"]["entry_id"]["enum"] == ["prepare_for_qq"]
    assert "source" in definition.description


# ── 看视频插件 0.3.1 起只剩 analyze 一个入口（带 return_images=true 才不往桌面推）──────
#
# 真机（2026-11-10 起）：装了新版看视频插件之后，QQ 这边只认 prepare_for_qq，于是每一轮都
# 把它归进「本次未挂载」，管理员私聊里再也触发不了看视频。两代入口都要认。

def single_entry_row():
    """新版看视频插件在候选表里的样子：只有 analyze，且它的入参声明了 return_images。"""
    return {"plugin_id": "video_reader", "entries": ["analyze"], "return_image_entries": ["analyze"], "running": True}


def test_the_single_entry_video_plugin_is_mounted_through_analyze():
    service = _plugin(tiers={"video_reader": "admin"}).plugin_tool_service
    selected = service.select_mounted([single_entry_row()], allowed_tiers={"admin"})
    assert [row["entries"] for row, _tier in selected] == [["analyze"]]
    definition = service.build_tool_definition(selected[0][0], "admin")
    assert definition.parameters["properties"]["entry_id"]["enum"] == ["analyze"]
    assert "仅用 analyze" in definition.description and "prepare_for_qq" not in definition.description
    # 两代入口同时在（不该发生，但别挑到会往桌面推的那个）：专用入口优先。
    both = {**single_entry_row(), "entries": ["analyze", "prepare_for_qq"]}
    assert service.qq_video_entry(both) == "prepare_for_qq"
    # 更早的版本也叫 analyze，但没有 return_images：那个只会往桌面推，不能挂。
    assert service.qq_video_entry({"plugin_id": "video_reader", "entries": ["analyze"]}) == ""


def test_the_candidate_table_records_which_entries_can_return_images():
    schema = {"type": "object", "properties": {"source": {"type": "string"}, "return_images": {"type": "boolean"}}}
    row = _candidate("video_reader", ("analyze",))
    row["entries"][0]["input_schema"] = schema
    plugin = _plugin(tiers={"video_reader": "admin"}, plugins_result=_plugins_payload(row))
    candidates = asyncio.run(_refresh(plugin.plugin_tool_service))
    video_row = next(item for item in candidates if item["plugin_id"] == "video_reader")
    assert video_row["return_image_entries"] == ["analyze"]
    assert plugin.plugin_tool_service.select_mounted(candidates, allowed_tiers={"admin"})[0][0]["entries"] == ["analyze"]
    # 入参里的 return_images 不是布尔型：不认（别把别的同名参数当成那个开关）。
    schema["properties"]["return_images"] = {"type": "string"}
    assert plugin.plugin_tool_service._return_image_entries(row) == []


@pytest.mark.asyncio
async def test_analyze_is_always_called_with_return_images_forced_on(monkeypatch):
    """return_images 由适配器写死为 True，模型传什么都不采信 —— 少了它这个入口会往桌面推。"""
    plugin = prepared_plugin()
    monkeypatch.setattr(video, "_describe_video", AsyncMock(return_value="概况"))
    params = {"source": "url", "return_images": False, "_ctx": {"lanlan_name": "desktop"}}
    result = await video.analyze_qq_video(plugin, params, lambda: True, entry="analyze")
    assert plugin.plugins.call_entry.call_args.args == ("video_reader:analyze", {"source": "url", "return_images": True})
    assert "概况" in result
    # 不认识的入口名：不调用、按失败处理。
    plugin.plugins.call_entry.reset_mock()
    assert await video.analyze_qq_video(plugin, {"source": "url"}, lambda: True, entry="watch_video") == video.FAILURE
    plugin.plugins.call_entry.assert_not_called()


@pytest.mark.asyncio
async def test_the_handler_routes_the_single_entry_through_the_video_adapter(monkeypatch):
    plugin = _plugin(tiers={"video_reader": "admin"})
    adapter = AsyncMock(return_value="完整概况")
    monkeypatch.setattr(video, "analyze_qq_video", adapter)
    handler = _handler(plugin, [(single_entry_row(), "admin")])
    assert "不在可用列表" in (await handler(call("prepare_for_qq"))).output
    assert (await handler(call("analyze"))).output == "完整概况"
    assert adapter.call_args.kwargs == {"entry": "analyze"}
    assert plugin.calls == [], "看视频不能走通用的 call_entry 那条路（那条不强制 return_images）"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["open_platform", "napcat"])
async def test_only_one_video_call_per_turn_and_never_desktop_entry(monkeypatch, mode):
    plugin = _plugin(tiers={"video_reader": "admin"}, mode=mode)
    adapter = AsyncMock(return_value="完整概况")
    monkeypatch.setattr(video, "analyze_qq_video", adapter)
    handler = _handler(plugin, mounted())
    for entry in ("analyze", "watch_video"):
        assert "不在可用列表" in (await handler(call(entry))).output
    first, second = await asyncio.gather(handler(call()), handler(call()))
    assert first.output == "完整概况"
    assert "本轮已尝试" in second.output
    adapter.assert_awaited_once()
    assert plugin.calls == []


@pytest.mark.asyncio
async def test_real_images_to_text_only_and_extra_params_removed(monkeypatch):
    plugin = prepared_plugin()
    describe = AsyncMock(return_value="5秒内出现红色画面；没有分析声音。")
    monkeypatch.setattr(video, "_describe_video", describe)
    result = await video.analyze_qq_video(plugin, {"source": "url", "_ctx": {"lanlan_name": "desktop"}}, lambda: True)
    args = plugin.plugins.call_entry.call_args
    assert args.args == ("video_reader:prepare_for_qq", {"source": "url"})
    assert args.kwargs["timeout"] == 300
    content = describe.call_args.args[0]
    assert len(content) == 3
    assert all(row["image_url"]["url"].startswith("data:image/jpeg;base64,") for row in content[1:])
    assert "5秒" in result and "base64" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "download", "vision"])
async def test_revocation_discards_results(monkeypatch, stage):
    plugin = prepared_plugin()
    permitted = stage != "before"
    async def download(*args, **kwargs):
        nonlocal permitted
        if stage == "download":
            permitted = False
        return envelope()
    async def describe(*args):
        nonlocal permitted
        permitted = False
        return "secret result"
    plugin.plugins.call_entry.side_effect = download
    vision = AsyncMock(side_effect=describe)
    monkeypatch.setattr(video, "_describe_video", vision)
    result = await video.analyze_qq_video(plugin, {}, lambda: permitted)
    assert result == video.REVOKED and "secret" not in result
    if stage != "vision":
        vision.assert_not_called()
    if stage == "before":
        plugin.plugins.call_entry.assert_not_called()


@pytest.mark.parametrize("broken", ["empty", "count", "base64", "mime", "size", "pixels", "status", "instruction", "corrupt"])
def test_invalid_envelope_is_rejected_completely(broken):
    data = envelope()
    if broken == "empty":
        data["images"] = []
    elif broken == "count":
        data["images"] *= 2
    elif broken == "base64":
        data["images"][1]["data_b64"] = "!!!"
    elif broken == "mime":
        data["images"][1]["mime"] = "image/png"
    elif broken == "size":
        data["images"][1]["data_b64"] = "A" * 500000
    elif broken == "pixels":
        buf = io.BytesIO()
        Image.new("RGB", (2049, 1)).save(buf, format="JPEG")
        data["images"][1]["data_b64"] = base64.b64encode(buf.getvalue()).decode()
    elif broken == "status":
        data["output"]["status"] = "error"
    elif broken == "instruction":
        data["output"]["instruction"] = ""
    elif broken == "corrupt":
        data["images"][1]["data_b64"] = base64.b64encode(b"not an image").decode()
    with pytest.raises((ValueError, OSError)):
        video._validated_content(data)


@pytest.mark.asyncio
async def test_failure_sanitized_and_cancellation_propagates(monkeypatch):
    plugin = prepared_plugin()
    describe = AsyncMock(side_effect=RuntimeError("sk-private-credential data:base64"))
    monkeypatch.setattr(video, "_describe_video", describe)
    assert await video.analyze_qq_video(plugin, {}, lambda: True) == video.FAILURE
    assert "private" not in str(plugin.logged)
    describe.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await video.analyze_qq_video(plugin, {}, lambda: True)


@pytest.mark.asyncio
async def test_vision_timeout_does_not_retry(monkeypatch):
    plugin = prepared_plugin()
    async def slow(*args):
        await asyncio.sleep(2)
    describe = AsyncMock(side_effect=slow)
    monkeypatch.setattr(video, "_describe_video", describe)
    monkeypatch.setattr(video, "VIDEO_VISION_TIMEOUT_SECONDS", 0.05)
    assert await video.analyze_qq_video(plugin, {}, lambda: True) == video.FAILURE
    describe.assert_awaited_once()
    plugin.plugins.call_entry.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_host_vision_config_is_isolated_and_client_closed(monkeypatch, cancel):
    import utils.config_manager as cm
    import utils.llm_client as lc
    config = SimpleNamespace(aget_model_api_config=AsyncMock(return_value={"model": "free-vision", "api_key": "secret", "base_url": "host", "provider_type": "openai"}))
    llm = SimpleNamespace()  # async magic methods belong on the type
    from unittest.mock import MagicMock
    llm = MagicMock()
    llm.__aenter__ = AsyncMock(return_value=llm)
    llm.__aexit__ = AsyncMock(return_value=False)
    llm.ainvoke = AsyncMock(return_value=SimpleNamespace(content="overview"))
    if cancel:
        llm.ainvoke.side_effect = asyncio.CancelledError
    create = AsyncMock(return_value=llm)
    monkeypatch.setattr(cm, "get_config_manager", Mock(return_value=config))
    monkeypatch.setattr(lc, "create_chat_llm_async", create)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await video._describe_video(video._validated_content(envelope()), lambda: True)
    else:
        assert await video._describe_video(video._validated_content(envelope()), lambda: True) == "overview"
    config.aget_model_api_config.assert_awaited_once_with("vision")
    assert create.call_args.kwargs["model"] == "free-vision"
    assert create.call_args.kwargs["max_retries"] == 0
    llm.__aexit__.assert_awaited_once()


def test_generation_timeout_budget_only_when_video_mounted_and_resets():
    plugin = _plugin(tiers={"video_reader": "admin"}, plugins_result=_plugins_payload(_candidate("video_reader", ("prepare_for_qq",))))
    asyncio.run(_refresh(plugin.plugin_tool_service))
    service = _generation_service(plugin)
    session = _Session()
    asyncio.run(service._arm_turn_tools(context=_context(), user_session=session, consent_before={}))
    assert session._qq_video_tool_budget == 365
    plugin._qq_settings["qq_open_plugin_tools"] = {}
    asyncio.run(service._arm_turn_tools(context=_context(), user_session=session, consent_before={}))
    assert session._qq_video_tool_budget == 0
