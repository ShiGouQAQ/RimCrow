# backend/mcp_adapters_query.py
"""查询适配器 handler：单步只读 → 过滤 / 投影 / 分页字段。

职责：结果整形，零业务副作用——把 API payload 整成
agent 可翻页、可定向查询的响应结构：

- ``list_mods`` / ``get_load_order``：filter + fields + limit/offset 分页字段；
- ``get_groups``：limit/offset 分页字段（不传 limit 仍返回全量，向后兼容）；
- ``list_profiles`` / ``list_log_files``：成功列表 → 全量响应（前者顺带剥离大载荷
  整序数组）；
- ``get_settings_overview``：按 ``package_id`` 定向取单模组设置条目（缺省全量透传）；
- ``read_logs`` + ``_newest_log_name``：日志文件名缺省时取最新文件 + 尾读优先的分页扫描。

注册表、调用锁、扫描门控、上游载荷 → 统一错误结构转换（``_raise_upstream_failure``）
都留在 ``mcp_server``。

依赖：``backend.mcp_guard``（分页字段构造）+ ``backend.mcp_errors``（因果错误构造）+
函数体内 lazy import 的 ``backend.mcp_logview`` / ``package_tokens``；
**不 import ``mcp_server``**（单向依赖，防循环）。
"""

from __future__ import annotations

import os
from typing import Any

from backend.mcp_errors import _PARAM_ACTIONS, _tool_error
from backend.mcp_guard import page_envelope

__all__ = [
    "_handle_get_game_status",
    "_handle_get_groups",
    "_handle_get_load_order",
    "_handle_get_settings_overview",
    "_handle_list_log_files",
    "_handle_list_mods",
    "_handle_list_profiles",
    "_handle_parse_load_order",
    "_handle_read_logs",
    "_newest_log_name",
]

# ---------------------------------------------------------------------------
# 查询适配器：分页 / 过滤 / 投影
# ---------------------------------------------------------------------------


def _text_of(value: Any) -> str:
    return str(value).casefold() if value is not None else ""


def _record_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("package_id") or item.get("packageId") or "")
    return str(item or "")


def _match_filter(item: dict[str, Any], needle: str) -> bool:
    """filter = name + packageId + workshopId 子串匹配（大小写不敏感）。

    ``workshop_id`` 可能是 int 也可能是 str——统一经 ``_text_of`` 字符串归一后
    再比，按工坊 id 找模组不必绕道 list_mods 全量再手筛。
    """
    if not needle:
        return True
    if needle in _text_of(item.get("name")) or needle in _text_of(_record_id(item)):
        return True
    return needle in _text_of(item.get("workshop_id"))


def _project(item: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    if not fields:
        return item
    wanted = set(fields)
    return {key: value for key, value in item.items() if key in wanted}


def _slice_page(
    items: list[Any],
    *,
    offset: int,
    limit: int | None,
) -> list[Any]:
    offset = max(0, int(offset or 0))
    if limit is None:
        return items[offset:]
    return items[offset : offset + max(0, int(limit))]


# list_mods.scope 取值（与 mcp_tool_specs.list_mods 签名的 Literal 同源）
_LIST_MODS_SCOPES = ("all", "active", "inactive")


def _active_id_set(payload: dict[str, Any]) -> set[str]:
    """激活 id 集合（casefold）。

    ``active_load_order`` 里的条目是**包 token**（带 ``_steam`` / ``_local`` 来源
    后缀），必须经 ``parse_package_token`` 归一到 canonical id 再与 all_mods 的
    ``package_id`` 比对（与 ``mcp_deps.load_edge_index`` 同一比对方式）——直接比对其
    字符串会让 ``is_active`` 对全部联接模组恒为 False。
    """
    from backend.load_order.package_tokens import parse_package_token

    active: set[str] = set()
    for entry in payload.get("active_load_order") or []:
        token = _text_of(_record_id(entry))
        if not token:
            continue
        canonical = parse_package_token(token).canonical_package_id or token
        active.add(canonical.casefold())
    return active


def _handle_list_mods(api: Any, **kwargs: Any) -> Any:
    """list_mods：all_mods 主表 + filter/scope/fields/limit/offset 分页字段。

    ``scope`` 只在适配层过滤（复用 ``_active_id_set`` 的激活集合），不改上游
    ``get_mod_list_core`` 的返回：all=不过滤（默认）、
    active/inactive=按 ``is_active`` 二选一。
    """
    payload = api.get_mod_list_core()
    if payload.get("status") != "success":
        return payload  # 错误/警告载荷原样透传（由语义层护栏序列化）
    data = payload.get("data") or {}
    mods: list[dict[str, Any]] = list(data.get("all_mods") or [])
    active = _active_id_set(data)
    needle = _text_of(kwargs.get("filter") or "")
    fields = kwargs.get("fields") or None
    offset = int(kwargs.get("offset") or 0)
    limit = kwargs.get("limit")
    scope = (_text_of(kwargs.get("scope")) or "all").lower()
    if scope not in _LIST_MODS_SCOPES:
        raise _tool_error(
            f"不支持的 scope: {scope}",
            ["scope 取值：all（全库）/ active（仅启用）/ inactive（仅未启用）"],
        )

    matched: list[dict[str, Any]] = []
    for mod in mods:
        if not isinstance(mod, dict) or not _match_filter(mod, needle):
            continue
        is_active = _text_of(_record_id(mod)) in active
        if scope == "active" and not is_active:
            continue
        if scope == "inactive" and is_active:
            continue
        record = _project(dict(mod), fields)
        # is_active 是适配层标注的元数据，投影后补齐（字段清单管不到它）。
        record["is_active"] = is_active
        matched.append(record)
    page = _slice_page(matched, offset=offset, limit=limit)
    return page_envelope(page, total=len(matched), offset=offset, limit=limit)


def _handle_get_load_order(api: Any, **kwargs: Any) -> Any:
    """get_load_order：按加载顺序的记录列表 + version_token + 分页字段。

    ``fields`` 投影与 list_mods 同序同语义：先按 filter 命中 → 再投影；
    version_token/file/modify_time 在响应结构的 extra 字段上，不受投影影响。
    """
    payload = api.load_order_get()
    if payload.get("status") != "success":
        return payload
    data = payload.get("data") or {}
    ids: list[Any] = list(data.get("active_ids") or [])
    names = list(data.get("mod_names") or [])
    workshop_ids = list(data.get("mod_steam_workshop_ids") or data.get("workshop_ids") or [])

    records: list[dict[str, Any]] = []
    for index, package_id in enumerate(ids):
        record: dict[str, Any] = {"package_id": package_id}
        if index < len(names):
            record["name"] = names[index]
        if index < len(workshop_ids):
            record["workshop_id"] = workshop_ids[index]
        records.append(record)

    needle = _text_of(kwargs.get("filter") or "")
    fields = kwargs.get("fields") or None
    offset = int(kwargs.get("offset") or 0)
    limit = kwargs.get("limit")
    # 与 _handle_list_mods 一致：filter 命中后再投影（filter 看 name/package_id/
    # workshop_id，投影不应影响过滤；未知字段静默忽略，同 _project 语义）。
    matched = [_project(item, fields) for item in records if _match_filter(item, needle)]
    page = _slice_page(matched, offset=offset, limit=limit)
    extra: dict[str, Any] = {
        "version_token": data.get("version_token"),
        "file": data.get("file"),
        "modify_time": data.get("modify_time"),
    }
    if data.get("warnings"):
        extra["warnings"] = data["warnings"]
    if data.get("errors"):
        extra["errors"] = data["errors"]
    return page_envelope(page, total=len(matched), offset=offset, limit=limit, extra=extra)


# 档案行里的整序数组是大载荷源头（inactive/temp 顺序各可达数百项，单档案响应 ~130KB
# 量级）——默认剥离：激活顺序的真相源是 get_load_order，停用/临时顺序按需
# 用 activate_profile 切档后再查。
_PROFILE_HEAVY_KEYS = ("inactive_mods_order", "temp_mods_order")


def _handle_list_profiles(api: Any, **kwargs: Any) -> Any:
    payload = api.profiles_get()
    if payload.get("status") == "success" and isinstance(payload.get("data"), list):
        items = [
            {key: value for key, value in profile.items() if key not in _PROFILE_HEAVY_KEYS}
            if isinstance(profile, dict)
            else profile
            for profile in payload["data"]
        ]
        return page_envelope(items, total=len(items), offset=0, limit=None)
    return payload


def _handle_get_groups(api: Any, **kwargs: Any) -> Any:
    """get_groups：分组列表 + limit/offset 分页字段（不传 limit 仍返回全量）。

    分页只在适配层做（上游 ``groups_get`` 恒全量）——每个分组带 ``mod_ids`` 数组，
    分组多时全量响应可观，允许按页取；不传 limit 时返回全量。
    """
    payload = api.groups_get()
    if payload.get("status") == "success" and isinstance(payload.get("data"), list):
        items = payload["data"]
        offset = int(kwargs.get("offset") or 0)
        limit = kwargs.get("limit")
        page = _slice_page(items, offset=offset, limit=limit)
        return page_envelope(page, total=len(items), offset=offset, limit=limit)
    return payload


def _handle_get_settings_overview(api: Any, **kwargs: Any) -> Any:
    """get_settings_overview：按 package_id 定向取单模组设置条目（缺省全量透传）。

    上游 ``api.mod_settings_get_overview``（内部走 ``ModSettingsManager.
    get_overview``）按模组聚合返回 ``mod_groups[]``，每组自带 ``package_id``
    （未识别文件的组该字段为空串）——过滤据此在适配层做，上游不改：

    - ``package_id`` 缺省（空串）→ 原样透传全量概览（含 total_files /
      matched_mod_count 等扫描级统计）；
    - 给定 ``package_id`` → 只保留与 ``mod_groups[].package_id`` **精确匹配**
      （忽略大小写）的组，返回 ``status/data`` 结构，data 只含
      ``config_path`` + 回显的 ``package_id`` + 命中的 ``mod_groups``——扫描级
      统计量按全量计算，不随行，避免与过滤后的条目数互相矛盾；
    - 无命中 → ``mod_groups`` 为空并附 ``hint`` 指路（过滤空结果不是错误）；
    - 上游 error/warning 载荷 → 原样透传（由语义层转统一错误结构）。
    """
    payload = api.mod_settings_get_overview()
    package_id = str(kwargs.get("package_id") or "").strip()
    if not package_id:
        return payload
    if payload.get("status") != "success" or not isinstance(payload.get("data"), dict):
        return payload
    data = payload["data"]
    needle = _text_of(package_id)
    groups = [
        group
        for group in (data.get("mod_groups") or [])
        if isinstance(group, dict) and _text_of(group.get("package_id")) == needle
    ]
    result: dict[str, Any] = {
        "config_path": data.get("config_path"),
        "package_id": package_id,
        "mod_groups": groups,
    }
    if not groups:
        result["hint"] = (
            "该 package_id 在当前档案的设置总览里没有条目；用 list_mods 的 filter "
            "核对包名，或不传 package_id 取全量概览"
        )
    return {"status": "success", "data": result}


def _handle_list_log_files(api: Any, **kwargs: Any) -> Any:
    payload = api.get_log_files(
        str(kwargs.get("log_type") or "game"),
        str(kwargs.get("profile_scope") or "active"),
    )
    if payload.get("status") == "success" and isinstance(payload.get("data"), list):
        items = payload["data"]
        return page_envelope(items, total=len(items), offset=0, limit=None)
    return payload


def _newest_log_name(api: Any, log_type: str, profile_scope: str = "active") -> str:
    """日志文件缺省解析：filename 未给时取最新的一个。"""
    try:
        payload = api.get_log_files(log_type, profile_scope)
    except Exception:  # noqa: BLE001 - 缺省解析失败不应打断调用
        return ""
    files = payload.get("data") if isinstance(payload, dict) else None
    if payload.get("status") == "success" and isinstance(files, list) and files:
        return str(files[0].get("name") or "")
    return ""


def _handle_read_logs(api: Any, **kwargs: Any) -> Any:
    """read_logs：分级/范围/关键字过滤 + 尾读优先的分页扫描（纯适配，零改后端）。

    **参数语义**：

    - 返回条数只由 ``limit``（默认 100）/ ``tail`` 决定，``page_size`` 管不到它；
    - ``page_size``（默认 1000）是**每次向后端 ``api.read_log_page`` 拉取的块数**
      （＝内部扫描批次大小，沿 ``scan_pages(page_size=...)`` → ``fetch_page(page,
      page_size)`` 一路传到 ``read_log_page(log_type, filename, page, page_size)``），
      影响的是一次扫描最多能读多少页（``DEFAULT_MAX_SCAN_PAGES`` 20 页 × page_size）
      与「凑够即停」的页粒度，**不是结果集大小**——想少返回请用 limit/tail。
    """
    import backend.mcp_logview as mcp_logview

    log_type = str(kwargs.get("log_type") or "game")
    filename = str(kwargs.get("filename") or "")
    profile_scope = str(kwargs.get("profile_scope") or "active")
    if not filename:
        filename = _newest_log_name(api, log_type, profile_scope)
        if not filename:
            raise _tool_error(
                "当前档案没有可读日志文件",
                ["用 list_log_files 确认 log_type 是否正确", "游戏/后端产生日志后重试"],
            )

    def fetch_page(page: int, page_size: int) -> dict[str, Any]:
        payload = api.read_log_page(
            log_type, filename, page, page_size, profile_scope
        )
        if payload.get("status") == "error":
            message = str(payload.get("message") or "日志读取失败")
            raise _tool_error(
                message,
                ["用 list_log_files 核对 filename", "确认 log_type/profile_scope 取值"],
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else {"blocks": []}

    offset = int(kwargs.get("offset") or 0)
    limit = int(kwargs.get("limit") or 100)
    tail = kwargs.get("tail")
    try:
        # levels / since / until 由 scan_pages 统一解析（相对量或 ISO），解析失败
        # 以 ValueError 上抛 → _invoke 转成带约束提示的因果错误。
        result = mcp_logview.scan_pages(
            fetch_page,
            levels=kwargs.get("levels"),
            since=kwargs.get("since"),
            until=kwargs.get("until"),
            line_from=kwargs.get("line_from"),
            line_to=kwargs.get("line_to"),
            keyword=kwargs.get("keyword"),
            tail=int(tail) if tail else None,
            limit=limit,
            offset=0 if tail else offset,
            page_size=int(kwargs.get("page_size") or 1000),
        )
    except ValueError as exc:
        raise _tool_error(
            str(exc),
            ["检查 since/until/levels 取值：相对量如 30m/1h/2d，或 ISO 时间"],
        ) from exc
    consumed = (0 if tail else offset) + len(result["items"])
    extra: dict[str, Any] = {
        "filename": filename,
        "log_type": log_type,
        "scanned_pages": result["scanned_pages"],
    }
    if tail:
        extra["next_offset"] = None  # 尾读只取尾，不翻页
        # 分页模式标记：告诉语义层 _realign_page_offset 跳过重算（它会把 tail 的
        # next_offset=None 反解成 total-original_len，按分页坐标重排）。非 tail 不带此键。
        extra["paging"] = "tail"
    elif not result["exhausted"] and consumed >= int(result["total"]):
        extra["next_offset"] = consumed  # total 是下界，还有页未扫
        extra["hint"] = "total 为已扫描匹配数的下界；继续用 offset 翻页或放宽过滤条件"
    return page_envelope(
        result["items"], total=int(result["total"]), offset=0 if tail else offset, limit=limit, extra=extra
    )


# ---------------------------------------------------------------------------
# 排序文件解析 / 游戏运行状态
# ---------------------------------------------------------------------------


def _handle_parse_load_order(api: Any, **kwargs: Any) -> Any:
    """parse_load_order：排序文件 / 分享码 → 解析结果 + import_check（零写入）。

    ``load_order_file_open`` 对「目录 / 不存在的路径」会落到 ``select_file_dialog``
    （该分支会弹出选择框）→ MCP 进程内会阻塞等待；本 handler 在入口校验「path 必须是
    已存在文件」。后端对「解析成功但列表为空」返回 warning 且 data 里带完整结果
    ——warning 被转成成功载荷里的 ``warning`` 字段，信息不丢（其余 error/warning
    原样上抛由管线转统一错误结构）。
    """
    path = str(kwargs.get("path") or "").strip()
    share_code = str(kwargs.get("share_code") or "").strip()
    if not path and not share_code:
        raise _tool_error("path 与 share_code 至少给一个", _PARAM_ACTIONS)
    if path and not os.path.isfile(path):
        raise _tool_error(
            f"path 不是已存在的文件：{path}",
            ["确认文件路径正确（后端对目录/缺失路径会弹文件对话框，MCP 进程内会阻塞，故直接拒绝）"],
        )

    payload = api.load_order_file_open(path) if path else api.load_order_share_import(share_code)
    status = payload.get("status")
    if status == "error":
        return payload
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    ids = list(data.get("active_ids") or [])
    result: dict[str, Any] = {
        "status": "success",
        "source": "path" if path else "share_code",
        "format": data.get("format"),
        "total": len(ids),
        "active_ids": ids,
        "warnings": list(data.get("warnings") or []),
        "errors": list(data.get("errors") or []),
        "import_check": data.get("import_check") or {"summary": {}, "items": []},
    }
    if data.get("list_name"):
        result["list_name"] = data["list_name"]
    if path:
        result["file"] = data.get("file") or path
    else:
        if data.get("share_code_ref"):
            result["share_code_ref"] = data["share_code_ref"]
    if status == "warning":
        result["warning"] = str(payload.get("message") or "")
    return result


def _handle_get_game_status(api: Any, **kwargs: Any) -> Any:
    """get_game_status：游戏进程 / Steam 客户端 / 启动会话三态（启动流程的验证入口）。

    ``_get_runtime_session_data`` 是 api 上启动会话降级链的唯一实现（monitor 形态
    变化由它吸收），适配层直接复用而不复制 fallback 逻辑。
    """
    monitor = getattr(api, "game_monitor", None)
    running = bool(getattr(monitor, "is_game_running", False))
    try:
        session = api._get_runtime_session_data()
    except Exception:  # noqa: BLE001 - 会话数据缺失不挡进程状态上报
        session = {}
    steam_mgr = getattr(api, "steam_mgr", None)
    try:
        steam_running = bool(steam_mgr.is_steam_running()) if steam_mgr else None
    except Exception:  # noqa: BLE001 - Steam 状态探测失败置空，不谎报
        steam_running = None
    return {
        "status": "success",
        "running": running,
        "steam_running": steam_running,
        "runtime_session": session,
    }
