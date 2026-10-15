# backend/mcp_handlers_features.py
"""功能 handler：依赖图查询、加载顺序写入、用户规则、导出/解析/智能插入、游戏启动。

职责：多步业务 handler——带参数校验 / 因果错误 / 副作用，
或依赖专项后端模块的整段流程；与负责「单步只读结果整形」的 ``mcp_adapters_query``
相对：

- ``get_mod_dependencies``：依赖图遍历（depth/format 校验 → 因果错误）；
- ``apply_load_order_ops``：读-校验-应用-单次落盘，全有全无；
- ``set_mod_rule``：单条用户规则先读后合并的整包写回；
- ``export_load_order``（显式 path，防 dialog 挂起）、
  ``smart_insert_mods``（token 保护下按规则插位落盘）、
  ``resolve_missing_dependencies``（缺依赖 → 工坊 ID）、
  ``launch_game``（两阶段启动确认）。
- 任务控制 / 模组文件读取 / 工坊详情：``cancel_task``（统一取消入口的类型白名单 + 参数校验）、
  ``read_mod_file``（模组根白名单的路径边界校验，拒绝 ``..`` 与越界读取）、
  ``get_workshop_mod_details``（工坊详情批量查询 + 条数上限）。
- Git 仓库订阅：``github_subscribe_repo``（先取仓库信息组装 payload，取信息失败即因果
  错误不落订阅记录，订阅与部署一次完成）。

写路径的读-改-写原子性靠 ``mcp_server`` 的工具层全局调用锁（``_CALL_LOCK``）
保证——锁本身留在 ``mcp_server``，本模块不碰。

依赖：``backend.mcp_guard`` + ``backend.mcp_errors`` + 函数体内 lazy import 的
``backend.mcp_deps`` / ``backend.mcp_loadops`` / ``backend.api``（仅取
``LaunchWarningAction`` 值域，不复制字面量）；**不 import ``mcp_server``**
（单向依赖，防循环）。
"""

from __future__ import annotations

import json
import os
from typing import Any

from backend.mcp_errors import _PARAM_ACTIONS, _tool_error

__all__ = [
    "_handle_apply_load_order_ops",
    "_handle_cancel_task",
    "_handle_export_load_order",
    "_handle_get_mod_dependencies",
    "_handle_get_workshop_mod_details",
    "_handle_github_subscribe_repo",
    "_handle_launch_game",
    "_handle_read_mod_file",
    "_handle_resolve_missing_dependencies",
    "_handle_set_mod_rule",
    "_handle_smart_insert_mods",
]

# ---------------------------------------------------------------------------
# 依赖图、加载顺序增量操作、用户规则写入
# ---------------------------------------------------------------------------


def _handle_get_mod_dependencies(api: Any, **kwargs: Any) -> Any:
    from backend.mcp_deps import query_dependencies

    try:
        return query_dependencies(
            api,
            package_id=str(kwargs.get("packageId") or ""),
            direction=str(kwargs.get("direction") or "both"),
            depth=int(kwargs.get("depth") or 1),
            fmt=str(kwargs.get("format") or "edges"),
        )
    except ValueError as exc:
        raise _tool_error(
            str(exc),
            [
                "用 search_mods 核对 packageId 拼写",
                "depth 限制 1..5；format=tree 仅 depth<=2",
            ],
        ) from exc


def _handle_apply_load_order_ops(api: Any, **kwargs: Any) -> Any:
    """apply_load_order_ops：读-校验-应用-单次落盘，全有全无。"""
    import backend.mcp_loadops as mcp_loadops

    base_token = kwargs.get("base_version_token")
    ops = kwargs.get("ops")
    dry_run = bool(kwargs.get("dry_run"))
    if not isinstance(base_token, dict) or not base_token:
        raise _tool_error(
            "缺少 base_version_token（磁盘冲突保护）",
            ["先调用 get_load_order 获取 version_token", "把 token 原样传入 base_version_token"],
        )

    current_ids = _read_current_actives(api, base_token)
    try:
        result = mcp_loadops.apply_ops(current_ids, ops)
    except ValueError as exc:
        raise _tool_error(str(exc), _PARAM_ACTIONS) from exc
    if not result.ok:
        raise _tool_error(
            f"{len(result.conflicts)} 个操作冲突，已整体拒绝（未做任何修改）",
            ["按 conflicts 逐条修正锚点/id 后重试", "用 dry_run:true 先预演再落盘"],
            extra={"conflicts": [conflict.to_dict() for conflict in result.conflicts]},
        )

    summary = {
        "inserted": result.summary.get("inserted", 0),
        "removed": result.summary.get("removed", 0),
        "moved": result.summary.get("moved", 0),
        "activated": result.summary.get("activated", 0),
        "deactivated": result.summary.get("deactivated", 0),
        "skipped": result.summary.get("skipped", 0),
    }
    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "ok": True,
            "applied": result.applied,
            "summary": summary,
            "conflicts": [],
        }

    data = _save_actives_with_token(api, result.ordered, base_token)
    return {
        "status": "success",
        "ok": True,
        "new_version_token": data.get("version_token"),
        "modify_time": data.get("modify_time"),
        "applied": result.applied,
        "summary": summary,
        "conflicts": [],
    }


def _read_current_actives(api: Any, base_token: dict) -> list[str]:
    """token 过期校验 + 读当前激活列表（apply_load_order_ops / smart_insert_mods 共用）。

    错误文案与 apply_load_order_ops 的既有文案逐字一致（agent 靠 nextActions 自纠，
    不一致会破坏既有测试与自纠链路）。
    """
    context_missing = getattr(api, "active_context", None) is None or getattr(api, "load_order_mgr", None) is None
    if context_missing:
        raise _tool_error(
            "当前档案上下文未初始化，无法写入加载顺序",
            ["确认档案可用（list_profiles / activate_profile）", "稍后重试"],
        )
    try:
        is_stale, _current_token = api.load_order_mgr.is_version_token_stale(base_token)
    except Exception as exc:  # noqa: BLE001 - token 校验失败按因果错误上报
        raise _tool_error(f"版本 token 校验失败：{type(exc).__name__}", _PARAM_ACTIONS) from exc
    if is_stale:
        raise _tool_error(
            "磁盘加载顺序已被外部修改，base_version_token 过期，已拒绝本次操作",
            [
                "重新调用 get_load_order 获取最新 version_token",
                "确认没有其它实例（RimCrow 界面/游戏）在改 ModsConfig.xml",
                "用新 token 重试同一组 ops",
            ],
        )
    read_result = api.load_order_mgr.read_active_mods()
    return list(read_result.get("active_mods") or [])


def _save_actives_with_token(api: Any, ordered_ids: list[str], base_token: dict) -> dict[str, Any]:
    """带 token 保护的最终落盘（apply_load_order_ops / smart_insert_mods 共用）。

    返回成功 data（含 version_token / modify_time）；warning（写盘前最后竞态或备份
    降级）与失败一律转因果错误。
    """
    save = api.load_order_save_with_token(ordered_ids, base_version_token=base_token)
    status = save.get("status")
    if status == "warning":
        message = str(save.get("message") or "保存被拒绝")
        raise _tool_error(
            message,
            ["重新调用 get_load_order 获取最新 version_token", "用新 token 重试同一组 ops"],
        )
    if status != "success":
        raise _tool_error(
            str(save.get("message") or "加载顺序保存失败"),
            ["查看 list_log_files → read_logs 中的后端错误", "确认 ModsConfig.xml 可写"],
        )
    return save.get("data") or {}


# 用户单项规则的三种关系（与 AI 助手 MOD_RULE 动作、规则引擎的消费方式同源）。
_RULE_RELATIONS = ("loadAfter", "loadBefore", "incompatibleWith")


def _mod_display_name(package_id: str) -> str:
    """取 target 的展示名供规则管理界面渲染（引擎不消费 name；拿不到就省略）。"""
    try:
        from peewee import fn

        from backend.database.models import ModAsset

        row = ModAsset.get_or_none(fn.Lower(ModAsset.package_id) == package_id.casefold())
        return str(row.name) if row and row.name else ""
    except Exception:  # noqa: BLE001 - 展示名仅用于界面渲染，取失败不挡规则写入
        return ""


def _handle_set_mod_rule(api: Any, **kwargs: Any) -> Any:
    """set_mod_rule：单条用户规则的合并写入/删除（AI 助手 MOD_RULE 动作的同源转发）。

    ``rule_mgr.update_user_mod_rule`` 是**整包替换**语义（直接覆盖该模组的全部用户
    规则）——危险的整包写不暴露给工具面：这里先读既有规则、只动 relation 指向的
    一条，再整体写回（工具层全局调用锁保证读-改-写原子）。target 键按引擎消费方式
    casefold（``get_effective_mod_rules`` 解析 user 规则时对 target 做小写归一）。
    内容无变化时短路返回 noop，不触碰规则文件；旧格式 ``rules`` 包装删到空会弹掉
    包装键 → 该模组无内容时整条删除，绝不把空壳写回规则文件。
    """
    rule_mgr = getattr(getattr(api, "sorter", None), "rule_mgr", None)
    if rule_mgr is None:
        raise _tool_error(
            "规则引擎未初始化，无法读写用户规则",
            ["确认档案可用（list_profiles / activate_profile）", "稍后重试"],
        )
    package_id = str(kwargs.get("package_id") or "").strip()
    target_id = str(kwargs.get("target_id") or "").strip()
    relation = str(kwargs.get("relation") or "").strip()
    comment = str(kwargs.get("comment") or "")
    remove = bool(kwargs.get("remove"))
    if not package_id or not target_id:
        raise _tool_error("package_id 与 target_id 均不能为空", _PARAM_ACTIONS)
    if package_id.casefold() == target_id.casefold():
        raise _tool_error("package_id 与 target_id 不能相同（自指规则无意义）", _PARAM_ACTIONS)
    if relation not in _RULE_RELATIONS:
        raise _tool_error(
            f"relation 仅支持 {'/'.join(_RULE_RELATIONS)}: {relation!r}", _PARAM_ACTIONS
        )

    pid = package_id.casefold()
    target_key = target_id.casefold()
    current = rule_mgr.user_mod_rules.get(pid)
    if current is not None and not isinstance(current, dict):
        raise _tool_error(
            f"模组 {pid} 的既有用户规则不是对象形态（{type(current).__name__}），拒绝合并写入",
            ["用 get_mod_rules 查看现状；如需清理请人工处理规则文件"],
        )

    before = json.dumps(current or {}, sort_keys=True, default=str)
    if current is None:
        current = {}
    # ``rules`` 内层包装是双格式读取：上游 mgr_rules._merge_rule 同样按
    # `rules = user.get("rules", user)` 兼容旧格式，本工具与之保持一致。
    container = current["rules"] if isinstance(current.get("rules"), dict) else current
    relations = container.setdefault(relation, {})
    if not isinstance(relations, dict):
        raise _tool_error(
            f"既有 {relation} 规则不是对象形态（{type(relations).__name__}），拒绝合并写入",
            ["用 get_mod_rules 查看现状", "如需清理请人工处理规则文件"],
        )

    removed = False
    if remove:
        removed = relations.pop(target_key, None) is not None
    else:
        entry: dict[str, Any] = {}
        if comment:
            entry["comment"] = comment
        display_name = _mod_display_name(target_id)
        if display_name:
            entry["name"] = [display_name]
        relations[target_key] = entry
    if isinstance(container.get(relation), dict) and not container[relation]:
        del container[relation]  # 清掉本次写空的关系组，不让空壳留在规则文件里

    if json.dumps(current, sort_keys=True, default=str) == before:
        return {
            "status": "success",
            "ok": True,
            "action": "noop",
            "package_id": pid,
            "relation": relation,
            "target_id": target_key,
            "removed": False,
        }

    if not container and container is not current:
        # 旧格式包装路径下容器与 current 不是同一对象：内层删到空先弹掉 "rules" 键，
        # 否则会把 {"rules": {}} 这种空壳整包写回规则文件
        current.pop("rules", None)
    if not current:
        # 该模组的用户规则已无任何内容 → 整条删除，保持规则文件干净
        save = api.rule_delete_user_mod(package_id)
    else:
        save = api.rule_update_user_mod(package_id, current)
    if isinstance(save, dict) and save.get("status") not in ("success", "warning"):
        raise _tool_error(
            str(save.get("message") or "用户规则保存失败"),
            ["确认规则文件可写（用 list_log_files → read_logs 看后端错误）", "稍后重试"],
        )
    return {
        "status": "success",
        "ok": True,
        "action": "removed" if remove else "upserted",
        "package_id": pid,
        "relation": relation,
        "target_id": target_key,
        "removed": removed,
    }


# ---------------------------------------------------------------------------
# 导出 / 智能插入 / 缺依赖解析 / 游戏启动
# ---------------------------------------------------------------------------

# 与上游 LoadOrderManager 的 FORMAT_* 常量同域；share_code 是工具层组合能力
# （load_order_share_export），不属文件导出，单独列出。
_EXPORT_FORMATS = ("modsconfig", "modlist", "rml", "share_code")

_SMART_INSERT_MAX = 100  # 与 apply_load_order_ops 的 ops 上限同档
_MISSING_DEPS_MAX = 500


def _handle_export_load_order(api: Any, **kwargs: Any) -> Any:
    """export_load_order：显式 path 文件导出 / 分享码内联返回。

    ``load_order_export`` 在无 path 时会把 ``trigger_dialog`` 强制回 True
    （api.py：``if not target_path and not trigger_dialog``）→ 弹文件对话框，
    MCP 进程内会阻塞等待；本 handler 在入口就要求「文件格式必须带 path」。
    """
    export_format = str(kwargs.get("export_format") or "").strip()
    path = str(kwargs.get("path") or "").strip()
    list_name = str(kwargs.get("list_name") or "").strip()
    if export_format not in _EXPORT_FORMATS:
        raise _tool_error(
            f"export_format 仅支持 {'/'.join(_EXPORT_FORMATS)}: {export_format!r}",
            _PARAM_ACTIONS,
        )
    if export_format == "share_code" and path:
        raise _tool_error("export_format=share_code 直接内联返回，path 必须为空", _PARAM_ACTIONS)

    mgr = getattr(api, "load_order_mgr", None)
    if mgr is None:
        raise _tool_error(
            "加载顺序管理器未初始化，无法读取当前激活列表",
            ["确认档案可用（list_profiles / activate_profile）", "稍后重试"],
        )
    active_ids = list(mgr.read_active_mods().get("active_mods") or [])
    if not active_ids:
        raise _tool_error(
            "当前激活列表为空，没有可导出的加载顺序",
            ["先用 apply_load_order_ops 启用模组", "确认档案是否正确（list_profiles）"],
        )

    if export_format == "share_code":
        return api.load_order_share_export(active_ids, list_name=list_name or None)

    if not path:
        raise _tool_error(
            "文件导出必须给 path（完整目标文件路径，含扩展名）——MCP 无文件对话框，"
            "后端不会代选路径",
            ["给出绝对路径后重试", "只要分享串就用 export_format=share_code"],
        )
    if os.path.isdir(path):
        raise _tool_error(f"path 是目录，需要完整文件路径：{path}", _PARAM_ACTIONS)

    res = api.load_order_export(
        active_ids,
        target_path=path,
        trigger_dialog=False,
        export_format=export_format,
        list_name=list_name or None,
        remember_dialog_dir=False,
    )
    if res.get("status") != "success":
        raise _tool_error(
            str(res.get("message") or "导出未成功"),
            ["确认目标路径可写、磁盘空间充足", "查看 list_log_files → read_logs 中的后端错误"],
        )
    result: dict[str, Any] = {
        "status": "success",
        "path": path,
        "format": export_format,
        "count": len(active_ids),
    }
    if list_name:
        result["list_name"] = list_name
    return result


def _handle_smart_insert_mods(api: Any, **kwargs: Any) -> Any:
    """smart_insert_mods：规则引擎插位——token 保护 + dry_run 预演 + 单次落盘。

    复用 apply_load_order_ops 的骨架（``_read_current_actives`` /
    ``_save_actives_with_token``）；锚点 diff 只做展示，真相源是排序引擎返回的
    final 列表，响应绝不回传全表。
    """
    base_token = kwargs.get("base_version_token")
    dry_run = bool(kwargs.get("dry_run"))
    if not isinstance(base_token, dict) or not base_token:
        raise _tool_error(
            "缺少 base_version_token（磁盘冲突保护）",
            ["先调用 get_load_order 获取 version_token", "把 token 原样传入 base_version_token"],
        )
    raw_ids = kwargs.get("package_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise _tool_error("package_ids 不能为空", _PARAM_ACTIONS)
    if len(raw_ids) > _SMART_INSERT_MAX:
        raise _tool_error(
            f"package_ids 上限 {_SMART_INSERT_MAX} 条（当前 {len(raw_ids)}）",
            ["分批插入"],
        )
    targets = [str(pid or "").strip() for pid in raw_ids if str(pid or "").strip()]
    if not targets:
        raise _tool_error("package_ids 没有有效 package_id", _PARAM_ACTIONS)

    current_ids = _read_current_actives(api, base_token)
    res = api.smart_insert_mod_in_actives(targets, current_ids)
    if res.get("status") != "success":
        raise _tool_error(
            str(res.get("message") or "智能插入失败"),
            [
                "用 search_mods 核对 package_id 是否已安装（未安装则先获取该模组）",
                "查看 list_log_files → read_logs 中的后端错误",
            ],
        )
    final_ids = list(res.get("data") or [])

    current_set = set(current_ids)
    requested = {pid.casefold() for pid in targets}
    from backend.load_order.package_tokens import parse_package_token

    def _canonical(token: str) -> str:
        return parse_package_token(token).canonical_package_id or token

    anchors: list[dict[str, Any]] = []
    auto_activated: list[str] = []
    for index, token in enumerate(final_ids):
        if token in current_set:
            continue
        anchors.append(
            {"id": token, "index": index, "after": final_ids[index - 1] if index else None}
        )
        if _canonical(token).casefold() not in requested:
            auto_activated.append(token)
    summary = {
        "requested": len(targets),
        "inserted": len(anchors),
        "auto_activated": len(auto_activated),
        "final_count": len(final_ids),
    }
    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "applied": anchors,
            "auto_activated": auto_activated,
            "summary": summary,
        }

    data = _save_actives_with_token(api, final_ids, base_token)
    return {
        "status": "success",
        "new_version_token": data.get("version_token"),
        "modify_time": data.get("modify_time"),
        "applied": anchors,
        "auto_activated": auto_activated,
        "summary": summary,
    }


def _handle_resolve_missing_dependencies(api: Any, **kwargs: Any) -> Any:
    """resolve_missing_dependencies：缺依赖 → 可下载工坊 ID（供后续下载获取）。

    package_ids 缺省时服务端自读当前激活列表（agent 不必回传全表）；
    给定列表用于「解析别人的分享码前先看缺什么」的场景。
    """
    raw = kwargs.get("package_ids")
    if raw is None:
        mgr = getattr(api, "load_order_mgr", None)
        current = mgr.read_active_mods() if mgr else {}
        ids = list(current.get("active_mods") or []) if isinstance(current, dict) else []
        if not ids:
            raise _tool_error(
                "当前激活列表为空且未提供 package_ids，无法检测缺失依赖",
                ["先启用模组（apply_load_order_ops）", "或传入要检查的 package_ids"],
            )
    else:
        if not isinstance(raw, list):
            raise _tool_error("package_ids 必须是字符串列表", _PARAM_ACTIONS)
        if len(raw) > _MISSING_DEPS_MAX:
            raise _tool_error(
                f"package_ids 上限 {_MISSING_DEPS_MAX} 条（当前 {len(raw)}）",
                ["收窄范围后重试"],
            )
        ids = [str(pid or "").strip() for pid in raw if str(pid or "").strip()]
        if not ids:
            raise _tool_error("package_ids 没有有效值", _PARAM_ACTIONS)

    res = api.lifecycle_resolve_dependencies(ids)
    if res.get("status") != "success":
        raise _tool_error(
            str(res.get("message") or "缺失依赖检测失败"),
            ["查看 list_log_files → read_logs 中的后端错误", "稍后重试"],
        )
    missing = list((res.get("data") or {}).get("missing") or [])
    if not missing:
        return {"status": "success", "missing": [], "count": 0, "message": "前置依赖完整，无需补充。"}
    return {
        "status": "success",
        "missing": missing,
        "count": len(missing),
        "hint": "用 download_workshop_mods([workshop_id, ...]) 补齐；下载成功后会自动触发扫描",
    }


def _launch_warning_action_values() -> set[str]:
    """LaunchWarningAction 值域（lazy import 上游枚举，不复制字面量）。"""
    from backend.api import LaunchWarningAction

    return {item.value for item in LaunchWarningAction}


def _launch_success_payload(res: dict[str, Any]) -> dict[str, Any]:
    data = res.get("data") or {}
    return {
        "status": "success",
        "runtime_session": data.get("runtime_session"),
        "message": str(res.get("message") or ""),
        "hint": 'wait_event("game-status-changed") 或 get_game_status 跟踪启动结果',
    }


def _handle_launch_game(api: Any, **kwargs: Any) -> Any:
    """launch_game：两阶段启动。

    ``game_launch`` 检测到 Steam 冲突等风险时返回确认 warning（
    ``data.action == "confirm_direct_launch"``，与 api.py 的字面量同源——该值不在
    LaunchWarningAction 枚举内，上游即字面量）；本 handler 把它转成 isError +
    nextActions，指引 agent 用 confirm_action 重调继续/取消。
    """
    profile_id = str(kwargs.get("profile_id") or "").strip()
    confirm_action = str(kwargs.get("confirm_action") or "").strip().lower()

    if confirm_action:
        valid = _launch_warning_action_values()
        if confirm_action not in valid:
            raise _tool_error(
                f"confirm_action 取值无效：{confirm_action!r}（可选：{'/'.join(sorted(valid))}）",
                _PARAM_ACTIONS,
            )
        if not profile_id:
            raise _tool_error(
                "确认重调需要带上上次警告里的 profile_id",
                ["从上一次 launch_game 的错误 extra 里取 profile_id"],
            )
        res = api.game_launch_resolve_warning(profile_id, confirm_action)
        status = res.get("status")
        if status == "success":
            return _launch_success_payload(res)
        data = res.get("data") or {}
        if status == "warning":
            if data.get("action") == "wait_steam_exit":
                return {
                    "status": "waiting",
                    "message": str(res.get("message") or ""),
                    "steam_running": bool(data.get("steam_running")),
                    "hint": "稍后用 get_game_status 确认 Steam 已退出，再重新调用",
                }
            return {"status": "cancelled", "message": str(res.get("message") or "已取消启动")}
        raise _tool_error(
            str(res.get("message") or "启动确认失败"),
            ["按 message 修正后重试", "查看 list_log_files → read_logs 中的后端错误"],
        )

    res = api.game_launch(profile_id)
    status = res.get("status")
    if status == "success":
        return _launch_success_payload(res)
    data = res.get("data") or {}
    if status == "warning" and data.get("action") == "confirm_direct_launch":
        raise _tool_error(
            str(res.get("message") or "启动前需要确认"),
            [
                '确认继续直启：带同一 profile_id + confirm_action="continue" 重新调用',
                '等 Steam 退出后再启动：confirm_action="wait_steam_exit"',
                '取消本次启动：confirm_action="cancel"',
            ],
            extra={
                "reason": data.get("reason"),
                "steam_running": data.get("steam_running"),
                "profile_id": data.get("profile_id"),
            },
        )
    return res  # error / 其余 warning 原样返回 → _invoke 转统一错误结构


# ---------------------------------------------------------------------------
# 任务控制 / 模组文件读取 / 工坊详情查询
# ---------------------------------------------------------------------------

# 上游统一取消入口实际路由的全部任务类型（cancel_progress_task 逐分支），
# 与前端可取消集合 frontend/src/app/stores/appStore.js 的 cancellableTaskTypes 一致（16 种）。
# 工具签名里的 Literal 与此处白名单由测试锁定一致，两侧必须同步修改。
_CANCEL_TASK_TYPES = (
    "scan",
    "download",
    "update",
    "localize",
    "mod-import",
    "mod-export",
    "steamcmd-download",
    "steamcmd-workshop-repair",
    "steamcmd-init",
    "steam-subscribe",
    "steam-unsubscribe",
    "steam-workshop-download",
    "texture-opt",
    "texture-opt-analyze",
    "ai-task",
    "file-search",
)

# 单文件读取上限：上游 path_read_text_file 默认 2 MB（上限在 mgr_files.read_text_file），
# 但工具层语义护栏只有 256 KiB × 0.75 = 196608 B（mcp_guard.DEFAULT_MAX_OUTPUT /
# SEMANTIC_RATIO），而 content 是**标量**、超限时无法按元素边界截断，会被护栏整段丢弃
# 成只剩 truncated_fields 的空壳——按最坏 2× JSON 转义（引号/换行）留余量收到 64 KiB，
# 保证「内容 + 截断标记」真的送达 agent。
_READ_MOD_FILE_MAX_BYTES = 64 * 1024

# 上游在线详情按 100 一批请求（mgr_steam_api._request_published_file_details），工具面按同量级收口。
_WORKSHOP_DETAILS_MAX = 100


def _collect_mod_file_roots(api: Any) -> list[str]:
    """当前环境已知的模组根目录（read_mod_file 白名单的两个来源并集）。

    1) 扫描根：``api._build_scan_paths_for_profile``＝DLC / 本地 Mods /
       自身 Mods / 工坊 Mods / 工具模组目录，与 get_residue_overview 返回的
       scan_roots 同源；
    2) 模组自身路径：``ModDAO.get_profile_mods``，补上不在标准扫描根下的
       模组——懒导入写法与 text_search.effective_files.resolve_scope_mods 一致。
    任一来源失败都只丢该来源，不因数据库未就绪把读文件整个挡死。
    """
    context = getattr(api, "active_context", None)
    roots: list[str] = []

    builder = getattr(api, "_build_scan_paths_for_profile", None)
    if callable(builder):
        try:
            roots.extend(str(item) for item in (builder(context) or []) if item)
        except Exception:  # noqa: BLE001 - 路径构建失败时保留另一来源
            pass

    try:
        from backend.database.dao import ModDAO

        for mod in ModDAO.get_profile_mods(context) or []:
            mod_path = str(mod.get("path") or "")
            if mod_path:
                roots.append(mod_path)
    except Exception:  # noqa: BLE001 - 库不可用 / 上下文字段不全时退回扫描根
        pass
    return roots


def _ensure_mod_file_path(path: Any, roots: list[str]) -> str:
    """路径边界校验：只放行模组根目录内的文件路径（拒绝 ``..`` 与越界）。

    判据是「任一形态在任一根之下」——原始形态（abspath 归一，``..`` 已折叠）覆盖
    ``_Link_`` 联接路径，realpath 形态覆盖联接另一侧（工坊真实目录），两者取并集，
    既不放过 ``E:/.../Mods/../../Windows/...`` 这类越界，也不误伤经联接读取的模组文件。
    """
    raw = str(path or "").strip()
    if not raw:
        raise _tool_error(
            "path 不能为空",
            ["用 search_mod_text 先定位文件，取结果里的 file_path 作为入参"],
        )
    if ".." in raw.replace("\\", "/").split("/"):
        raise _tool_error(
            f"path 不允许包含 ..（拒绝越界读取）：{raw}",
            [
                "给出模组根目录内的完整文件路径（不带 ..）",
                "用 search_mod_text 先定位，结果里的 file_path 可直接作为入参",
            ],
        )
    if not roots:
        raise _tool_error(
            "当前环境没有可用的模组根目录，无法校验读取边界",
            ["确认档案可用（list_profiles / activate_profile）", "稍后重试"],
        )

    candidates = {
        os.path.normcase(os.path.abspath(raw)),
        os.path.normcase(os.path.realpath(raw)),
    }
    root_keys: set[str] = set()
    for root in roots:
        root_keys.add(os.path.normcase(os.path.abspath(str(root))))
        root_keys.add(os.path.normcase(os.path.realpath(str(root))))

    for candidate in candidates:
        for root_key in root_keys:
            prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
            if candidate == root_key or candidate.startswith(prefix):
                return raw

    raise _tool_error(
        f"path 不在当前环境的模组根目录内，已拒绝读取：{raw}",
        [
            "只允许读取模组根目录下的文件（DLC / 本地 / 自身 / 工坊 / 工具模组目录）",
            "get_residue_overview 返回的 scan_roots 就是当前可读的根目录清单",
        ],
    )


def _handle_cancel_task(api: Any, **kwargs: Any) -> Any:
    """cancel_task：统一取消入口 cancel_progress_task 的类型白名单 + 参数校验后原样转发。"""
    task_type = str(kwargs.get("task_type") or "").strip().lower()
    task_id = str(kwargs.get("task_id") or "").strip()

    if task_type not in _CANCEL_TASK_TYPES:
        raise _tool_error(
            f"task_type 仅支持 {'/'.join(_CANCEL_TASK_TYPES)}: {task_type!r}",
            _PARAM_ACTIONS,
        )
    if not task_id and task_type != "scan":
        raise _tool_error(
            "缺少 task_id（仅 task_type=scan 允许留空＝取消当前扫描）",
            [
                "task_id 取自发起任务的响应：search_mod_text / download_workshop_mods 的 task_id，"
                "scan_mods 的 data.details.task_id",
                "确认任务仍在进行中后重试",
            ],
        )

    res = api.cancel_progress_task(task_id, task_type)
    if not isinstance(res, dict) or res.get("status") != "success":
        message = res.get("message") if isinstance(res, dict) else ""
        raise _tool_error(
            str(message or "取消任务失败"),
            [
                "任务可能已结束，或 task_id 与 task_type 不匹配——先用 get_download_tasks 刷新任务列表",
                "重新核对发起任务时返回的 task_id 与 task_type",
            ],
        )
    return res


def _handle_read_mod_file(api: Any, **kwargs: Any) -> Any:
    """read_mod_file：模组根白名单内的文本读取（search 定位 → 读全文）。

    边界先于 IO：路径越界一律在 handler 内拒绝，``api.path_read_text_file`` 只会收到
    已通过白名单的路径——本工具因此不是任意文件读取口。读取按
    ``_READ_MOD_FILE_MAX_BYTES`` 收窄，截断标记原样透传给 agent。
    """
    target = _ensure_mod_file_path(kwargs.get("path"), _collect_mod_file_roots(api))
    res = api.path_read_text_file(target, max_bytes=_READ_MOD_FILE_MAX_BYTES)
    if not isinstance(res, dict) or res.get("status") != "success":
        message = res.get("message") if isinstance(res, dict) else ""
        raise _tool_error(
            str(message or "读取文本文件失败"),
            [
                "确认 path 是模组根目录内已存在的文件（不是目录）",
                "用 search_mod_text 先定位，结果里的 file_path 可直接作为本工具入参",
                "查看 list_log_files → read_logs 中的后端错误",
            ],
        )
    data = res.get("data") or {}
    return {
        "status": "success",
        "file_path": str(data.get("path") or target),
        "content": str(data.get("content") or ""),
        "truncated": bool(data.get("truncated")),
        "file_size": data.get("file_size"),
        "encoding": str(data.get("encoding") or ""),
        "max_bytes": _READ_MOD_FILE_MAX_BYTES,
    }


def _handle_get_workshop_mod_details(api: Any, **kwargs: Any) -> Any:
    """get_workshop_mod_details：工坊详情批量查询（mod_settings_workshop_details 透传）+ 条数上限。"""
    raw_ids = kwargs.get("workshop_ids")
    if not isinstance(raw_ids, (list, tuple)):
        raise _tool_error("workshop_ids 必须是字符串数组", _PARAM_ACTIONS)
    ids = [str(item).strip() for item in raw_ids if str(item or "").strip()]
    if not ids:
        raise _tool_error("workshop_ids 不能为空", _PARAM_ACTIONS)
    if len(ids) > _WORKSHOP_DETAILS_MAX:
        raise _tool_error(
            f"workshop_ids 上限 {_WORKSHOP_DETAILS_MAX} 条（当前 {len(ids)}）",
            ["分批查询"],
        )

    res = api.mod_settings_workshop_details(ids)
    if not isinstance(res, dict) or res.get("status") != "success":
        message = res.get("message") if isinstance(res, dict) else ""
        raise _tool_error(
            str(message or "获取工坊信息失败"),
            [
                "检查网络连接与 Steam 服务状态后重试",
                "本地已有的信息可用 trace_install_sources 先查",
            ],
        )
    return res


# ---------------------------------------------------------------------------
# Git 仓库订阅：先取仓库信息再落订阅，订阅与部署一次完成
# ---------------------------------------------------------------------------

_GITHUB_INSTALL_TYPES = ("source", "zip", "release")


def _handle_github_subscribe_repo(api: Any, **kwargs: Any) -> Any:
    """github_subscribe_repo：先取仓库信息再落订阅（上游 github_subscribe 需要 info 缓存）。

    上游 ``api.github_subscribe(payload)`` 要求 payload 携带 ``info``（在线信息缓存，
    订阅记录与版本决策都用它）——直接委托会逼 agent 手工拼 dict。handler 先
    ``github_fetch_info``（失败即因果错误，不落订阅记录）再组装 payload，
    「订阅 + 触发部署」一次完成；响应即上游 ``github_get_subscribed()`` 全量。
    """
    url = str(kwargs.get("url") or "").strip()
    if not url:
        raise _tool_error(
            "url 不能为空",
            [
                "提供仓库主页链接，如 https://github.com/<owner>/<repo> 或 https://gitgud.io/<owner>/<repo>",
                "不确定地址时先与用户确认仓库来源",
            ],
        )
    install_type = str(kwargs.get("install_type") or "source").strip() or "source"
    if install_type not in _GITHUB_INSTALL_TYPES:
        raise _tool_error(
            f"install_type 仅支持 {'/'.join(_GITHUB_INSTALL_TYPES)}: {install_type!r}",
            _PARAM_ACTIONS,
        )
    source_branch = str(kwargs.get("source_branch") or "").strip()
    version = str(kwargs.get("version") or "").strip()

    info_res = api.github_fetch_info(url, source_branch=source_branch)
    if not isinstance(info_res, dict) or info_res.get("status") != "success":
        message = info_res.get("message") if isinstance(info_res, dict) else ""
        raise _tool_error(
            str(message or "获取 Git 仓库信息失败，订阅未提交"),
            [
                "确认仓库链接可公开访问（私有仓库/需要登录的仓库不在本工具射程）",
                "检查网络连接与代理设置后重试",
                "仓库确已删除/改名时，让用户先在浏览器核实地址",
            ],
        )
    info = info_res.get("data") or {}

    payload = {
        "url": url,
        "install_type": install_type,
        "info": info,
        "default_branch": source_branch or str(info.get("default_branch") or ""),
        "installed_version": version,
    }
    return api.github_subscribe(payload)
