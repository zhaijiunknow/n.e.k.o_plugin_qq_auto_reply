# -*- coding: utf-8 -*-
"""随包发布的图片必须是真图片，不能是 Git LFS 指针。

事故（v0.15.6，2026-11-12 从市场安装的那一份）：首页 logo、爪印图标和 4 张教程图在安装
目录里只有 129 字节左右，内容是 ``version https://git-lfs.github.com/spec/v1 …``。

原因有两半：``.gitattributes`` 把所有 ``*.png`` 交给了 LFS；而发版走本体仓库的公共流程
（``plugin-market-release.yml``），它检出插件仓库时不拉 LFS 对象 —— 打进包里的就是指针。
公共流程不归这个仓库管，所以在这边保证：**会被打进包里的文件一律不走 LFS**。

为什么要两条：装了 git-lfs 的开发机上，工作区里永远是真图片，只看文件内容的那条测试在
本地恒绿；真正拦得住回退的是看 ``.gitattributes`` 的那条。
"""

from __future__ import annotations

import fnmatch
import os
import pathlib

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".dsh-artifacts", "__pycache__", ".venv", "node_modules", "data"}
IMAGE_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
    ".ico": (b"\x00\x00\x01\x00",),
}
LFS_POINTER = b"version https://git-lfs"


def _shipped_files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for root, dirs, files in os.walk(PLUGIN):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]     # 不往里走，否则光 .git 就要扫几秒
        found.extend(pathlib.Path(root) / name for name in files)
    return found


def _lfs_patterns() -> list[str]:
    attributes = PLUGIN / ".gitattributes"
    if not attributes.is_file():
        return []
    patterns = []
    for line in attributes.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "filter=lfs" in line.split()[1:]:
            patterns.append(line.split()[0])
    return patterns


def test_no_shipped_file_is_routed_through_git_lfs():
    patterns = _lfs_patterns()
    routed = sorted(
        str(path.relative_to(PLUGIN)).replace("\\", "/")
        for path in _shipped_files()
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)
    )
    assert not routed, (
        "这些文件会被 .gitattributes 交给 Git LFS，而发版流程不拉 LFS 对象，打进包里的会是指针：\n  "
        + "\n  ".join(routed)
    )


def test_every_packaged_image_is_a_real_image():
    broken = []
    for path in _shipped_files():
        magics = IMAGE_MAGIC.get(path.suffix.lower())
        if magics is None:
            continue
        head = path.read_bytes()[:32]
        if head.startswith(LFS_POINTER) or not head.startswith(magics):
            broken.append(f"{path.relative_to(PLUGIN)}（{path.stat().st_size} 字节）")
    assert not broken, "这些图片不是真图片（多半是 LFS 指针）：\n  " + "\n  ".join(broken)
