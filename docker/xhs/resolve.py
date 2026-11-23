"""SPDX-License-Identifier: GPL-3.0-only. Parse one already-fetched XHS page."""

import ctypes
import errno
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
INPUT_LIMIT = 16 * 1024 * 1024


def disable_network() -> None:
    """Install a process-local Linux socket restriction before any upstream import."""
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    allow = 0x7FFF0000  # SCMP_ACT_ALLOW
    deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO
    ctx = lib.seccomp_init(allow)
    if not ctx:
        raise RuntimeError("seccomp initialization failed")
    try:
        # The only input/output descriptors are pipes. Also deny asynchronous socket creation.
        for name in (b"socket", b"connect", b"io_uring_setup"):
            number = lib.seccomp_syscall_resolve_name(name)
            if number < 0 or lib.seccomp_rule_add(ctx, deny, number, 0):
                raise RuntimeError("unsupported parser sandbox")
        if lib.seccomp_load(ctx):
            raise RuntimeError("could not install parser sandbox")
    finally:
        lib.seccomp_release(ctx)


def parse(page_url: str, html: str) -> dict[str, object] | None:
    from source.application.explore import Explore
    from source.application.video import Video
    from source.expansion import Converter, Namespace
    from source.translation import switch_language

    switch_language("zh_CN")
    note = Converter().run(html)
    # A page can contain other recommended works after the requested note disappears.
    target = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    if not target or not isinstance(note, dict) or note.get("noteId") != target:
        return None
    wrapped = Namespace(note)
    metadata = Explore().run(wrapped)
    kind = metadata["作品类型"]
    return {
        "作品ID": target,
        "作品标题": metadata["作品标题"],
        "作品类型": kind,
        "下载地址": Video.deal_video_link(wrapped) if kind == "视频" else [],
    }


def main() -> None:
    # Suppress dependency diagnostics even when a native library writes directly to the fd.
    with os.fdopen(os.dup(1), "w", encoding="utf-8") as output, open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        payload = sys.stdin.buffer.read(INPUT_LIMIT + 1)
        if len(payload) > INPUT_LIMIT:
            raise ValueError("oversized input")
        request = json.loads(payload)
        if set(request) != {"url", "html"} or not all(isinstance(v, str) for v in request.values()):
            raise ValueError("invalid request")
        disable_network()
        sys.path.insert(0, str(ROOT / "upstream"))
        result = parse(request["url"], request["html"])
        json.dump({"data": result}, output, ensure_ascii=False, separators=(",", ":"))
        output.write("\n")


if __name__ == "__main__":
    main()
