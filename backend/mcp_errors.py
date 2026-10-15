# backend/mcp_errors.py
"""工具层错误构造（叶子模块）：因果文案 + nextActions 的 ToolError 工厂。

查询适配器与三个功能 handler 模块都要在
handler 体内抛因果错误，但**不得** import ``mcp_server``（会成环）；本模块只依赖
``backend.mcp_guard.build_error_text`` 与函数体内 lazy import 的 fastmcp，位于
依赖图最底层——``mcp_server`` 与三个 handler 模块都单向 import 这里，无环。

边界：上游 error/warning 响应 → 统一错误结构的转换（``_raise_upstream_failure``、traceback 错误原因提取、
业务特征分流）仍留在 ``mcp_server`` 与 ``_invoke`` 管线同址；本模块只收
「构造单个 ToolError」这一件事。
"""

from __future__ import annotations

from typing import Any

from backend.mcp_guard import build_error_text

__all__ = ["_INTERNAL_ACTIONS", "_PARAM_ACTIONS", "_tool_error"]

# 参数类失败的通用 nextActions（跨 handler 共用，改文案波及全部工具错误）。
_PARAM_ACTIONS = ["核对参数取值与工具 desc 中的约束后重试"]
# 服务端内部异常的通用 nextActions（与 _invoke 的降级分支同源）。
_INTERNAL_ACTIONS = [
    "稍后重试（服务端日志含完整堆栈）",
    "若持续失败，用 list_log_files + read_logs 查看后端日志定位",
]


def _tool_error(
    message: str,
    next_actions: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Exception:
    """构造带因果文案 + nextActions 的 ToolError（产物为单个合法 JSON 字符串）。

    FastMCP 把 ToolError 映射为 ``isError:true`` 且保留原文（mask 分支不包裹
    FastMCPError，源码实证），因此错误必须自带自纠指令。
    """
    from fastmcp.exceptions import ToolError

    return ToolError(build_error_text(message, next_actions, extra))
