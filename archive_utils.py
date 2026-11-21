"""Archive validation helpers shared by backend installers."""
import hashlib
from pathlib import Path


def sha256_of(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_member_path(name: str, root: Path) -> Path | None:
    """把压缩包内的条目名解析成 root 下的绝对路径；不安全返回 ``None``。

    拒绝三类：绝对路径、盘符/UNC、以及解析后逃出 root 的（``../`` 穿越，zip-slip）。
    这里选择**拒绝**而不是"清洗成安全名" —— 清洗会让一个被篡改的包看起来装成功了，
    之后在别处以更难排查的方式炸掉。
    """
    if not name or name.endswith("/"):
        return None
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        return None
    if len(normalized) > 1 and normalized[1] == ":":  # C:\... 之类
        return None
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    target = root.joinpath(*parts)
    try:
        resolved = target.resolve()
        base = root.resolve()
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None
    return target
