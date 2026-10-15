# backend/mcp_stdio.py
"""MCP stdio 纪律层：stdout 只承载 JSON-RPC，日志一律 stderr。

三个职责（按调用顺序）：

1. ``ensure_protocol_stdio()`` —— 在 import FastMCP 之前确认 ``sys.std*`` 是带
   ``.buffer`` 的真实流，否则 JSON-RPC 协议流不通；不可用（无父管道 / 占位流）时
   返回状态串由调用方记日志干净退出。
2. ``isolate_stdout()`` —— RimCrow logger 默认写 stdout，会污染 JSON-RPC；
   把所有控制台 StreamHandler 重定向到 stderr（与 fork 实现一致）。
3. ``stdio_entry()`` —— 冻结窗口化 exe 捕获到未处理异常时 bootloader 会弹
   ``Unhandled exception in script`` 模态框并挂住；
   stdio 入口必须把一切异常收进日志后干净退出。

本模块只依赖标准库，任何 MCP 宿主（工具层 stdio 测试载体等）import 均无副作用。
生产 stdio 入口是独立的 ``rimcrow-mcp.exe``（Rust 桥）。
"""

from __future__ import annotations

import contextlib
import functools
import io
import logging
import os
import sys
import traceback
from collections.abc import Iterator, Callable
from pathlib import Path
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

# quiet_import 用过的静默缓冲（isolate_stdout 用它认领“绑在缓冲上的控制台 handler”）。
_QUIET_BUFFERS: list[io.StringIO] = []

# 已认领的控制台 handler（identity 记账）：后续调用**无条件重指当前 sys.stderr**——
# 否则「宿主换过 stderr 对象后再 isolate」按流特征匹配不到旧目标（重指逻辑保留为
# 防御：日志在 isolate 之后重建 stderr 的场景仍成立）。
_CLAIMED_CONSOLE_HANDLERS: list[logging.Handler] = []

__all__ = [
    "anchor_project_root",
    "ensure_protocol_stdio",
    "isolate_stdout",
    "quiet_import",
    "stdio_entry",
]


def anchor_project_root() -> None:
    """锚定项目根，保证 backend.settings 的 DATA_DIR 等路径正确（与 main.py 同款）。

    冻结态以 exe 所在目录为根，开发态以 backend/ 的上一级为根。
    """
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def ensure_protocol_stdio() -> str:
    """确认 ``sys.std*`` 是可用的、带 ``.buffer`` 的真实流（否则 stdio 协议走不通）。

    返回状态串供日志诊断：

    - ``inplace``   标准流本来就可用，未做任何修改（dev / smoke / 测试载体的常态）；
    - ``no-handle`` 无父管道（双击 / 父进程未绑定）或标准流是占位对象，
                    调用方应记日志干净退出；
    - ``unsupported`` 非 Windows 且标准流不可用，交给上层按 no-handle 处理。

    本函数不做 Win32 句柄重建：那只为「冻结窗口化 stdio exe」（``sys.std*`` 被
    ``_NullTextStream`` 占位）而需要，而生产 stdio 入口是 ``rimcrow-mcp.exe``
    （Rust 桥，不 import 本模块），``python -m backend.mcp_server`` 始终运行在
    有真实管道的解释器下（smoke/pytest 均为 subprocess PIPE）；
    ``pack_pyinstaller`` / ``pack_nuitka`` 对 ``mcp_server`` 零引用（冻结态无入口）。
    """
    have_stdin = sys.stdin is not None and getattr(sys.stdin, "buffer", None) is not None
    have_stdout = getattr(sys.stdout, "buffer", None) is not None
    if have_stdin and have_stdout:
        return "inplace"
    if os.name != "nt":
        return "unsupported"
    return "no-handle"


def _is_mutable_console_stream(stream: Any) -> bool:
    """判断 handler 当前的流是不是「stdout 侧控制台流」（可安全改指 stderr）。

    - 文件 handler 的流是带 ``.buffer`` 的真实文件 → 不可动；
    - 已在 stderr（当前对象）上 → 无需动；``stream is None``（delay 型未打开）→ 不动；
    - ``sys.stdout`` / ``sys.__stdout__`` / quiet 缓冲 / 无 ``.buffer`` 的占位流
      （``_NullTextStream`` 等哑流）→ 控制台流，认领。
    """
    if stream is None:
        return False
    if stream is sys.stderr or stream is sys.__stderr__:
        return False
    if stream is sys.stdout or stream is sys.__stdout__:
        return True
    if any(stream is buffer for buffer in _QUIET_BUFFERS):
        return True
    return not hasattr(stream, "buffer")


def isolate_stdout(logger: logging.Logger | None = None) -> None:
    """把 **控制台** StreamHandler 指向 stderr，stdout 专供 JSON-RPC。

    只认领「流是 stdout 侧 / 静默缓冲 / 占位哑流」的 handler，**绝不碰文件轮转
    handler**——``TimedRotatingFileHandler`` 同样是 ``StreamHandler`` 子类，误改会把
    app.log 的写入重定向到 stderr；
    ``WebviewHandler`` 是普通 Handler 天然豁免。只动 ``RimCrow`` logger
    （``propagate=False``），不碰 root logger（由宿主框架管理）。

    幂等且**可重入认领**：已认领过的 handler 无条件重指「当前」``sys.stderr``——
    stdio 形态在模块导入时认领一次、宿主若在其后替换过 ``sys.stderr``（重建流）
    再调用一次即可真正换到新对象（仅按流特征匹配够不到旧目标）。
    """
    target = logger or logging.getLogger("RimCrow")
    for handler in target.handlers:
        if not isinstance(handler, logging.StreamHandler):
            continue
        already_claimed = any(handler is claimed for claimed in _CLAIMED_CONSOLE_HANDLERS)
        if not already_claimed and not _is_mutable_console_stream(handler.stream):
            continue
        handler.stream = sys.stderr
        if not already_claimed:
            _CLAIMED_CONSOLE_HANDLERS.append(handler)


@contextlib.contextmanager
def quiet_import() -> Iterator[None]:
    """import 期间把 stdout 换成缓冲，吞掉导入链路上的控制台输出。

    RimCrow 的日志/打印在 import 期可能落到 stdout，
    进入协议流即断管。with 块内完成 backend 导入后自动恢复真实 stdout。
    缓冲对象登记进 ``_QUIET_BUFFERS``，供 :func:`isolate_stdout` 识别
    「绑在静默缓冲上的控制台 handler」。
    """
    real_stdout = sys.stdout
    buffer = io.StringIO()
    _QUIET_BUFFERS.append(buffer)
    sys.stdout = buffer
    try:
        yield
    finally:
        sys.stdout = real_stdout


def stdio_entry(fn: Callable[[], None]) -> Callable[[], int]:
    """把 stdio 入口包成“永不抛出”的退出码函数（冻结窗口化 exe 不弹窗的关键）。

    - 正常返回 → 0；
    - ``SystemExit(n)`` → n；
    - 其它异常 → 写 stderr 日志 + traceback，返回 1（禁止冒泡到 bootloader）。
    """

    @functools.wraps(fn)
    def wrapper() -> int:
        try:
            fn()
        except SystemExit as exc:
            code = exc.code
            if code is None:
                return 0
            return code if isinstance(code, int) else 1
        except BaseException:  # noqa: BLE001 - 冻结态最后一道防线，必须吞干净
            sys.stderr.write("".join(traceback.format_exc()))
            sys.stderr.flush()
            return 1
        return 0

    return wrapper
