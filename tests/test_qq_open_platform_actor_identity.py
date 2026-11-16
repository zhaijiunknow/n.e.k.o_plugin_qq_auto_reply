# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""R11 resolved: where the open platform's speaker id actually comes from.

The payloads below are transcribed from Tencent's own published material and
are the whole reason this file exists -- the bug being pinned here was a
connector reading ``author.id``, a key that does not exist on either of the
two events it handles, which made every speaker collapse into the empty
string in silence.

Sources, both first-party and mutually corroborating:

- ``tencent-connect/bot-docs``,
  ``develop/api-v2/server-inter/message/send-receive/event.md`` -- the field
  tables and the sample JSON for both events;
- ``tencent-connect/botpy``, ``botpy/message.py`` -- ``C2CMessage._User``
  reads only ``user_openid``, ``GroupMessage._User`` only ``member_openid``;
  only the guild-side ``Message._User`` has an ``id``.

See ``docs/design/speaker-trust-entity-semantics.md`` section 2.15.4.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from plugin.plugins.qq_auto_reply.connector_seam import qq_open_plat as _qq_plat
from plugin.plugins.qq_auto_reply.dashboard_service import QQDashboardService
from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService

_C2C_ACTOR_ID_KEYS = _qq_plat._C2C_ACTOR_ID_KEYS
_GROUP_ACTOR_ID_KEYS = _qq_plat._GROUP_ACTOR_ID_KEYS
QQOpenPlatformConnection = _qq_plat.QQOpenPlatformConnection
pick_actor_id = _qq_plat.pick_actor_id

# The vendor's own sample payloads, field for field.
OFFICIAL_C2C_EVENT = {
    "author": {"user_openid": "E4F4AEA33253A2797FB897C50B81D7ED"},
    "content": "123",
    "id": "ROBOT1.0_.b6nx.CVryAO0nR58RXuU6SC.m92gc19j02qKqdm8ek!",
    "timestamp": "2023-11-06T13:37:18+08:00",
}
OFFICIAL_GROUP_EVENT = {
    "author": {"member_openid": "E4F4AEA33253A2797FB897C50B81D7ED"},
    "content": " 123",
    "group_openid": "C9F778FE6ADF9D1D1DBE395BF744A33A",
    "id": "ROBOT1.0_eBIyWnxpmSu6uLQ7u7fU0eGloKGYg4eEa737vRyKnMCgyZjKi7JLYkQ9B0",
    "timestamp": "2023-11-06T13:37:18+08:00",
}


def _connection():
    conn = QQOpenPlatformConnection.__new__(QQOpenPlatformConnection)
    conn._self_id = ""
    return conn


# ==========================================================================
# A. The extractor, against the vendor's own payloads
# ==========================================================================


def test_official_c2c_payload_yields_the_user_openid():
    message = _connection()._convert_event(
        "C2C_MESSAGE_CREATE", OFFICIAL_C2C_EVENT,
    )

    assert message["user_id"] == "E4F4AEA33253A2797FB897C50B81D7ED"
    assert message["message_type"] == "private"


def test_official_group_payload_yields_the_member_openid_and_group_openid():
    message = _connection()._convert_event(
        "GROUP_AT_MESSAGE_CREATE", OFFICIAL_GROUP_EVENT,
    )

    assert message["user_id"] == "E4F4AEA33253A2797FB897C50B81D7ED"
    assert message["group_id"] == "C9F778FE6ADF9D1D1DBE395BF744A33A"


@pytest.mark.parametrize("event_type,payload", [
    ("C2C_MESSAGE_CREATE", OFFICIAL_C2C_EVENT),
    ("GROUP_AT_MESSAGE_CREATE", OFFICIAL_GROUP_EVENT),
])
def test_no_official_payload_leaves_the_speaker_id_empty(event_type, payload):
    """The regression this whole PR exists for.

    An empty speaker id does not raise anywhere: permissions resolve it to
    ``none``, memory writes it into a subject id, and the sender POSTs to
    ``/v2/users//messages``. Every one of those fails quietly, which is why
    this assertion is worth making separately from the two above.
    """
    message = _connection()._convert_event(event_type, payload)

    assert message["user_id"] != ""


def test_the_two_paths_do_not_read_each_other_s_key():
    """A group event carrying a ``user_openid`` must not be read as one.

    They are different scopes for the same human. Crossing them would merge
    two identities that the platform deliberately keeps apart -- exactly the
    automatic identity merge the design forbids, done by accident.
    """
    group = _connection()._convert_event("GROUP_AT_MESSAGE_CREATE", {
        "author": {"member_openid": "MEMBER_X", "user_openid": "USER_GLOBAL"},
        "group_openid": "GROUP_X",
    })
    c2c = _connection()._convert_event("C2C_MESSAGE_CREATE", {
        "author": {"member_openid": "MEMBER_X", "user_openid": "USER_GLOBAL"},
    })

    assert group["user_id"] == "MEMBER_X"
    assert c2c["user_id"] == "USER_GLOBAL"


def test_id_is_only_a_fallback_never_a_preference():
    """If the protocol ever adds ``id`` back, the documented key still wins."""
    group = _connection()._convert_event("GROUP_AT_MESSAGE_CREATE", {
        "author": {"id": "LEGACY_ID", "member_openid": "MEMBER_X"},
        "group_openid": "GROUP_X",
    })
    c2c = _connection()._convert_event("C2C_MESSAGE_CREATE", {
        "author": {"id": "LEGACY_ID", "user_openid": "USER_1"},
    })

    assert group["user_id"] == "MEMBER_X"
    assert c2c["user_id"] == "USER_1"


def test_id_is_used_when_the_documented_key_is_absent():
    group = _connection()._convert_event("GROUP_AT_MESSAGE_CREATE", {
        "author": {"id": "ONLY_ID"}, "group_openid": "GROUP_X",
    })

    assert group["user_id"] == "ONLY_ID"


@pytest.mark.parametrize("author", [
    None, {}, "not-a-dict", 42, {"member_openid": ""}, {"member_openid": "   "},
])
def test_missing_or_blank_author_degrades_to_empty_without_raising(author):
    assert pick_actor_id(author, _GROUP_ACTOR_ID_KEYS) == ""
    assert pick_actor_id(author, _C2C_ACTOR_ID_KEYS) == ""


def test_key_order_pins_the_documented_key_first():
    # Reordering these tuples silently reintroduces the bug, and every test
    # above would still pass on payloads that carry only one of the keys.
    assert _GROUP_ACTOR_ID_KEYS[0] == "member_openid"
    assert _C2C_ACTOR_ID_KEYS[0] == "user_openid"
    assert "user_openid" not in _GROUP_ACTOR_ID_KEYS
    assert "member_openid" not in _C2C_ACTOR_ID_KEYS


# ==========================================================================
# B. The protocol table that gets declared to the trust pool
# ==========================================================================


def test_open_platform_is_declared_per_conversation_on_the_actor_axis():
    channel, actor_scope, conversation_scope = (
        QQSettingsService.IDENTITY_SCOPE_BY_MODE["open_platform"]
    )

    assert channel == "open"
    # Tencent's "unique identity" page: the same person's member_openid
    # differs per group for one and the same bot.
    assert actor_scope == "per_conversation"
    # group_openid is one-per-group, not one-per-group-per-person -- the
    # asymmetry that makes the conversation side rescuable.
    assert conversation_scope == "global"


def test_napcat_stays_global_on_both_axes():
    channel, actor_scope, conversation_scope = (
        QQSettingsService.IDENTITY_SCOPE_BY_MODE["napcat"]
    )

    assert (channel, actor_scope, conversation_scope) == (
        "onebot", "global", "global",
    )


def test_every_declared_mode_names_its_protocol_as_the_asserter():
    """``asserted_by`` must say which protocol, not "code".

    The whole value of this container is that a reader can tell a transcribed
    vendor contract apart from something the process inferred from traffic.
    """
    for mode in QQSettingsService.IDENTITY_SCOPE_BY_MODE:
        asserter = QQSettingsService.IDENTITY_SCOPE_ASSERTED_BY[mode]
        assert asserter.startswith("protocol:")


# ==========================================================================
# C. Protocol scope remains independent of account links.


def _dashboard(*, roster, profiles, mode="open_platform"):
    service = QQDashboardService.__new__(QQDashboardService)
    service.plugin = SimpleNamespace(settings_service=QQSettingsService,
                                     _qq_settings={"qq_connection_mode": mode})
    return service


async def test_a_stopped_client_does_not_count_as_the_running_transport():
    """``stop_runtime`` disconnects but leaves the object installed.

    ``CHANNEL`` is a class attribute, so judging by the object alone keeps a
    stopped channel looking live -- and the claim UI then stays shown (or
    stays hidden) against the saved configuration the docstring promises.
    """
    service = _dashboard(roster=[], profiles={}, mode="napcat")
    service.plugin.qq_client = SimpleNamespace(CHANNEL="open")
    service.plugin._running = False

    assert service._identity_scope_payload()["actor_scope"] == "global"

    service.plugin._running = True
    assert service._identity_scope_payload()["actor_scope"] == "per_conversation"


async def test_a_failing_scope_declaration_never_fails_an_established_start():
    """It is one registration with its own backoff -- not a startup dependency.

    Letting it raise inside ``start_auto_reply`` sends an ALREADY CONNECTED
    start into the except branch: the plugin reports a startup failure while
    the socket is up, and the housekeeping task never gets created.
    """
    from plugin.plugins.qq_auto_reply.runtime_ops_service import (
        QQRuntimeOpsService,
    )

    settings = {"qq_connection_mode": "open_platform"}

    async def _connect_then_someone_saves_another_mode():
        # The window codex pointed at: ``connect()`` is an await, and another
        # dashboard session can save a different mode across it. The channel
        # that actually connected is the open platform one; re-reading the
        # settings afterwards would register napcat's semantics for it.
        settings["qq_connection_mode"] = "napcat"

    plugin = SimpleNamespace(
        _session_housekeeping_task=None,
        _session_housekeeping_loop=AsyncMock(),
        _running=False,
        _message_task=None,
        _process_messages=AsyncMock(),
        _qq_settings=settings,
        _ensure_qq_client_initialized=lambda: None,
        qq_client=SimpleNamespace(
            needs_attention=False,
            connect=AsyncMock(side_effect=_connect_then_someone_saves_another_mode),
            onebot_url="",
        ),
        attention_service=None,
        attention_gate_service=None,
        napcat_service=SimpleNamespace(get_startup_error=lambda: "", clear_startup_error=lambda: None, set_startup_error=lambda *a, **k: None),
        _emit_log=lambda *a, **k: None,
        logger=MagicMock(),
        i18n=SimpleNamespace(t=lambda key, default="", **kw: default),
        _startup_error=None,
        settings_service=SimpleNamespace(
            ensure_identity_scope_declared=MagicMock(
                side_effect=RuntimeError("memory_server unreachable"),
            ),
        ),
    )

    result = await QQRuntimeOpsService(plugin).start_auto_reply()

    assert plugin._running is True
    assert plugin._startup_error is None
    assert result.value["status"] == "started"
    declared = plugin.settings_service.ensure_identity_scope_declared
    # The mode is pinned at the moment the connection was built, not re-read.
    assert declared.call_args.args == ("open_platform",)
    if plugin._message_task:
        plugin._message_task.cancel()


async def test_the_scope_follows_the_running_transport_not_the_saved_config():
    """Saving a mode does not switch the live client; the save says so itself.

    Describing the new mode during that gap hides the claim UI while open
    platform messages are still arriving.
    """
    service = _dashboard(roster=[], profiles={}, mode="napcat")
    service.plugin.qq_client = SimpleNamespace(CHANNEL="open")
    service.plugin._running = True

    assert service._identity_scope_payload()["actor_scope"] == "per_conversation"


async def test_an_unrecognised_live_channel_says_unknown_rather_than_the_config():
    """A transport outside the table is not described by the saved mode.

    Falling back to the configuration there would show a scope that belongs to
    some other channel, which is the opposite of what "follow the running
    transport" promises. Unreachable today (both channels are in the table);
    it is the third one that would silently get a wrong answer.
    """
    service = _dashboard(roster=[], profiles={}, mode="open_platform")
    service.plugin.qq_client = SimpleNamespace(CHANNEL="some_new_transport")
    service.plugin._running = True

    scope = service._identity_scope_payload()

    assert scope["actor_scope"] == "unknown"
    assert scope["conversation_scope"] == "unknown"


async def test_the_scope_falls_back_to_config_when_nothing_is_connected():
    service = _dashboard(roster=[], profiles={}, mode="open_platform")
    service.plugin.qq_client = None

    assert service._identity_scope_payload()["actor_scope"] == "per_conversation"


def test_the_dashboard_reports_the_degradation_only_on_the_open_platform():
    open_scope = _dashboard(
        roster=[], profiles={}, mode="open_platform",
    )._identity_scope_payload()
    napcat_scope = _dashboard(
        roster=[], profiles={}, mode="napcat",
    )._identity_scope_payload()

    assert open_scope["actor_scope"] == "per_conversation"
    assert napcat_scope["actor_scope"] == "global"


def test_an_unknown_connection_mode_says_unknown_rather_than_guessing():
    scope = _dashboard(
        roster=[], profiles={}, mode="something_new",
    )._identity_scope_payload()

    assert scope["actor_scope"] == "unknown"
    assert scope["conversation_scope"] == "unknown"
