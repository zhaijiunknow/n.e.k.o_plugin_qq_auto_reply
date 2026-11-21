"""Explicit deployment backend selection; OneBot wire mode stays independent."""
from enum import StrEnum


class OneBotBackend(StrEnum):
    SNOWLUMA = "snowluma"


def selected_backend(settings) -> OneBotBackend:
    # Legacy backend names are read-compatible only; saving NapCat is rejected.
    value = (settings or {}).get("onebot_backend")
    return OneBotBackend("snowluma" if value in (None, "", "napcat") else value)


class OneBotBackendService:
    """Route the historical NapCat service API to the selected implementation."""

    def __init__(self, *, get_settings, **kwargs):
        from .snowluma_service import QQSnowLumaService

        self.get_settings = get_settings
        self.services = {
            OneBotBackend.SNOWLUMA: QQSnowLumaService(get_settings=get_settings, **kwargs),
        }

    def __getattr__(self, name):
        return getattr(self.services[selected_backend(self.get_settings())], name)

    async def stop_managed_napcat(self):
        # Also clean up a previously selected backend on shutdown.
        for service in self.services.values():
            await service.stop_managed_napcat()
