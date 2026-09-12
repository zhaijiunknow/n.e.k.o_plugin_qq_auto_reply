"""一键部署编排：定位 → 获取 → 写 OneBot 配置 → 启动 → 出二维码。

每一步都通过 ``emit`` 回调上报（插件把它转成 SSE 推给界面）。下载实测可能几十秒到
几分钟（镜像速度不稳），没有进度反馈用户会以为卡死。

止于"出二维码" —— 扫码登录必须人工，绕不过去。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from . import napcat_install, napcat_onebot_config, napcat_platform
from .napcat_service import bundled_napcat_dir


#: 步骤名 → 界面展示用的标签
STEP_LABELS: dict[str, str] = {
    "locate": "定位 NapCat",
    "fetch": "获取 NapCat",
    "config": "写入 OneBot 配置",
    "start": "启动 NapCat",
    "qrcode": "等待登录二维码",
}

#: 启动后等二维码出现的上限。NapCat 首次启动要先拉起 QQ，比后续启动慢。
QRCODE_WAIT_SECONDS = 60.0


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

    async def _ensure_token(self, emit=None) -> str:
        """复用已有 token；为空则生成一个并**同时**持久化。

        两边 token 不一致是"连不上但毫无提示"的经典原因 —— 生成后立刻写回设置，
        NapCat 侧由 ``napcat_onebot_config`` 写同一份。
        """
        settings = self._settings()
        token = str(settings.get("token") or "").strip()
        if token:
            return token
        token = secrets.token_urlsafe(12)
        settings["token"] = token
        try:
            await self.plugin.settings_service.persist_business_config()
            self._emit(emit, "config", "已生成反向 WS Token 并保存")
        except Exception as e:
            self._emit(emit, "config", f"Token 已生成，但保存失败: {e}")
        return token

    # ── 主流程 ──────────────────────────────────────────────

    async def deploy(self, *, uin: str = "", force: bool = False,
                     emit: Callable[[dict], None] | None = None) -> dict:
        """跑完整条部署。返回结果字典；失败抛 ``RuntimeError``。"""
        svc = self.plugin.napcat_service
        steps: list[dict] = []

        def step(key: str, msg: str, **ex: Any) -> None:
            steps.append({"step": key, "message": msg, **ex})
            self._emit(emit, key, msg, **ex)

        # ① 定位 -------------------------------------------------
        configured = svc.get_configured_napcat_path()
        napcat_dir = svc.get_napcat_directory()
        launcher = napcat_platform.find_launcher(napcat_dir) if napcat_dir else None

        if configured and launcher is None:
            # 用户显式指了一个目录却没有启动器 —— 不要"帮"他装到别处，
            # 那会让设置与实际位置长期不一致，下次更难看懂。
            raise RuntimeError(
                f"napcat_directory 指向的目录里没有 NapCat 启动器: {configured}"
            )
        if launcher is not None and not force:
            step("locate", f"已有可用 NapCat: {napcat_dir}")
        else:
            step("locate", "未发现可用 NapCat，准备下载")
            napcat_dir = await self._fetch(step, emit)

        # ③ 写配置（在启动之前 —— NapCat 启动时读配置）--------------
        mode = str(self._settings().get("qq_connection_mode") or "napcat")
        uin = str(uin or "").strip()
        if uin:
            await self._write_config(napcat_dir, uin, mode, step, emit)
        else:
            step("config", "未填机器人 QQ 号：扫码登录后请点「补写 OneBot 配置」",
                 needs_uin=True)

        # ④ 启动 -------------------------------------------------
        await svc.ensure_napcat_started()
        err = svc.get_startup_error()
        if err:
            raise RuntimeError(err)
        step("start", "NapCat 已启动")

        # ⑤ 二维码 -----------------------------------------------
        ready = await self._wait_qrcode(svc)
        step("qrcode", "二维码已就绪，请扫码登录" if ready
             else "暂未出现二维码，NapCat 可能仍在启动（可点「刷新二维码」重试）",
             ready=ready)

        return {
            "ok": True,
            "napcat_directory": str(napcat_dir),
            "uin": uin,
            "qrcode_ready": ready,
            "needs_uin": not uin,
            "steps": steps,
        }

    async def _fetch(self, step, emit) -> Path:
        """下载并解包到插件自带位置。"""
        ok, ver = napcat_platform.node_available()
        if not ok:
            raise RuntimeError(
                f"缺少 Node.js（{ver}）。NapCat 的发行包是 Node 程序，需要系统已安装 Node 18+；"
                "插件不会代为安装系统级软件包。"
            )
        self._emit(emit, "fetch", f"Node 就绪: {ver}")

        target = bundled_napcat_dir()
        asset = napcat_install.asset_name()
        downloads = self.plugin.data_path("downloads")

        step("fetch", f"下载 {asset}（{napcat_install.NAPCAT_VERSION}）…")
        zip_path = await napcat_install.download_asset(
            asset, downloads,
            progress=lambda d, t: self._progress(emit, "fetch", d, t),
        )
        step("fetch", f"下载完成并通过校验: {zip_path.stat().st_size / 1048576:.1f} MB")

        napcat_install.extract_zip(
            zip_path, target,
            progress=lambda d, t: self._progress(emit, "fetch", d, t, phase="extract"),
        )
        # 校验通过、解包完成，zip 已无用；留着白占 28MB。
        zip_path.unlink(missing_ok=True)
        step("fetch", f"已解包到 {target}")
        return target

    async def _write_config(self, napcat_dir: Path, uin: str, mode: str,
                            step, emit) -> Path:
        """写入连接配置 —— **反向和正向两条都写**。

        ``onebot_url`` 的含义随模式而变（反向=我们的监听地址，正向=NapCat 的服务端）。
        当前模式那侧用设置里的值，另一侧用该模式的默认值。两条都配好后，用户在
        N.E.K.O 里切模式**不用重配 NapCat、也不用重启它**；只写当前那一条的话，
        每次切模式都得把整套部署重跑一遍。
        """
        token = await self._ensure_token(emit)
        listen = str(self._settings().get("onebot_url") or "")

        if mode == "napcat_forward":
            fwd_host, fwd_port = napcat_onebot_config.host_port_of(listen)
            reverse_url = napcat_onebot_config.DEFAULT_REVERSE_DIAL
        else:
            fwd_host = napcat_onebot_config.DEFAULT_FORWARD_HOST
            fwd_port = napcat_onebot_config.DEFAULT_FORWARD_PORT
            reverse_url = napcat_onebot_config.dial_url(listen)

        path = napcat_onebot_config.onebot_config_path(napcat_dir, uin)
        napcat_onebot_config.apply(
            path, reverse_url=reverse_url, forward_host=fwd_host,
            forward_port=fwd_port, token=token,
        )
        step("config",
             f"已写入双向连接配置（反向: NapCat 连 {reverse_url}；"
             f"正向: NapCat 监听 {fwd_host}:{fwd_port}）: {path.name}")
        return path

    async def _wait_qrcode(self, svc) -> bool:
        deadline = time.time() + QRCODE_WAIT_SECONDS
        while time.time() < deadline:
            try:
                if await svc.sync_napcat_qrcode_into_static():
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
        return False

    # ── 扫码登录后的补写 ────────────────────────────────────

    async def apply_onebot_config(self, *, uin: str = "", restart: bool = True,
                                  emit: Callable[[dict], None] | None = None) -> dict:
        """登录后补写 OneBot 配置（预置路径没走通时的兜底）。

        ``uin`` 为空时自动取最新的 ``onebot11_*.json`` —— 扫码登录成功后 NapCat 会
        自己建出那个文件，靠扫目录就能知道登的是哪个号。
        """
        svc = self.plugin.napcat_service
        napcat_dir = svc.get_napcat_directory()
        if not napcat_dir:
            raise RuntimeError("还没定位到 NapCat 目录，请先执行一键部署")

        uin = str(uin or "").strip()
        if not uin:
            found = napcat_onebot_config.list_onebot_configs(napcat_dir)
            if not found:
                raise RuntimeError(
                    "还没发现任何 onebot11_*.json —— 请先在 NapCat 里扫码登录成功后再试"
                )
            uin = napcat_onebot_config.uin_from_path(found[-1])
            self._emit(emit, "config", f"从配置目录发现已登录账号: {uin}")

        mode = str(self._settings().get("qq_connection_mode") or "napcat")
        path = await self._write_config(napcat_dir, uin, mode,
                                        lambda k, m, **e: self._emit(emit, k, m, **e), emit)

        if restart:
            # NapCat 在启动时读网络配置，改完必须重启才生效。
            await svc.stop_managed_napcat()
            await svc.ensure_napcat_started()
            self._emit(emit, "start", "已重启 NapCat 使配置生效")

        return {"ok": True, "uin": uin, "config_path": str(path), "restarted": bool(restart)}
