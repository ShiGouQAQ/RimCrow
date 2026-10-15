# backend/mcp_endpoint.py
"""endpoint 文件格式（schema_version=1）：mcp-embed 写，stdio 入口读（生产 rimcrow-mcp.exe；Python 侧工具层 stdio 测试形态亦读）。

默认落在 ``%LOCALAPPDATA%\\RimCrow\\mcp-endpoint.json``：与安装目录解耦（``DATA_DIR``
= ``HOME_DIR/data`` 随程序目录走，便携安装/只读介质/升级重装下不可写或不可靠），
``%LOCALAPPDATA%`` 是用户级 ACL 的稳定可写位置，且对任何 cwd/启动方式一致。
**不是**「-savedatafolder 按档案分裂」——那是 RimCrow 传给游戏进程的参数
（mgr_profile.py），RimCrow 自身 DATA_DIR 不随档案变化。
``RIMCROW_MCP_ENDPOINT`` 环境变量可覆盖（测试/沙盒）。

写出 = 临时文件 + 原子 ``os.replace``；陈旧判定（stdio 入口侧）= pid 不存活、
**或进程名不匹配、或进程启动时间与 started_at 不吻合**（Windows pid 会复用，
必须做二次校验）。

本模块只依赖标准库 + psutil，任何 MCP 宿主 import 均无后端副作用。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "EndpointInfo",
    "default_endpoint_path",
    "probe_endpoint",
    "read_endpoint",
    "remove_endpoint",
    "write_endpoint",
]

SCHEMA_VERSION = 1
ENDPOINT_ENV = "RIMCROW_MCP_ENDPOINT"

# started_at 与进程 create_time 的允许偏差（秒）：
# - 下界（started_at >= create - CLOCK_SKEW）：endpoint 必须在该进程生命周期内写出；
#   pid 被新进程复用时 create_time 会晚于旧 started_at → 判陈旧。
# - 上界（started_at <= create + MAX_BOOT）：endpoint 写出距进程启动最多这么久；
#   超出说明同名进程复用了 pid（或系统时钟跳变）→ 判陈旧。
CLOCK_SKEW = 5.0
MAX_BOOT = 300.0


@dataclass(frozen=True, slots=True)
class EndpointInfo:
    """endpoint 文件的结构化形态。"""

    url: str
    pid: int
    token: str
    started_at: str
    mode: str
    schema_version: int = SCHEMA_VERSION
    path: Path | None = None  # 不序列化进文件，仅读回时携带

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("path", None)
        return payload


def default_endpoint_path() -> Path:
    """endpoint 文件路径：env 覆盖 → %LOCALAPPDATA%\\RimCrow\\mcp-endpoint.json。"""
    override = os.environ.get(ENDPOINT_ENV, "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "RimCrow" / "mcp-endpoint.json"


def write_endpoint(
    *,
    url: str,
    pid: int,
    token: str,
    mode: str,
    path: Path | None = None,
    started_at: datetime | str | None = None,
) -> EndpointInfo:
    """原子写出 endpoint 文件（tmp + os.replace），返回落盘后的 EndpointInfo。"""
    target = path or default_endpoint_path()
    if started_at is None:
        started_text = datetime.now().astimezone().isoformat()
    elif isinstance(started_at, str):
        started_text = started_at
    else:
        started_text = started_at.isoformat()
    info = EndpointInfo(
        url=url,
        pid=int(pid),
        token=token,
        started_at=started_text,
        mode=mode,
        schema_version=SCHEMA_VERSION,
        path=target,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(info.to_payload(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return info


def read_endpoint(path: Path | None = None) -> EndpointInfo | None:
    """读取 endpoint 文件；不存在或损坏返回 None（损坏原因由 probe_endpoint 细分）。"""
    target = path or default_endpoint_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return EndpointInfo(
            url=str(raw["url"]),
            pid=int(raw["pid"]),
            token=str(raw["token"]),
            started_at=str(raw.get("started_at") or ""),
            mode=str(raw.get("mode") or ""),
            schema_version=int(raw.get("schema_version") or 0),
            path=target,
        )
    except (KeyError, TypeError, ValueError):
        return None


def remove_endpoint(path: Path | None = None) -> None:
    """删除 endpoint 文件（缺失不报错；退出清理用）。"""
    target = path or default_endpoint_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # 权限异常不应炸退出流程
        sys.stderr.write(f"[mcp] endpoint cleanup failed: {exc}\n")


def _expected_process_names() -> set[str]:
    """探活允许的进程名集合（casefold）。

    - 当前解释器名（dev 态 GUI 与 attach 都是 ``python.exe``）；
    - 打包主程序名 ``RimCrow.exe``（``pack_pyinstaller.py`` 的 APP_NAME）——
      覆盖「frozen GUI × dev attach」等混用边界：endpoint 是给**同机任何
      形态客户端**用的，进程名只做 pid 复用的第一道筛，启动时间比对才是主防线。
    """
    return {Path(sys.executable).name.casefold(), "rimcrow.exe"}


def probe_endpoint(path: Path | None = None) -> tuple[str, EndpointInfo | None, str]:
    """探活：返回 ``(status, info, reason)``，status ∈ missing / stale / ready。

    reason 是给 agent 看的因果陈述（含文件路径与 pid 事实，不含第三方内容），
    直接进调用方的错误文案（错误信息随本次调用返回；生产消费方 = rimcrow-mcp.exe）。
    """
    import psutil

    target = path or default_endpoint_path()
    info = read_endpoint(target)
    if info is None:
        if target.exists():
            return "stale", None, f"endpoint 文件损坏或字段缺失：{target}"
        return "missing", None, f"endpoint 文件不存在：{target}"
    if info.schema_version != SCHEMA_VERSION:
        return "stale", info, f"endpoint schema_version={info.schema_version} 不受支持（期望 {SCHEMA_VERSION}）：{target}"
    if not str(info.url).startswith("http://127.0.0.1:"):
        return "stale", info, f"endpoint url 非 loopback，拒绝连接：{info.url}"

    try:
        process = psutil.Process(info.pid)
    except psutil.NoSuchProcess:
        return "stale", info, f"RimCrow 未运行（endpoint 中的 pid {info.pid} 已不存在）：{target}"
    except OSError as exc:
        return "stale", info, f"无法校验 pid {info.pid}（{exc}）：{target}"

    expected_names = _expected_process_names()
    try:
        actual_name = process.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        actual_name = ""
    if actual_name and actual_name.casefold() not in expected_names:
        allowed = "/".join(sorted(expected_names))
        return "stale", info, f"pid {info.pid} 已被其它进程占用（{actual_name} ∉ {allowed}）：{target}"

    started_epoch = _parse_epoch(info.started_at)
    create_epoch = _safe_create_time(process)
    if started_epoch is not None and create_epoch is not None:
        if started_epoch < create_epoch - CLOCK_SKEW:
            return "stale", info, f"pid {info.pid} 启动时间晚于 endpoint 写出时间（pid 复用）：{target}"
        if started_epoch > create_epoch + MAX_BOOT:
            return "stale", info, f"pid {info.pid} 与 endpoint 的 started_at 不吻合：{target}"
    return "ready", info, "ok"


def _parse_epoch(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _safe_create_time(process: Any) -> float | None:
    try:
        return float(process.create_time())
    except Exception:  # noqa: BLE001 - 拿不到时间就退回“不校验”
        return None
