# scripts/gen_tool_cache.py
"""构建期清单快照生成器。

从工具清单唯一真相源 ``backend.mcp_tool_specs`` 出发，经
``backend.mcp_server.create_mcp_app`` 的**真实 FastMCP 注册结果**导出
``tools-cache.json``，供 Rust 薄封装层 ``rimcrow-mcp.exe`` 编译期 ``include_str!``
嵌入——GUI 未开时的备用清单（跨语言快照）。

同源要求（**schema 不手工复制**）：name / description / inputSchema / annotations
一律取自 ``mcp.list_tools()`` 的实际注册产物（经 FastMCP 自己的
``Tool.to_mcp_tool()`` 序列化为协议线上形态）——从函数签名手写 JSON schema 会与
运行时走样；一致性由 ``tests/test_tool_cache_generator.py`` 校验。instructions /
rimcrow_version / server_name 同样单源（``SERVER_INSTRUCTIONS`` / ``backend._version``）。

零后端副作用：不 import ``backend.api``、不连数据库。``create_mcp_app`` /
``ManifestOnlyApi`` 在函数体内 lazy import（模块导入期只有标准库）；占位 api
保证任何方法被调用直接 raise，不会触碰真实后端。

格式约定 v1（与 Rust 桥逐字段一致）::

    {"schema_version": 1, "rimcrow_version": ..., "captured_at": ISO8601 UTC,
     "server_name": "rimcrow-mcp", "instructions": ..., "tools": [
        {"name", "description", "inputSchema", "annotations"} ...]}

用法：``uv run python scripts/gen_tool_cache.py [--out <path>]``
stdout 仅一行摘要（路径 + 工具数 + rimcrow_version）；退出码 0 = 写出成功，非 0 = 失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA_VERSION = 1
SERVER_NAME = "rimcrow-mcp"

def _default_out() -> Path:
    """快照缺省落点 = Rust 桥 crate 目录（构建期 include_str! 的输入）。

    crate 与 RimCrow 仓库解耦，两仓相邻但可能隔一层 worktree
    （``dev/RimCrow`` 或 ``dev/RimCrow/.worktrees/<name>``），按两种布局依次探测；
    ``RIMCROW_MCP_CRATE_DIR`` 可显式指定，``--out`` 优先级最高。
    """
    override = os.environ.get("RIMCROW_MCP_CRATE_DIR", "").strip()
    if override:
        return Path(override) / "tools-cache.json"
    bases = [REPO_ROOT.parent]
    if len(REPO_ROOT.parents) > 2:
        bases.append(REPO_ROOT.parents[2])
    for base in bases:
        candidate = base / "rimcrow-mcp"
        if candidate.is_dir():
            return candidate / "tools-cache.json"
    return REPO_ROOT.parent / "rimcrow-mcp" / "tools-cache.json"


DEFAULT_OUT = _default_out()

# tools[] 条目字段（格式约定 v1，增删字段必须同步桥侧 serde 结构）。
TOOL_FIELDS = ("name", "description", "inputSchema", "annotations")


def serialize_tool(tool: Any) -> dict[str, Any]:
    """单个 FastMCP Tool → 快照条目（协议线上形态，camelCase 按别名序列化）。

    ``to_mcp_tool()`` 是 FastMCP 自己的转换器（= 客户端 tools/list 实际看到的
    形态），走它就不会与运行时走样；这里只裁出约定的四个字段。
    """
    wire = tool.to_mcp_tool().model_dump(by_alias=True, exclude_none=True)
    return {field: wire.get(field) for field in TOOL_FIELDS}


def build_payload() -> dict[str, Any]:
    """构造 tools-cache.json 载荷（唯一真相源 → FastMCP 注册结果 → 快照）。

    lazy import：本模块导入期零副作用（不碰 backend.utils.logger / cwd / DB），
    只有真正执行生成时才装配工具层。
    """
    from backend._version import __version__
    from backend.mcp_server import ManifestOnlyApi, create_mcp_app
    from backend.mcp_stdio import isolate_stdout
    from backend.mcp_tool_specs import SERVER_INSTRUCTIONS
    from backend.utils.logger import logger

    # stdout 约定（本脚本：stdout 仅一行摘要）：create_mcp_app 会经 RimCrow
    # logger 打 "app ready"，控制台 handler 默认写 stdout，先认领到 stderr。
    isolate_stdout(logger)
    mcp = create_mcp_app(ManifestOnlyApi())
    tools = asyncio.run(mcp.list_tools())
    return {
        "schema_version": SCHEMA_VERSION,
        "rimcrow_version": __version__,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "server_name": SERVER_NAME,
        "instructions": SERVER_INSTRUCTIONS,
        "tools": [serialize_tool(tool) for tool in tools],
    }


def write_cache(payload: dict[str, Any], out_path: Path) -> None:
    """原子写出（tmp + os.replace，参考 backend.mcp_endpoint.write_endpoint）。

    UTF-8 无 BOM、LF 换行、ensure_ascii=False、indent=2。
    """
    target = out_path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/gen_tool_cache.py",
        description="Export tools-cache.json from backend.mcp_tool_specs (bridge build input)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output path (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)

    payload = build_payload()
    write_cache(payload, args.out)

    target = args.out.expanduser()
    print(
        f"tools-cache: {target} | tools={len(payload['tools'])} "
        f"| rimcrow_version={payload['rimcrow_version']}"
    )
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001 - 构建期脚本：原因全量落 stderr，退出码非 0
        traceback.print_exc(file=sys.stderr)
        code = 1
    raise SystemExit(code)
