"""QQ 开放平台富媒体上传 —— 图片走这条路，群聊与单聊各一份。

**为什么单独一个模块**：上传流程比"发一条消息"复杂得多，而且是**两条并存的协议**：

1. **旧式直传**（本仓库群聊一直在用的那条，见 ``qq_open_plat._upload_group_image``）：
   ``POST /v2/{scope}/{id}/files {file_type, file_name, file_size, mime_type}``
   → 响应里给 ``upload_url`` → 客户端 ``PUT`` 字节 → 拿 ``file_info``。
2. **当前官方文档**（bot.q.qq.com wiki，2026-07/08 更新）只列另外两条：
   * **URL 上传**：``{file_type, url, srv_send_msg: false}``，平台自己去下载转存
     —— 只接受 http(s) 地址，**本地文件走不了**；
   * **分片上传**：``upload_prepare``（要给 ``file_size``/``md5``/``sha1``/``md5_10m``）
     → 逐片 ``PUT`` 预签名 URL → 每片 ``upload_part_finish`` → 带 ``upload_id``
     调 ``files`` 合并。

   文档里**没有**第 1 条的字段（``file_size``/``mime_type``/``upload_url``）——
   也就是说群聊那条直传到底还算不算数，只能靠真机才知道。所以这里的策略是
   **两条都试**：本地文件先试旧式直传（与既有行为完全一致，不会比今天更差），
   失败再走文档的分片上传；http 地址直接走文档的 URL 上传。哪条成功都会写日志，
   下次真机接上开放平台时，日志本身就回答了"哪条还活着"。

**宿主副本问题**：运行时优先用宿主那份连接器（``connector_seam``），而插件**改不了宿主
的文件**。所以这些函数写成**自由函数**（第一个参数是连接对象），插件侧对任何一份连接
都能用；副本里的方法只是薄薄一层转发。函数只依赖连接对象的这几个成员
（``_http`` / ``_API_BASE`` / ``_ensure_token`` / ``_auth_headers`` / ``logger``），
``tests/test_qq_open_platform_media.py`` 里有针对它们的漂移守卫。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from typing import Any

#: 平台口径的媒体类型。
FILE_TYPE_IMAGE = 1

#: 图片软限制（超过平台会把图片降级成"文件"类型，我们宁可降级成文字也不偷偷改语义）。
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: ``upload_prepare`` 要的 ``md5_10m`` = **前 10002432 字节**的 MD5（文档原文）。
_MD5_10M_BYTES = 10_002_432


def is_open_platform(conn: Any) -> bool:
    """这个连接是不是 QQ 开放平台。

    读 ``CHANNEL`` 优先（``"open"``，与 ``OneBotClient.CHANNEL`` 的取值域一致），
    再退到 ``mode``。两个都是既有属性，不新增协议字段。
    """
    if str(getattr(conn, "CHANNEL", "") or "").strip() == "open":
        return True
    return str(getattr(conn, "mode", "") or "").strip() == "open_platform"


def _api_base(conn: Any) -> str:
    return str(getattr(conn, "_API_BASE", "") or "").rstrip("/")


def _log(conn: Any, level: str, message: str) -> None:
    logger = getattr(conn, "logger", None)
    if logger is None:
        return
    try:
        getattr(logger, level, logger.info)(f"[QQOpenPlatform] {message}")
    except Exception:
        pass


async def _post(conn: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """带鉴权的 POST，返回解析后的 JSON（拿不到就空 dict，绝不抛给调用方）。"""
    response = await conn._http.post(
        f"{_api_base(conn)}{path}", json=body, headers=conn._auth_headers(),
    )
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _local_path(source: str) -> str:
    text = str(source or "").strip()
    if text.startswith("file://"):
        text = text[7:]
    return text


def _read_source(source: str) -> tuple[bytes, str]:
    """读本地文件，返回 ``(字节, 文件名)``；读不了就是 ``(b"", "")``。"""
    path = _local_path(source)
    if not path or not os.path.isfile(path):
        return b"", ""
    with open(path, "rb") as handle:
        return handle.read(), os.path.basename(path)


def _digests(payload: bytes) -> dict[str, str]:
    return {
        "md5": hashlib.md5(payload).hexdigest(),
        "sha1": hashlib.sha1(payload).hexdigest(),
        "md5_10m": hashlib.md5(payload[:_MD5_10M_BYTES]).hexdigest(),
    }


async def _upload_by_url(conn: Any, *, scope: str, owner_id: str, url: str, file_type: int) -> str:
    """文档里的 URL 上传：把地址给平台，由平台下载转存。"""
    data = await _post(
        conn, f"/v2/{scope}/{owner_id}/files",
        {"file_type": file_type, "url": url, "srv_send_msg": False},
    )
    return str(data.get("file_info") or "")


async def _upload_chunked(
    conn: Any, *, scope: str, owner_id: str, payload: bytes, file_name: str, file_type: int,
) -> str:
    """文档里的分片上传：prepare → 逐片 PUT + part_finish → 带 upload_id 合并。"""
    digests = _digests(payload)
    prepare = await _post(
        conn, f"/v2/{scope}/{owner_id}/upload_prepare",
        {
            "file_type": file_type,
            "file_size": str(len(payload)),
            "file_name": file_name,
            **digests,
        },
    )
    upload_id = str(prepare.get("upload_id") or "")
    parts = prepare.get("parts")
    if not upload_id or not isinstance(parts, list) or not parts:
        return ""

    mime_type = mimetypes.guess_type(file_name)[0] or "image/png"
    ordered = sorted(
        (p for p in parts if isinstance(p, dict)),
        key=lambda p: int(p.get("index") or 0),
    )
    offset = 0
    for part in ordered:
        try:
            size = int(part.get("block_size") or 0)
        except (TypeError, ValueError):
            size = 0
        chunk = payload[offset:offset + size] if size > 0 else payload[offset:]
        presigned = str(part.get("presigned_url") or "")
        if not chunk or not presigned:
            return ""
        await conn._http.put(presigned, content=chunk, headers={"Content-Type": mime_type})
        await _post(
            conn, f"/v2/{scope}/{owner_id}/upload_part_finish",
            {
                "upload_id": upload_id,
                "part_index": int(part.get("index") or 0),
                "block_size": str(len(chunk)),
                "md5": hashlib.md5(chunk).hexdigest(),
            },
        )
        offset += len(chunk)

    if offset != len(payload):
        # 分片列表没覆盖完整个文件：合并上去就是**残缺文件**，而平台不会替我们
        # 发现这件事。宁可这次不发图，也不要上传一个坏文件后说"成功了"。
        _log(conn, "warning", f"分片只覆盖 {offset}/{len(payload)} 字节，放弃合并")
        return ""

    merged = await _post(
        conn, f"/v2/{scope}/{owner_id}/files",
        {"file_type": file_type, "upload_id": upload_id, "srv_send_msg": False, "file_name": file_name},
    )
    return str(merged.get("file_info") or "")


async def _upload_legacy(
    conn: Any, *, scope: str, owner_id: str, payload: bytes, file_name: str, file_type: int,
) -> str:
    """旧式直传：申请 ``upload_url`` 再 PUT。仓库里群聊原来一直在用这条。"""
    mime_type = mimetypes.guess_type(file_name)[0] or "image/png"
    data = await _post(
        conn, f"/v2/{scope}/{owner_id}/files",
        {
            "file_type": file_type,
            "file_name": file_name,
            "file_size": len(payload),
            "mime_type": mime_type,
        },
    )
    upload_url = str(data.get("upload_url") or "")
    if not upload_url:
        return ""
    response = await conn._http.put(upload_url, content=payload, headers={"Content-Type": mime_type})
    file_info = ""
    try:
        file_info = str((response.json() or {}).get("file_info") or "")
    except Exception:
        file_info = ""
    return file_info or str(data.get("file_info") or "")


async def upload_image(conn: Any, *, scope: str, owner_id: str, source: str) -> str:
    """把一张图传到 ``scope``（``"groups"`` / ``"users"``），返回 ``file_info``。

    ``scope`` 是**平台语义**上的隔离：用单聊接口传的只能发到单聊，反之亦然
    （文档原话），所以调用方必须传对。失败一律返回空串，由调用方决定怎么降级。
    """
    url = str(source or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        try:
            await conn._ensure_token()
            file_info = await _upload_by_url(
                conn, scope=scope, owner_id=owner_id, url=url, file_type=FILE_TYPE_IMAGE,
            )
        except Exception as exc:
            _log(conn, "warning", f"图片 URL 上传异常: {exc}")
            return ""
        if file_info:
            _log(conn, "info", f"图片上传成功(url): {file_info[:24]}")
        else:
            _log(conn, "warning", "图片 URL 上传失败")
        return file_info

    try:
        payload, file_name = _read_source(url)
    except Exception as exc:
        _log(conn, "warning", f"图片读取失败: {exc}")
        return ""
    if not payload:
        _log(conn, "warning", f"图片文件不存在或为空: {_local_path(url)}")
        return ""
    if len(payload) > MAX_IMAGE_BYTES:
        _log(
            conn, "warning",
            f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 软限制，放弃上传: {len(payload)} 字节",
        )
        return ""

    try:
        await conn._ensure_token()
    except Exception as exc:
        _log(conn, "warning", f"取 token 失败，无法上传图片: {exc}")
        return ""

    # 两条都试，旧式优先：与仓库既有行为一致，不会比今天更差。
    for label, attempt in (
        ("直传", _upload_legacy),
        ("分片", _upload_chunked),
    ):
        try:
            file_info = await attempt(
                conn, scope=scope, owner_id=owner_id,
                payload=payload, file_name=file_name, file_type=FILE_TYPE_IMAGE,
            )
        except Exception as exc:
            _log(conn, "warning", f"图片{label}上传异常: {exc}")
            continue
        if file_info:
            _log(conn, "info", f"图片上传成功({label}): {file_info[:24]}")
            return file_info
        _log(conn, "warning", f"图片{label}上传未拿到 file_info")
    return ""


async def send_private_image(
    conn: Any, user_id: str, source: str, *,
    content: str = "", reply_message_id: str = "", record_sent: bool = True,
) -> str | None:
    """给单聊发一张图（文档口径：``msg_type=7`` + ``media.file_info``）。

    返回消息 id；任何一步失败都返回 ``None``，由调用方降级成文字。
    """
    target = str(user_id or "").strip()
    if not target:
        return None
    file_info = await upload_image(conn, scope="users", owner_id=target, source=source)
    if not file_info:
        return None
    body: dict[str, Any] = {"msg_type": 7, "media": {"file_info": file_info}}
    text = str(content or "").strip()
    if text:
        body["content"] = text
    reply_id = str(reply_message_id or "").strip()
    if reply_id:
        body["msg_id"] = reply_id
    try:
        data = await _post(conn, f"/v2/users/{target}/messages", body)
    except Exception as exc:
        _log(conn, "warning", f"发送单聊图片失败: {exc}")
        return None
    message_id = str(data.get("id") or "")
    if message_id and record_sent:
        try:
            conn.record_sent_message_id(message_id)
        except Exception:
            pass
    return message_id or None


async def send_group_image(
    conn: Any, group_id: str, source: str, *,
    content: str = "", reply_message_id: str = "", at_user_id: str = "", record_sent: bool = False,
) -> str | None:
    """给群聊发一张图（``msg_type=7`` + ``media.file_info``）。

    **为什么群聊也要走这里** —— 2026-09-26 真机实测逼出来的。投递层原来直接调连接器的
    ``send_group_image``，而那份（宿主副本）只实现了**旧式直传**。真机上旧式直传在开放
    平台**已经失效**，日志原文：

        [QQOpenPlatform] 图片直传上传未拿到 file_info
        [QQOpenPlatform] 图片上传成功(分片): wIFo43EanZwsn01Ru9mCJ9rU

    也就是说**群聊表情包在开放平台上一直是坏的**（静默降级成 `[图片]` 三个字），
    而私聊那条只是因为走了本模块的分片兜底才成功。两边现在走同一条。
    """
    target = str(group_id or "").strip()
    if not target:
        return None
    file_info = await upload_image(conn, scope="groups", owner_id=target, source=source)
    if not file_info:
        return None
    body: dict[str, Any] = {"msg_type": 7, "media": {"file_info": file_info}}
    at = str(at_user_id or "").strip()
    text = str(content or "").strip()
    if at or text:
        body["content"] = (f"<@!{at}>" if at else "") + text
    reply_id = str(reply_message_id or "").strip()
    if reply_id:
        body["msg_id"] = reply_id
    try:
        data = await _post(conn, f"/v2/groups/{target}/messages", body)
    except Exception as exc:
        _log(conn, "warning", f"发送群聊图片失败: {exc}")
        return None
    message_id = str(data.get("id") or "")
    if message_id and record_sent:
        try:
            conn.record_sent_message_id(message_id)
        except Exception:
            pass
    return message_id or None
