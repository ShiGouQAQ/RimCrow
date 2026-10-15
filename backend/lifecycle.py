# backend/lifecycle.py
"""数据面单实例互斥。

数据面与 GUI 属于重运行时进程，必须单例：所有会竞争写同一份 endpoint 文件
（``backend.mcp_endpoint``，默认 ``%LOCALAPPDATA%\\RimCrow\\mcp-endpoint.json``）
的实例互斥——endpoint 文件是唯一权威，双开 = last-writer-wins 孤儿子弹。
CLI worker 等一次性子进程不参与互斥。第二实例检测到已有实例后由调用方
（main.py）打印一行提示并 ``exit 0``：不唤起、不弹窗、不开窗口、不初始化后端。

实现：Windows named mutex（``kernel32.CreateMutexW``，经 ``ctypes`` 直调）。

- **句柄生命周期语义**：句柄收进 ``_LIVE_HANDLES`` 后不主动 ``CloseHandle``，
  锁随进程存续（进程退出由 OS 回收）。pywin32 的 ``PyHANDLE`` 在 GC 时自动
  ``CloseHandle``，最后一个句柄关闭即销毁命名互斥对象，表达不了「锁 = 进程
  生命周期」，因此不采用；文件锁（``portalocker`` / ``filelock``）是锁文件粒度，
  与命名内核对象互斥语义不等价，同样不适用。
- **互斥名绑定 endpoint 路径**：``Local\\RimCrowEndpoint-<sha1(endpoint_path
  lowercase)[:16]>``。换 endpoint（``RIMCROW_MCP_ENDPOINT``）即换互斥域，
  测试 / 沙盒 / 多档案进程天然并行。路径按小写哈希：**Windows 命名内核对象名
  比较是大小写敏感的**（MS *Object Names* / *CreateMutexW*：「Name comparison
  is case sensitive.」），``C:\\Foo`` 与 ``c:\\foo`` 会落到不同的两把锁上，
  统一小写归一把它们收拢成同一把。
- **生命周期 = 进程生命周期**：句柄获取后不主动释放（不 CloseHandle），进程退出
  时由 OS 自动回收；占用判定只认内核对象，不用「endpoint 文件存在」这类软判断
  （陈旧文件会误伤，新进程没写文件又会漏判）。
- **获取时机**（main.py）：--steam-worker 分支之后、``validate_environment`` 与
  ``from backend.api import API`` 之前——一次性子进程不参与互斥，而第二实例必须
  零 GUI 痕迹（不弹原生错误框、不导入后端重活）。
- **跳过开关**：``RIMCROW_NO_SINGLE_INSTANCE=1`` 完全跳过互斥（测试/开发并行跑）。
- **非 Windows 平台**：跳过互斥（本项目只在 Windows 运行；代码不得在 Linux 上崩）。
- **fail-open**：拿不到内核对象（权限/命名异常）时警告后按「无互斥」继续——
  互斥故障不应导致应用永远起不来，双开可诊断、起不来不可诊断。
- **短宽限轮询**：已有实例恰好在「拉起新实例 → os._exit(0)」的重启窗口里时，
  默认再等 ``DEFAULT_GRACE_SECONDS`` 才判忙，吸收调度抖动（重启流程见
  ``backend/utils/restart.py``：Popen 之后立即 os._exit，宽限期只需百毫秒级）。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from backend.mcp_endpoint import default_endpoint_path, read_endpoint

__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "ESCAPE_ENV",
    "MUTEX_PREFIX",
    "STATUS_ACQUIRED",
    "STATUS_BUSY",
    "STATUS_SKIPPED",
    "SingleInstanceResult",
    "acquire_single_instance",
    "endpoint_mutex_name",
    "release_single_instance",
    "report_second_instance",
]

# 互斥对象名前缀：Local\ = 当前登录会话命名空间（同用户会话内互斥，不跨会话污染）
MUTEX_PREFIX = "Local\\RimCrowEndpoint-"

# 跳过开关：测试 / 开发并行运行时置 1 跳过互斥
ESCAPE_ENV = "RIMCROW_NO_SINGLE_INSTANCE"
_ESCAPED_VALUES = {"1", "true", "yes", "on"}

STATUS_ACQUIRED = "acquired"   # 拿到互斥，本进程是唯一数据面实例
STATUS_BUSY = "busy"           # 已有实例在跑，调用方应打印提示后 exit 0
STATUS_SKIPPED = "skipped"     # 跳过开关 / 非 Windows / 创建失败 → 不互斥继续启动

ERROR_ALREADY_EXISTS = 183

# 判忙前的宽限轮询时长（秒）：只为吸收「旧实例 Popen 新实例后还没来得及
# os._exit」的重启窗口；真实双开也只多等这么久就静默退出。
DEFAULT_GRACE_SECONDS = 0.25
_GRACE_POLL_SECONDS = 0.05

# 本进程持有的互斥句柄登记表（仅登记、不自动关闭；release_single_instance 才关）
_LIVE_HANDLES: dict[str, int] = {}


@dataclass(frozen=True, slots=True)
class SingleInstanceResult:
    """互斥获取结果：status + 名称 + 句柄（仅 acquired 时非 None）。"""

    status: str
    name: str | None = None
    handle: int | None = None

    @property
    def acquired(self) -> bool:
        return self.status == STATUS_ACQUIRED

    @property
    def busy(self) -> bool:
        return self.status == STATUS_BUSY


def endpoint_mutex_name(endpoint: Path | str | None = None) -> str:
    """互斥对象名：``Local\\RimCrowEndpoint-<sha1(路径小写)[:16]>``。

    ``endpoint=None`` 时取 ``default_endpoint_path()``（已含
    ``RIMCROW_MCP_ENDPOINT`` env 覆盖）；显式传入仅供测试与自省。
    """
    path = Path(endpoint) if endpoint is not None else default_endpoint_path()
    digest = hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:16]
    return MUTEX_PREFIX + digest


def _escape_hatch_enabled() -> bool:
    return os.environ.get(ESCAPE_ENV, "").strip().lower() in _ESCAPED_VALUES


def _kernel32():
    """惰性加载 kernel32（只在 win32 分支触达，非 Windows 平台 import 不受影响）。"""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _create_mutex(name: str) -> tuple[int | None, bool]:
    """CreateMutexW（不抢初始所有权）→ (句柄, 是否已存在)。失败 → (None, False)。"""
    kernel32 = _kernel32()
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None, False
    return int(handle), ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def _close_handle(handle: int) -> None:
    try:
        _kernel32().CloseHandle(handle)
    except Exception:  # 关不掉就随进程退出回收，不值得打断启动/退出流程
        pass


def acquire_single_instance(
    *, grace_seconds: float = DEFAULT_GRACE_SECONDS
) -> SingleInstanceResult:
    """获取数据面单实例互斥。

    - ``acquired``：本进程成为唯一实例，句柄保有到进程退出（不主动释放）。
    - ``busy``：宽限期内一直被占用 → 已有实例在跑，调用方应 exit 0。
    - ``skipped``：跳过开关 / 非 Windows / 创建失败 → 不互斥，照常启动。

    ``grace_seconds=0`` 立即判定（测试用）。
    """
    if _escape_hatch_enabled():
        return SingleInstanceResult(STATUS_SKIPPED)
    if sys.platform != "win32":
        return SingleInstanceResult(STATUS_SKIPPED)

    name = endpoint_mutex_name()
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while True:
        handle, already_exists = _create_mutex(name)
        if handle is None:
            # 拿不到内核对象属于异常环境（权限、命名空间损坏等）：fail-open。
            sys.stderr.write(
                f"[RimCrow] single-instance mutex create failed "
                f"(name={name}); continuing without mutex\n"
            )
            return SingleInstanceResult(STATUS_SKIPPED, name=name)
        if not already_exists:
            # 已创建并持有：句柄从此随进程存续，不 CloseHandle。
            _LIVE_HANDLES[name] = handle
            return SingleInstanceResult(STATUS_ACQUIRED, name=name, handle=handle)
        # 已存在 = 已有实例在跑。关掉本次多余打开的引用（持有者的句柄不受影响）。
        _close_handle(handle)
        if time.monotonic() >= deadline:
            return SingleInstanceResult(STATUS_BUSY, name=name)
        time.sleep(_GRACE_POLL_SECONDS)


def release_single_instance(result: SingleInstanceResult) -> None:
    """主动释放互斥（**仅测试 / 工具使用**；生产路径靠进程退出由 OS 回收句柄）。"""
    handle = result.handle
    if handle is None:
        return
    _close_handle(handle)
    if result.name and _LIVE_HANDLES.get(result.name) == handle:
        del _LIVE_HANDLES[result.name]


def _existing_instance_hint() -> str:
    """尽力从 endpoint 文件里取已有实例的 pid/url（读不到就省略，不影响退出）。"""
    try:
        info = read_endpoint()
    except Exception:
        return ""
    if info is None:
        return ""
    return f" existing_pid={info.pid} existing_url={info.url} existing_mode={info.mode}"


def report_second_instance(result: SingleInstanceResult) -> str:
    """给第二实例的一行提示：先走 logger（文件 + 控制台），设施不可用退回 stderr。

    只输出文字，不弹 MessageBox、不唤起已有实例。返回该行文本（便于测试断言）。
    """
    message = (
        "RimCrow 已有实例在运行，本次启动静默退出（exit 0）："
        f"mutex={result.name} endpoint={default_endpoint_path()}"
        f"{_existing_instance_hint()}"
    )
    emitted = False
    try:
        from backend.utils.logger import logger

        if getattr(logger, "handlers", None):
            logger.info(message)
            emitted = True
    except Exception:
        emitted = False
    if not emitted:
        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except Exception:
            pass
    return message
