"""SnowLuma deployment dispatch and shared progress/status reporting."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

STEP_LABELS = {
    "locate": "定位 SnowLuma", "node": "检查运行环境", "fetch": "下载并解包 SnowLuma",
    "config": "写入 OneBot 配置", "start": "启动 SnowLuma", "qrcode": "等待 QQ 登录",
}


class QQDeployService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        #: (step, phase) → 上次已上报的百分比，用于节流
        self._progress_last: dict[tuple[str, str], int] = {}

    # ── 内部 ────────────────────────────────────────────────

    def _settings(self) -> dict:
        s = getattr(self.plugin, "_qq_settings", None)
        return s if isinstance(s, dict) else {}

    def _emit(self, emit: Callable[[dict], None] | None, step: str,
              message: str, **extra: Any) -> None:
        payload = {"step": step, "label": STEP_LABELS.get(step, step),
                   "message": message, **extra}
        if emit is not None:
            try:
                emit(payload)
            except Exception:
                pass
        self.plugin._emit_log("INFO", f"[Deploy] {message}")
        # 同时落盘：面板那份是**内存**缓冲（500 行、重启即失），而用户来报"卡在下载"
        # 时我们手上只有文件日志 —— 真机上因此只剩一句 `一键部署失败: …`，看不出
        # 停在哪一步、试过几个源、续传到多少字节。
        self._log_to_file(f"[Deploy] {message}")

    def _log_to_file(self, line: str) -> None:
        """把部署明细也写进文件日志（尽力而为：日志失败不该掀翻部署）。"""
        logger = getattr(self.plugin, "logger", None)
        if logger is None:
            return
        try:
            logger.info(line)
        except Exception:
            pass

    def _progress(self, emit, step: str, done: int, total: int, *,
                  phase: str = "download") -> None:
        """上报一次进度。

        ``phase="download"`` 按 MB 显示，``"extract"`` 按条目数 —— 一次解包 690 个文件，
        套用 MB 格式会显示成 "690.0 / 690.0 MB"，纯属误导。

        按百分比节流：一次下载会产生几百个 chunk 回调，全推给 SSE 会把界面日志刷爆，
        也会把真正有用的行挤没。
        """
        pct = int(done * 100 / total) if total else 0
        if phase == "extract":
            text = f"{done}/{total} 个文件"
        elif total:
            text = f"{done / 1048576:.1f} / {total / 1048576:.1f} MB"
        else:
            text = f"已下载 {done / 1048576:.1f} MB"

        key = (step, phase)
        if self._progress_last.get(key) == pct:
            return
        self._progress_last[key] = pct
        self._emit(emit, step, text, progress=pct,
                   downloaded_bytes=done, total_bytes=total, phase=phase)

    async def _record_deployment(self, backend: str, directory: Path | None = None) -> None:
        """Remember completion per backend/path, independently of QQ login state."""
        def update(settings):
            records = dict(settings.get("backend_deployments") or {})
            # An empty entry suppresses legacy running-process detection after a failed attempt.
            records[backend] = str(directory.resolve()) if directory is not None else ""
            settings["backend_deployments"] = records
            return True

        try:
            if await self.plugin.settings_service.mutate_business_config(update) is False:
                raise RuntimeError("部署状态保存失败，请检查存档目录权限后重试")
        except Exception:
            # Do not publish a successful completion if the final write failed.
            records = dict(self._settings().get("backend_deployments") or {})
            records[backend] = ""
            self._settings()["backend_deployments"] = records
            raise

    # ── 主流程 ──────────────────────────────────────────────

    async def deploy(self, *, uin: str = "", force: bool = False,
                     auto_start: bool = True,
                     emit: Callable[[dict], None] | None = None) -> dict:
        """跑完整条部署。返回结果字典；失败抛 ``RuntimeError``。"""
        if self._settings().get("qq_connection_mode") == "open_platform":
            raise RuntimeError("开放平台无需部署 OneBot 后端，请先选择 OneBot")
        from .snowluma_deploy import QQSnowLumaDeployService
        return await QQSnowLumaDeployService(self.plugin).deploy(uin=uin, force=force, auto_start=auto_start, emit=emit)

    async def _online_login_uin(self) -> str:
        """仅实时连接可确认在线；历史配置文件不作为页面登录状态。"""
        runtime = getattr(self.plugin, "runtime_service", None)
        if runtime is not None:
            try:
                payload = await runtime.fetch_login_status_payload()
            except Exception:
                return ""
            if isinstance(payload, dict) and payload.get("status") == "online":
                return str(payload.get("self_id") or "").strip()
        return ""

    async def poll_login(self, *, auto_start: bool = True,
                         emit: Callable[[dict], None] | None = None) -> dict:
        from .snowluma_deploy import QQSnowLumaDeployService
        return await QQSnowLumaDeployService(self.plugin).poll_login(auto_start=auto_start, emit=emit)

    # ── 扫码登录后的补写 ────────────────────────────────────

    async def apply_onebot_config(self, *, uin: str = "", restart: bool = False,
                                  emit: Callable[[dict], None] | None = None) -> dict:
        if self._settings().get("qq_connection_mode") == "open_platform":
            raise RuntimeError("开放平台无需写入 OneBot 配置")
        from .snowluma_deploy import QQSnowLumaDeployService
        return await QQSnowLumaDeployService(self.plugin).apply_onebot_config(uin=uin, restart=restart, emit=emit)
