# backend/mcp_server.py
"""FastMCP 工具层：全量管理工具 + 语义护栏 + 分页。

三种宿主复用同一个 app 工厂 ``create_mcp_app()``：

- **stdio 开发/测试入口**（``python -m backend.mcp_server``，``scripts/smoke_mcp.py``）；
- **mcp-embed**：GUI 进程内 Streamable HTTP 端点托管（生产数据面）；
- （生产 stdio 入口 ``rimcrow-mcp.exe`` 为 Rust 薄封装，只消费构建期快照，
  不 import 本模块——attach-only。）

设计要点：

- **绑定机制**：``backend.mcp_tool_specs`` 的函数签名即 inputSchema、docstring 即
  description；这里用 ``functools.wraps`` 把真实处理器披在同一签名上注册，
  stdio 测试载体与 embed 的 tools/list 因此由同一份定义派生（唯一来源，测试锁定）。
- **双层护栏**：语义层 ``guard_payload`` 包住每个返回值（元素边界二分截断、
  合法 JSON、翻页 hint，cap = 保险层 × 0.75）+ 保险层 ``ResponseLimitingMiddleware``
  按整帧字节上限截断；两层**分档**而非同值——整帧含 JSON-RPC 包装与转义开销，语义层 cap
  必须留出这部分余量，产物才能穿过整帧 JSON-RPC 转义。
- **串行化**：除 wait_event 外的所有工具调用在全局锁内执行——
  MCP 侧自并发由本层串行保证（读-校验-写原子性、makedirs 竞争消解）；MCP×GUI
  跨面竞争沿用既有防线（version token + 原子落盘），不在工具层加锁。
- **stdout 纪律**：import 期吞 stdout、logger handler 改指 stderr。
- **职责拆分（单向依赖，防循环）**：handler 按职责外置到四个同级模块——
  ``backend.mcp_adapters_query``（单步只读 → 过滤/投影/分页字段）、
  ``backend.mcp_handlers_features``（依赖图 / 加载顺序增量写 / 用户规则写）、
  ``backend.mcp_handlers_texture``（贴图转换）、
  ``backend.mcp_errors``（叶子：``_tool_error`` 与通用 nextActions）。本模块只留
  绑定与调用管线、锁与扫描门控、上游载荷 → 统一错误结构转换、事件桥、``_HANDLER_OVERRIDES``
  注册表、``create_mcp_app`` 与 stdio 入口；**新模块一律不 import 本模块**。
"""

from __future__ import annotations

import argparse
import functools
import json
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from backend.mcp_adapters_query import (
    _handle_get_game_status,
    _handle_get_groups,
    _handle_get_load_order,
    _handle_get_settings_overview,
    _handle_list_log_files,
    _handle_list_mods,
    _handle_list_profiles,
    _handle_parse_load_order,
    _handle_read_logs,
    _newest_log_name,
)
from backend.mcp_errors import _INTERNAL_ACTIONS, _PARAM_ACTIONS, _tool_error
from backend.mcp_guard import guard_payload, max_output
from backend.mcp_handlers_features import (
    _handle_apply_load_order_ops,
    _handle_cancel_task,
    _handle_export_load_order,
    _handle_get_mod_dependencies,
    _handle_get_workshop_mod_details,
    _handle_github_subscribe_repo,
    _handle_launch_game,
    _handle_read_mod_file,
    _handle_resolve_missing_dependencies,
    _handle_set_mod_rule,
    _handle_smart_insert_mods,
)
from backend.mcp_handlers_texture import (
    _handle_cancel_texture_convert,
    _handle_get_texture_convert_tasks,
    _handle_start_texture_convert,
)
from backend.mcp_stdio import (
    anchor_project_root,
    ensure_protocol_stdio,
    isolate_stdout,
    quiet_import,
    stdio_entry,
)
from backend.mcp_tool_specs import (
    SERVER_INSTRUCTIONS,
    TOOL_SPECS,
    ToolSpec,
    annotations_for,
)

# 必须在 import backend.settings 之前锚定工作目录（DATA_DIR 在 import 期绑定）。
anchor_project_root()

# --- MCP stdio 协议要求 stdout 只承载 JSON-RPC（fork 原样保留段）--------------
# import 期间先把 stdout 换成缓冲吞掉日志，随后把所有控制台 handler 重定向到 stderr。
with quiet_import():
    from backend.utils.logger import logger

    # 通用错误结构常量：error_contract 连带 import backend.i18n → backend.settings
    # （DATA_DIR 在 import 期绑定），必须在 anchor_project_root() 之后、stdout 隔离
    # 窗口内导入；下游直接导入这些常量，不复制 api.py 的文案/错误码（见常量处注释）。
    from backend.utils.error_contract import (
        UNHANDLED_EXCEPTION_CODE,
        UNHANDLED_EXCEPTION_MARKER,
    )

isolate_stdout(logger)

__all__ = [
    "ManifestOnlyApi",  # 构建期约定（清单/快照占位 api），非运行时 API
    "create_mcp_app",
    "install_event_tap",
    "restore_event_tap",
    "run_stdio",
    "validate_bindings",
]

# 服务器起始时刻：「初始化扫描中（已 N 秒）」的计时零点（≈ API 就绪）。
_SERVER_STARTED_AT = time.monotonic()

# 工具层统一串行化（wait_event / search_mod_text 是长等待/事件
# 轮询，不碰共享写状态，天然豁免）；start_texture_convert 的 dry_run 是同步扫描
# 可长等待、正式启动仅派发后台线程，任务态由 texture_mgr 自锁。
_CALL_LOCK = threading.RLock()
_NO_LOCK_TOOLS = frozenset({"wait_event", "search_mod_text", "start_texture_convert"})

# 依赖扫描结果的查询工具——首扫进行中且库里还没有数据时给因果错误而非半成品。
_SCAN_GATED_TOOLS = frozenset(
    {
        "get_initial_data",
        "list_mods",
        "get_load_order",
        "get_groups",
        "get_mod_dependencies",
        "search_mods",
        "get_active_mod_list",
        "get_mod_info",
        "get_mod_rules",
        "get_mod_user_data",
        "get_group_mods",
        # 智能插位与缺依赖解析都吃 ModDAO 扫描数据
        "smart_insert_mods",
        "resolve_missing_dependencies",
    }
)


# ---------------------------------------------------------------------------
# 事件桥：wait_event 的数据源（embed/stdio 两态同一实现）
# ---------------------------------------------------------------------------

_EVENT_QUEUE: queue.Queue = queue.Queue(maxsize=500)
# fan-out：活跃等待者各持一条私有缓冲，tap 把事件复制分发给所有等待者。
# wait_event / search_mod_text 同属 _NO_LOCK_TOOLS（无全局调用锁）会真并发，
# 共享单队列「取一个丢一个」会让两者互相取走对方的事件（搜索取走
# scan-complete、wait_event 取走 file-search-results 的 done）。
_TAP_LOCK = threading.Lock()
_ORIGINAL_EMIT_DESCRIPTOR: Any = None  # EventBus.__dict__["emit"]（classmethod 描述符）
_WAITERS_LOCK = threading.Lock()  # 只护下面列表的增删/快照，不与队列操作嵌套
_SUBSCRIBERS: list[queue.Queue] = []  # 活跃等待者缓冲，增删均在 _WAITERS_LOCK 内
_SUBSCRIBER_MAXSIZE = 500  # 与 _EVENT_QUEUE 同上限：满载丢弃，语义与 fork 一致


def install_event_tap() -> bool:
    """把 EventBus.emit 换成「记录 + 原样转发」的 tap（幂等）。

    为什么不直接用 ``set_browser_dispatcher``：desktop 模式下 dispatcher 会**顶替**
    窗口派发（``_dispatch_to_target`` 先判 dispatcher），且 ``set_window`` 会把
    dispatcher 清空——两条路都会破坏 GUI 事件。包装 emit 则完整保留既有行为，
    只额外复制一份给 MCP 等待者（零改动 backend/utils）。
    """
    global _ORIGINAL_EMIT_DESCRIPTOR
    with _TAP_LOCK:
        if _ORIGINAL_EMIT_DESCRIPTOR is not None:
            return False
        from backend.utils.event_bus import EventBus

        descriptor = EventBus.__dict__.get("emit")
        if descriptor is None:
            logger.warning("MCP: EventBus.emit 形态变化，wait_event 事件桥未安装")
            return False
        _ORIGINAL_EMIT_DESCRIPTOR = descriptor

        def tap(event_name: str, data: Any = None) -> None:
            item = {"event": event_name, "data": data}
            try:
                _EVENT_QUEUE.put_nowait(item)
            except queue.Full:
                pass  # 队列满丢弃旧事件，与 fork 语义一致
            # fan-out：复制分发给全部活跃等待者（同一 item 只读不改，共享安全）。
            # 先取快照再放锁，避免持锁做 put。
            with _WAITERS_LOCK:
                subscribers = tuple(_SUBSCRIBERS)
            for sub in subscribers:
                try:
                    sub.put_nowait(item)
                except queue.Full:
                    pass  # 单缓冲满同样丢弃，与共享队列的满载语义一致
            original(event_name, data)

        # classmethod 描述符按 (None, owner) 取绑定（cls=EventBus）；若已被改成
        # 普通函数则原样取函数本身——两条形态都成立。
        original = descriptor.__get__(None, EventBus)

        EventBus.emit = tap  # type: ignore[method-assign]
        logger.debug("MCP: EventBus 事件桥已安装")
        return True


def restore_event_tap() -> None:
    """还原 EventBus.emit（退出清理用，幂等）。"""
    global _ORIGINAL_EMIT_DESCRIPTOR
    with _TAP_LOCK:
        if _ORIGINAL_EMIT_DESCRIPTOR is None:
            return
        from backend.utils.event_bus import EventBus

        EventBus.emit = _ORIGINAL_EMIT_DESCRIPTOR  # type: ignore[method-assign]
        _ORIGINAL_EMIT_DESCRIPTOR = None


def _subscribe_events() -> queue.Queue:
    """注册一个活跃等待者的私有缓冲（fan-out 接收端），返回该队列。

    缓冲在**等待开始时刻**创建且为空——陈旧事件只可能躺在共享队列 ``_EVENT_QUEUE``
    里，进不了新缓冲，因此每个等待者在等待开始后都拿不到积压事件。
    """
    sub: queue.Queue = queue.Queue(maxsize=_SUBSCRIBER_MAXSIZE)
    with _WAITERS_LOCK:
        _SUBSCRIBERS.append(sub)
    return sub


def _unsubscribe_events(sub: queue.Queue) -> None:
    """注销等待者缓冲（handler 必须在 finally 里调用，否则 tap 会一直向死缓冲分发）。"""
    with _WAITERS_LOCK:
        try:
            _SUBSCRIBERS.remove(sub)
        except ValueError:
            pass  # 已注销过，幂等


# ---------------------------------------------------------------------------
# 通用处理器：委托 backend.api.API，不改业务代码
# ---------------------------------------------------------------------------


def _resolve_api_method(api: Any, method: str) -> Callable[..., Any]:
    target = getattr(api, method, None)
    if target is None or not callable(target):
        raise _tool_error(
            f"API 方法缺失：{method}",
            ["确认 RimCrow 版本与工具层匹配", "用 get_initial_data 验证后端状态"],
        )
    return target


# ---------------------------------------------------------------------------
# 既有内置 AI 工具的桥接（只读复用，零改 backend/ai）
# ---------------------------------------------------------------------------


def _make_ai_handler(ai_name: str, *, with_log_source: bool = False) -> Callable[..., Any]:
    """构造 AIToolExecutor 桥接处理器。

    接口约定在上游内部（构造参数形状随上游变）→ 全部收敛在本适配层；
    ``log_type``/``filename`` 是 MCP 侧的日志源选择参数，进入 arguments 之前
    必须弹出（AI 参数模型 extra="forbid"），改走 payload 上下文注入。
    """

    def handler(api: Any, *args: Any, **kwargs: Any) -> Any:
        log_type = str(kwargs.pop("log_type", "game") or "game") if with_log_source else ""
        filename = str(kwargs.pop("filename", "") or "") if with_log_source else ""
        if ai_name == "get_mod_info" and kwargs.get("fields") is None:
            kwargs["fields"] = []
        payload: dict[str, Any] = {}
        reader = None
        if with_log_source:
            if not filename:
                filename = _newest_log_name(api, log_type)
            if filename:
                payload = {"log_source_type": log_type, "filename": filename}
            if log_type == "game":
                reader = api.game_log_mgr
            else:
                # app 日志读取器是 backend.utils.logger 的**模块级单例**（backend.api 也在
                # 模块级导入它），API 实例上没有这个属性——getattr(api, "app_log_reader") 恒为
                # None，会让 get_log_context {log_type:"app"} 必然报「不支持按行反查」。
                from backend.utils.logger import app_log_reader

                reader = app_log_reader
        from backend.ai.ai_tools import AIToolExecutor

        executor = AIToolExecutor(getattr(api, "active_context", None), payload, reader)
        result = executor.execute_structured(ai_name, json.dumps(kwargs, ensure_ascii=False))
        data = result.data
        failed = bool(result.error) or (isinstance(data, dict) and bool(data.get("error")))
        message = result.error or (data.get("error") if isinstance(data, dict) else "") or result.summary
        return {"status": "error" if failed else "success", "message": message, "data": data}

    return handler


def _drain_stale_events() -> int:
    """清掉等待开始前已入队的事件，返回丢弃条数。

    队列是长存单例（embed 在 API 就绪即装 tap），历史扫描结果会一直积压，不清会
    涨满到 maxsize——这里清的是**共享队列** ``_EVENT_QUEUE``。fan-out 之后真正的
    等待者读的是自己的私有缓冲（``_subscribe_events`` 创建即空，天然只含等待期间
    新事件），所以本函数的清理绝不会波及其它活跃等待者。事件无时间
    戳，新旧只能以「等待起点」为界判定。
    """
    dropped = 0
    while True:
        try:
            _EVENT_QUEUE.get_nowait()
        except queue.Empty:
            return dropped
        dropped += 1


def _handle_wait_event(api: Any, **kwargs: Any) -> Any:
    """wait_event：只命中**等待期间新到达**的事件 + EventBus 轮询语义。

    fan-out：起等即订阅私有缓冲（创建即空 ⇒ 陈旧事件进不来），随后清理共享队列
    仅为存量卫生；``timeout<=0`` 直接走既有超时路径（不订阅、不消费任何事件）。
    """
    event_name = str(kwargs.get("event_name") or "")
    timeout = float(kwargs.get("timeout") if kwargs.get("timeout") is not None else 60.0)
    install_event_tap()
    if timeout <= 0:
        # 零/负超时＝即刻超时的空轮询：不订阅也不清理，队列原封未动
        return {"status": "timeout", "data": {"event": event_name}}
    deadline = time.monotonic() + timeout
    sub = _subscribe_events()
    try:
        stale = _drain_stale_events()
        if stale:
            logger.debug("MCP wait_event: dropped %d stale queued event(s)", stale)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = sub.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if item["event"] == event_name:
                return {"status": "success", "data": item}
    finally:
        _unsubscribe_events(sub)
    return {"status": "timeout", "data": {"event": event_name}}


# ---------------------------------------------------------------------------
# search_mod_text（事件依赖 handler 与 wait_event
# 同址——handler 子模块禁止 import 本模块，事件桥全局量在这里）
# ---------------------------------------------------------------------------

# max_results / timeout 的服务端硬上限：schema 不设上限的参数在此收口，
# 超限直接钳制而不是报错（agent 侧无法超出护栏拿更多数据）。
_SEARCH_MAX_RESULTS_CAP = 200
_SEARCH_TIMEOUT_CAP = 300.0


def _handle_search_mod_text(api: Any, **kwargs: Any) -> Any:
    """search_mod_text：file-search 任务 + file-search-results 事件聚合。

    终态沿用 mgr_text_search 的 ``status`` 值域（success/cancelled/failed）；
    ``failed`` 转因果错误，``cancelled``/超时如实返回已收到的部分结果。
    并发：与 wait_event 各持 fan-out 私有缓冲，互不干扰（聚合、截断、
    failed→因果错误的行为均不变）。
    """
    query = str(kwargs.get("query") or "").strip()
    if not query:
        raise _tool_error("query 不能为空", _PARAM_ACTIONS)
    scope = str(kwargs.get("scope") or "current-active")
    try:
        max_results = max(1, min(int(kwargs.get("max_results") or 50), _SEARCH_MAX_RESULTS_CAP))
        timeout = max(1.0, min(float(kwargs.get("timeout_seconds") or 120.0), _SEARCH_TIMEOUT_CAP))
    except (TypeError, ValueError) as exc:
        raise _tool_error("max_results / timeout_seconds 必须是数字", _PARAM_ACTIONS) from exc
    mgr = getattr(api, "file_search_mgr", None)
    if mgr is None:
        raise _tool_error(
            "文件搜索管理器未初始化",
            ["确认 RimCrow 版本与工具层匹配", "稍后重试"],
        )

    install_event_tap()  # 幂等；embed 宿主在 API 就绪时已装
    # fan-out：先订阅私有缓冲**再**启动任务（真实任务可能同步 emit 完成事件），
    # 与并发 wait_event 各读各的缓冲，互不干扰。
    sub = _subscribe_events()
    try:
        search_payload: dict[str, Any] = {
            "query": query,
            "scope": scope,
            "use_regex": bool(kwargs.get("use_regex")),
            "case_sensitive": bool(kwargs.get("case_sensitive")),
            "filenames_only": bool(kwargs.get("filenames_only")),
        }
        raw_file_types = kwargs.get("file_types")
        if raw_file_types is not None:
            if isinstance(raw_file_types, (str, bytes)) or not isinstance(raw_file_types, (list, tuple)):
                raise _tool_error(
                    "file_types 必须是字符串数组（如 [\".xml\", \".cs\"]），不限类型传 [\".\"]",
                    _PARAM_ACTIONS,
                )
            cleaned_file_types = [str(item).strip() for item in raw_file_types if str(item).strip()]
            if cleaned_file_types:
                search_payload["file_types"] = cleaned_file_types
        task_id = mgr.start_search(search_payload)

        collected: list[Any] = []
        matched_count = 0
        truncated = False
        final_status = ""
        message = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                item = sub.get(timeout=0.5)
            except queue.Empty:
                continue
            if item.get("event") != "file-search-results":
                continue  # 非本工具的事件丢弃（只丢自己缓冲里的副本，不影响他人）
            data = item.get("data") or {}
            if str(data.get("task_id") or "") != task_id:
                continue
            matched_count = int(data.get("matched_count") or matched_count)
            message = str(data.get("message") or message)
            for result in data.get("results") or []:
                if len(collected) < max_results:
                    collected.append(result)
                else:
                    truncated = True
            if data.get("done"):
                final_status = str(data.get("status") or "success")
                break

        if final_status == "failed":
            raise _tool_error(
                message or "文件搜索任务失败",
                ["确认 scope 取值与当前档案路径可用", "查看 list_log_files → read_logs 中的后端错误"],
            )
        payload: dict[str, Any] = {
            "status": "success" if final_status in ("success", "cancelled") else "timeout",
            "query": query,
            "scope": scope,
            "filenames_only": search_payload["filenames_only"],
            "task_id": task_id,
            "matched_count": matched_count,
            "returned": len(collected),
            "truncated": truncated or matched_count > len(collected),
            "results": collected,
        }
        if search_payload.get("file_types"):
            payload["file_types"] = search_payload["file_types"]
        if final_status == "cancelled":
            payload["message"] = message or "搜索任务被新任务替换而取消，返回已收到的部分结果"
        elif not final_status:
            payload["message"] = (
                f"等待搜索完成超时（{timeout:.0f} 秒），返回已收到的部分结果；"
                "可加大 timeout_seconds 重试"
            )
        elif message:
            payload["message"] = message
        return payload
    finally:
        _unsubscribe_events(sub)


# 处理器覆盖表：不在表内且有 method 的工具走默认委托。
_HANDLER_OVERRIDES: dict[str, Callable[..., Any]] = {
    "list_mods": _handle_list_mods,
    "get_load_order": _handle_get_load_order,
    "list_profiles": _handle_list_profiles,
    "get_groups": _handle_get_groups,
    "get_settings_overview": _handle_get_settings_overview,
    "read_logs": _handle_read_logs,
    "wait_event": _handle_wait_event,
    # AI 工具桥
    "get_log_context": _make_ai_handler("get_log_context", with_log_source=True),
    "search_mods": _make_ai_handler("search_mods"),
    "get_active_mod_list": _make_ai_handler("get_active_mod_list"),
    "get_mod_info": _make_ai_handler("get_mod_info"),
    "get_mod_rules": _make_ai_handler("get_mod_rules"),
    "get_mod_user_data": _make_ai_handler("get_mod_user_data"),
    "get_group_mods": _make_ai_handler("get_group_mods"),
    # 依赖图 / 加载顺序 / 用户规则
    "get_mod_dependencies": _handle_get_mod_dependencies,
    "list_log_files": _handle_list_log_files,
    "apply_load_order_ops": _handle_apply_load_order_ops,
    "set_mod_rule": _handle_set_mod_rule,
    # 状态 / 排障 / 整理 / 获取
    "get_game_status": _handle_get_game_status,
    "launch_game": _handle_launch_game,
    "search_mod_text": _handle_search_mod_text,
    "parse_load_order": _handle_parse_load_order,
    "export_load_order": _handle_export_load_order,
    "smart_insert_mods": _handle_smart_insert_mods,
    "resolve_missing_dependencies": _handle_resolve_missing_dependencies,
    # 补全批（MCP 缺失能力）：任务控制 / 文件读取 / 工坊详情
    "cancel_task": _handle_cancel_task,
    "read_mod_file": _handle_read_mod_file,
    "get_workshop_mod_details": _handle_get_workshop_mod_details,
    # github_subscribe_repo：上游 github_subscribe 要求 payload 自带仓库在线信息，
    # 由 handler 先取信息再组装，不走直接委托
    "github_subscribe_repo": _handle_github_subscribe_repo,
    # 贴图转换
    "start_texture_convert": _handle_start_texture_convert,
    "get_texture_convert_tasks": _handle_get_texture_convert_tasks,
    "cancel_texture_convert": _handle_cancel_texture_convert,
}


def validate_bindings(api: Any = None) -> None:
    """绑定自检：注册前强制校验「一个工具只有一条绑定路径」。

    三条不变量：

    1. **无孤儿**：``_HANDLER_OVERRIDES`` 的键必须都在 ``TOOL_SPECS`` 里——
       孤儿 handler 注册不到、也不会被任何路径调用，静默失效；
    2. **无双注册**：既在覆盖表里、``spec.method`` 又非 None（即同时登记在
       ``mcp_tool_specs._API_METHODS``）的工具必须为零。两张表双写同一个工具时，
       漏改其一没有任何测试会报错（适配工具不登记在 ``_API_METHODS``，
       mapping 只写在 handler 体内）；
    3. **委托可达**：走默认直接委托的工具，其 API 方法必须在 ``api`` 上可解析——
       缺方法在建 app 时就炸，不留到首次调用（``_resolve_api_method`` 是运行期降级路径）。

    判据说明：第 2 条用 ``spec.method`` 而非直摸 ``_API_METHODS``——
    ``spec.method`` 恒由 ``_API_METHODS.get(name)`` 派生，两者等价。
    ``ManifestOnlyApi``（``--dry-run`` / 构建期快照）对**任意**属性都 raise
    （见其 ``__getattr__``），第 3 条跳过；``api is None`` 同样跳过（未注入后端）。
    """
    names = {spec.name for spec in TOOL_SPECS}
    orphans = sorted(set(_HANDLER_OVERRIDES) - names)
    if orphans:
        raise RuntimeError(f"orphan handler override(s), not in TOOL_SPECS: {orphans}")
    dual = sorted(
        spec.name
        for spec in TOOL_SPECS
        if spec.method is not None and spec.name in _HANDLER_OVERRIDES
    )
    if dual:
        raise RuntimeError(
            f"tool(s) registered both as handler override and API method: {dual}"
        )
    if api is None or isinstance(api, ManifestOnlyApi):
        return
    for spec in TOOL_SPECS:
        if spec.method is None:
            continue
        if not callable(getattr(api, spec.method, None)):
            raise RuntimeError(
                f"tool {spec.name!r} delegates to API method {spec.method!r} "
                "which is missing or not callable on api"
            )


# ---------------------------------------------------------------------------
# 初始化期语义
# ---------------------------------------------------------------------------


def _scan_pending_reason(api: Any) -> str | None:
    """首扫进行中且库里尚无数据 → 因果错误文案；否则 None（正常服务）。"""
    scanner = getattr(api, "scanner", None)
    if scanner is None or not getattr(scanner, "is_scanning", False):
        return None
    try:
        from backend.database.dao import ModDAO

        visible = ModDAO.get_profile_mods(getattr(api, "active_context", None))
        if visible:
            return None  # 二次扫描：数据在，照常服务
    except Exception:  # noqa: BLE001 - 判定失败不挡服务
        return None
    elapsed = int(time.monotonic() - _SERVER_STARTED_AT)
    return f"RimCrow 初始化扫描中（已 {elapsed} 秒），稍后重试"


# ---------------------------------------------------------------------------
# 绑定与调用管线
# ---------------------------------------------------------------------------


def _bind(spec: ToolSpec, handler: Callable[..., Any], api: Any) -> Callable[..., str]:
    """把处理器披到 spec 函数的签名/文档上（schema 由此派生的机制）。

    ``api`` 绑进闭包——同一进程里建多个 app（单测常见）时互不串档。
    """

    @functools.wraps(spec.func)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        return _invoke(spec, handler, api, args, kwargs)

    # functools.wraps 理论上会复制 __annotations__；仍显式再赋值一层，防止未来
    # Python/框架改用 __annotations__ 直读导致参数全退化成 string。
    wrapper.__annotations__ = dict(getattr(spec.func, "__annotations__", {}))
    return wrapper


# backend/api.py 的 ``log_api_call`` 把**所有**异常统一转成只含通用文案的响应
# ``ApiResponse.error("API 调用发生未处理异常", code="API.CALL.UNHANDLED_EXCEPTION", ...)``，
# 真实原因只留在 ``payload["detail"]["traceback"]``；``except ValueError`` 分支不传 code，
# 由 error_contract 分类为 SYSTEM.INVALID_INPUT，业务校验错误不进本文件，下面的 traceback
# 末行提取只服务真正未处理异常；判据取 error_contract 常量（导入，勿复制 api.py 字面量）。
_GENERIC_SHELL_CODE = UNHANDLED_EXCEPTION_CODE
_GENERIC_SHELL_TEXT = UNHANDLED_EXCEPTION_MARKER

# 错误原因单行上限：异常消息可能内嵌用户数据（模组名/分组名/路径），且 traceback 其余
# 部分（堆栈路径、源码片段）绝不能外泄给 agent——只取单行 + 截断，不整包回传。
_CAUSE_MAX_CHARS = 300

# ``SomeError: 消息``（traceback.format_exception 的末行形态）。帧源码行带缩进、
# ``File "..."`` 行与 ``The above exception was the direct cause...`` 连接语均不匹配。
_EXCEPTION_LINE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*:"
)

# 业务校验类错误的特征词 → nextActions 指向「先查现状再重试」而非「核对 schema」
#（schema 根本没错，让 agent 去核对它是误导）。
_BUSINESS_CAUSE_MARKERS = (
    "不存在",
    "未找到",
    "已存在",
    "不能为空",
    "无效",
    "不支持",
    "重复",
    # --- 以下覆盖上游直传真因果的主路径 ---
    "不一致",   # 「分组排序失败：提交的分组数量与当前数据不一致。」
    "为空",     # 「分组内排序失败：提交的成员列表为空。」
    "未提供",   # 「更新分组失败：未提供有效字段。」（注意「无效」不覆盖它：非连续子串）
    "失败：",   # DAO 层 ValueError 的统一格式「{动作}失败：{原因}」，作为业务校验的备选特征词
)


def _upstream_cause_from_traceback(payload: dict[str, Any]) -> str | None:
    """从上游通用错误响应的 ``detail.traceback`` 提取**单行**真实错误原因。

    只认**最后一行非空行**且必须形如 ``SomeError: 消息``；提取不到（无 traceback /
    不是异常行 / 格式对不上）→ 返回 None，调用方保持通用文案。防注入边界：永远只
    回传一行，且截断到 ``_CAUSE_MAX_CHARS`` 字符。
    """
    detail = payload.get("detail")
    if not isinstance(detail, dict):
        return None
    raw = detail.get("traceback")
    if not isinstance(raw, str) or not raw.strip():
        return None
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if not _EXCEPTION_LINE_RE.match(last):
        return None  # 末行不是异常行（多行消息续行/截断产物）→ 不猜、不外传
    return last.strip()[:_CAUSE_MAX_CHARS]


def _raise_upstream_failure(spec: ToolSpec, payload: Any) -> None:
    """上游 ApiResponse 的 error/warning 响应 → 统一错误结构。

    通用委托（``_API_METHODS``）原样返回上游响应：error/warning 若直接透传，agent 只
    拿到无 ``isError`` 的内容（大载荷还会被语义护栏截成不可读的壳），违反「错误即
    自纠指令」。统一转 ToolError：message
    保留因果、nextActions 指引自纠——上游 warning 常携带的 data（如冲突双方列表）由
    按需重读取回，不整包回传。success 及 timeout 等业务状态原样放行。

    错误原因有两条来源——①上游**直传真因果**（api.py 装饰器对 ValueError 保留
    message 后的主路径）；②命中**通用错误响应**（``API.CALL.UNHANDLED_EXCEPTION`` /
    文案含「未处理异常」）时从 ``detail.traceback`` 末行提取（真正的未处理异常取因）。
    nextActions 按**最终 message** 的业务特征分流，两条来源都给「先查现状再重试」，
    避免让 agent 去核对 schema（schema 本来没错）。版本 token 分支保持优先；两者皆无
    则沿用通用文案。
    """
    if not isinstance(payload, dict) or payload.get("status") not in ("error", "warning"):
        return
    message = (
        str(payload.get("message") or payload.get("user_message") or "").strip()
        or "操作未成功"
    )
    error_code = str(payload.get("error_code") or "").strip()
    if "version_token" in message or "磁盘加载顺序" in message:
        # 优先级最高：冲突/token 类文案即使带 traceback 也不改写（既有自纠链路）。
        actions = [
            "重新调用 get_load_order 获取最新 version_token",
            "确认没有其它实例（RimCrow 界面/游戏）在改 ModsConfig.xml",
            "用新 token 重试本次操作",
        ]
    else:
        cause = (
            _upstream_cause_from_traceback(payload)
            if (error_code == _GENERIC_SHELL_CODE or _GENERIC_SHELL_TEXT in message)
            else None
        )
        if cause:
            message = f"{cause}（上游 {error_code or _GENERIC_SHELL_CODE}）"
        # 业务特征分流对**最终 message**判断，而不是只对 traceback 提取结果判断——
        # 两条原因来源都要覆盖：
        #   1) 上游直传真因果（api.py 装饰器对 ValueError 保留 message 后的主路径）；
        #   2) 通用错误响应文案 → traceback 末行提取（真正的未处理异常取因）。
        # 只按 cause 分流会让主路径落回「核对 schema」，而 schema 本来没错。
        if any(marker in message for marker in _BUSINESS_CAUSE_MARKERS):
            actions = [
                "上游业务校验未通过：先用只读工具确认目标现状"
                "（分组 get_groups / get_group_mods，模组 list_mods，加载顺序 get_load_order）",
                "按确认到的现状修正参数后重试；目标确实不存在就换用正确的 id",
            ]
        elif cause:
            actions = [
                "查看 list_log_files → read_logs 中的后端日志定位完整堆栈",
                "稍后重试；若持续失败，把本条错误原文反馈给维护者",
            ]
        else:
            actions = [
                "重新读取最新状态后重试（get_load_order / list_mods / list_backups 等按需）",
                "查看 list_log_files → read_logs 定位服务端错误详情",
                f"参数类失败：核对 {spec.name} 的 schema 与参数取值后重试",
            ]
    raise _tool_error(message, actions)


def _invoke(
    spec: ToolSpec,
    handler: Callable[..., Any],
    api: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """工具调用管线：就绪判定 → 串行锁 → 处理器 → 统一错误结构转换 → 语义护栏 → JSON 字符串。"""
    if spec.name in _SCAN_GATED_TOOLS:
        reason = _scan_pending_reason(api)
        if reason:
            raise _tool_error(
                reason,
                ["等待扫描完成后再查询（wait_event(\"scan-complete\")）", "或稍后轮询 list_mods"],
            )
    lockless = spec.name in _NO_LOCK_TOOLS
    started = time.perf_counter()
    try:
        if lockless:
            payload = handler(api, *args, **kwargs)
        else:
            with _CALL_LOCK:
                payload = handler(api, *args, **kwargs)
        _raise_upstream_failure(spec, payload)
    except Exception as exc:  # noqa: BLE001 - 统一错误结构出口
        from fastmcp.exceptions import ToolError

        if isinstance(exc, ToolError):
            raise
        if isinstance(exc, ValueError):
            raise _tool_error(str(exc), _PARAM_ACTIONS) from exc
        logger.error(
            "MCP tool failed: %s", spec.name, exc_info=True,
            extra={"error_code": "MCP.TOOL.FAILED", "extra_context": {"tool": spec.name}},
        )
        raise _tool_error(
            f"工具执行异常（{type(exc).__name__}），详情见服务端日志",
            _INTERNAL_ACTIONS,
        ) from exc
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > 500:
            logger.debug("MCP slow tool: %s elapsed_ms=%.1f", spec.name, elapsed_ms)
    return guard_payload(payload)


def _create_dev_api() -> Any:
    """stdio 开发入口的真实 API（browser 运行态，与 fork 同款）。"""
    with quiet_import():
        from backend.api import API

    return API(runtime_mode="browser")


def create_mcp_app(api: Any = None, *, with_event_bridge: bool = False) -> Any:
    """app 工厂：装配 TOOL_SPECS 全部工具 + 双层护栏 + instructions（三宿主同源）。

    Args:
        api: ``backend.api.API`` 实例；None 时为 stdio 开发入口构造一个。
        with_event_bridge: True（mcp-embed）表示在 API 就绪即安装 EventBus 事件桥，
            让 wait_event 能捕获从现在起的全部事件。
    """
    from fastmcp import FastMCP
    from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware

    if api is None:
        api = _create_dev_api()

    # 注册前强制校验绑定不变量（孤儿 handler / 双注册 / 直接委托方法缺失）
    validate_bindings(api)

    from backend._version import __version__

    mcp = FastMCP(
        "rimcrow-mcp",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        mask_error_details=False,  # 因果文案必须抵达 agent
    )

    for spec in TOOL_SPECS:
        handler = _HANDLER_OVERRIDES.get(spec.name)
        if handler is None:
            if spec.method is None:
                raise RuntimeError(f"tool {spec.name!r} has neither handler nor API method")
            # 默认委托：方法名在调用期才解析（api 换档/热替换不受 import 期约束）
            handler = functools.partial(_delegate_method, spec.method)
        mcp.tool(
            name=spec.name,
            description=spec.desc,
            output_schema=None,  # 文本 JSON 单份下发，不进 structuredContent 双写
            annotations=annotations_for(spec),
        )(_bind(spec, handler, api))

    # 保险层：按整帧基准降级。注意与语义层**分档**——语义层 cap =
    # 本层 × 0.75（mcp_guard.semantic_max_output），留出 JSON-RPC 整帧转义开销；
    # 同值会让「语义层放行、保险层切破」。
    # 其截断产物预期非合法 JSON（agent 以文案为准）。
    mcp.add_middleware(ResponseLimitingMiddleware(max_size=max_output()))

    if with_event_bridge:
        install_event_tap()
    logger.info("MCP: app ready with %d tools", len(TOOL_SPECS))
    return mcp


def _delegate_method(method: str, api: Any, *args: Any, **kwargs: Any) -> Any:
    target = _resolve_api_method(api, method)
    return target(*args, **kwargs)


# ---------------------------------------------------------------------------
# stdio 开发/测试入口
# ---------------------------------------------------------------------------


class ManifestOnlyApi:
    """清单/快照构建用占位 api：只列清单不调任何方法（避免为清单初始化后端/DB）。

    **对外公开（构建期约定），但不属于运行时 API**：``scripts/gen_tool_cache.py``
    与 ``tests/`` 按公开名直接引用（名字带下划线前缀，但按公开名使用）；
    运行时路径仍只用 ``backend.api.API`` 实例。对任意属性访问直接 raise——
    ``validate_bindings`` 据此跳过「直接委托方法可达」检查。
    """

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"manifest-only api must not be called: {name}")


def run_stdio(argv: list[str] | None = None, *, dry_run: bool = False) -> int:
    """stdio 形态：先重建协议流 → 起 app → FastMCP stdio serve。

    顺序是硬约束：``ensure_protocol_stdio()`` 必须先于 FastMCP import
    （lazy import 在 create_mcp_app 内完成），否则冻结态拿到 None 流直接崩。
    """
    status = ensure_protocol_stdio()
    if status in ("no-handle", "unsupported"):
        sys.stderr.write(f"[mcp] stdio unavailable (status={status}); nothing to serve\n")
        sys.stderr.flush()
        return 0  # 无客户端可应答：记日志、干净退出（stdio 入口约定：无客户端不算失败）
    sys.stderr.write(f"[mcp] stdio status={status}\n")
    sys.stderr.flush()
    isolate_stdout(logger)  # 重建后的 stderr 是新对象，控制台 handler 重新认领

    if dry_run:
        import anyio

        mcp = create_mcp_app(ManifestOnlyApi())

        async def _print_tools() -> None:
            tools = await mcp.list_tools()
            for tool in tools:
                print(f"- {tool.name}: {(tool.description or '').splitlines()[0]}")
            print(f"\nTotal tools: {len(tools)}")

        anyio.run(_print_tools)
        return 0

    api = _create_dev_api()
    mcp = create_mcp_app(api)
    logger.info("RimCrow MCP server starting (stdio).")
    mcp.run(transport="stdio", show_banner=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backend.mcp_server", description="RimCrow MCP tool layer (stdio dev entry)"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the tool manifest and exit")
    args = parser.parse_args(argv)
    return run_stdio(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(stdio_entry(main)())
