# backend/mcp_handlers_texture.py
"""贴图转换功能 handler：贴图转换全流程（启动 / 查询 / 取消）。

与 ``mcp_handlers_features`` 同责（多步业务 handler：参数校验 / 因果错误 / 大载荷
投影），本模块只服务贴图转换域：

- ``start_texture_convert``：mod_paths 校验（上限 50）→ 缩放档位 → process_mode
  映射（原尺寸档 overwrite=false 走增量 all_skip_existing；缩放档沿用 GUI 的
  scaled_only_overwrite 语义）→ ``api.texture_start_convert``（dry_run 同步预演 /
  异步任务派发）；
- ``get_texture_convert_tasks``：任务快照大载荷投影——剥 ``metrics.final_mods`` /
  ``metrics.summary``（与 list_profiles 的大载荷投影同一方式），失败清单截前 5 条；
- ``cancel_texture_convert``：直达 ``texture_mgr.cancel_task``，不存在的任务给因果错误。

``get_texture_encoder_status`` / ``download_texture_encoder`` 是直接委托
（api 方法即最终形状），登记在 ``mcp_tool_specs._API_METHODS``，不经本模块。

依赖：``backend.mcp_errors`` + 函数体内 lazy import（``backend.managers.
texture_encoders`` 的异常类型）；**不 import ``mcp_server``**（单向依赖防循环）。
写路径原子性由 ``mcp_server`` 工具层全局调用锁保证；``start_texture_convert`` 在
``_NO_LOCK_TOOLS``（dry_run 是同步扫描可长等待，正式启动仅派发后台线程）。
"""

from __future__ import annotations

from typing import Any

from backend.mcp_errors import _PARAM_ACTIONS, _tool_error

__all__ = [
    "_handle_cancel_texture_convert",
    "_handle_get_texture_convert_tasks",
    "_handle_start_texture_convert",
]

_TEXTURE_CONVERT_PATHS_MAX = 50
_TEXTURE_FAILED_ITEMS_MAX = 5
_TEXTURE_BACKENDS = ("auto", "todds", "nvtt", "texconv")  # 展示顺序：标准/中等/快速
_TEXTURE_OUTPUT_FORMATS = ("dds", "zstd")
# 大载荷投影时从 metrics 剥离的字段：final_mods 是逐模组全量统计、summary 是其聚合，
# 两者都会把轮询响应撑到大载荷档；最终统计由顶层 summary 承载。
_TEXTURE_TASK_METRICS_STRIPPED = ("summary", "final_mods")


def _texture_worker_bounds() -> tuple[int, int]:
    """worker 上下限唯一定义在 ``texture_encoders``（lazy import：重模块不进本模块 import 期）。"""
    from backend.managers.texture_encoders import MAX_TEXTURE_WORKERS, MIN_TEXTURE_WORKERS

    return MIN_TEXTURE_WORKERS, MAX_TEXTURE_WORKERS


def _scale_step_percents() -> tuple[int, ...]:
    """缩放档位唯一定义在管理器（与 GUI 下拉框同源）。"""
    from backend.managers.mgr_texture_opt import SCALE_STEP_SEQUENCE

    return tuple(int(step) for step in SCALE_STEP_SEQUENCE)


def _validate_scale_factor(scale_factor: float) -> None:
    """非档位 ``scale_factor`` 必须在派发前拒绝，否则会**静默按原尺寸转换**。

    管理器 ``_get_scale_factor_percent`` 对不在 ``SCALE_STEP_SEQUENCE`` 的值返回 ``None``
    → 条目 ``scale_percent`` 为空 → 实测 0.9 / 0.3 / 0.55 全部既不缩放也不报错；
    同时 ``needs_scaling`` 为 False 让 auto 落到 texconv（不支持缩放），与工具 desc 的
    「scale_factor<1 auto 落 todds」直接矛盾——工具层必须在派发前把好这道校验。
    """
    if scale_factor <= 0 or abs(scale_factor - 1.0) <= 1e-6:
        return  # 原尺寸档（<=0 与 ≈1.0 都是不缩放，与 process_mode 映射同源）
    steps = _scale_step_percents()
    if int(round(scale_factor * 100)) in steps:
        return
    allowed = "/".join(f"{step}%" for step in steps)
    raise _tool_error(
        f"scale_factor={scale_factor} 不在支持的缩放档位上（{allowed}；1.0=原尺寸）："
        "非档位值会被按原尺寸静默转换",
        ["用 scale_factor=1.0 做原尺寸转换", f"或选一个档位: {allowed}"],
    )


def _handle_start_texture_convert(api: Any, **kwargs: Any) -> Any:
    raw_paths = kwargs.get("mod_paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise _tool_error(
            "mod_paths 不能为空（模组根目录绝对路径列表，取 list_mods 条目的 path 字段）",
            _PARAM_ACTIONS,
        )
    if len(raw_paths) > _TEXTURE_CONVERT_PATHS_MAX:
        raise _tool_error(
            f"mod_paths 上限 {_TEXTURE_CONVERT_PATHS_MAX} 个（当前 {len(raw_paths)}）",
            ["分批转换"],
        )
    mod_paths = [str(path or "").strip() for path in raw_paths if str(path or "").strip()]
    if not mod_paths:
        raise _tool_error("mod_paths 没有有效路径", _PARAM_ACTIONS)

    backend = str(kwargs.get("backend") or "auto").strip().lower()
    if backend not in _TEXTURE_BACKENDS:
        raise _tool_error(
            f"backend 仅支持 {'/'.join(_TEXTURE_BACKENDS)}: {backend!r}",
            _PARAM_ACTIONS,
        )
    output_format = str(kwargs.get("output_format") or "dds").strip().lower()
    if output_format not in _TEXTURE_OUTPUT_FORMATS:
        raise _tool_error(
            f"output_format 仅支持 {'/'.join(_TEXTURE_OUTPUT_FORMATS)}: {output_format!r}",
            _PARAM_ACTIONS,
        )
    try:
        scale_factor = float(kwargs.get("scale_factor", 1.0))
    except (TypeError, ValueError):
        raise _tool_error("scale_factor 必须是数字（1.0=原尺寸，或 GUI 缩放档位）", _PARAM_ACTIONS) from None
    _validate_scale_factor(scale_factor)
    overwrite = bool(kwargs.get("overwrite", False))
    worker_min, worker_max = _texture_worker_bounds()
    workers: dict[str, int] = {}
    for key in ("gpu_workers", "cpu_workers"):
        value = kwargs.get(key)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise _tool_error(f"{key} 必须是 {worker_min}..{worker_max} 的整数", _PARAM_ACTIONS) from None
        if not worker_min <= number <= worker_max:
            raise _tool_error(f"{key} 取值范围 {worker_min}..{worker_max}: {number}", _PARAM_ACTIONS)
        workers[key] = number
    dry_run = bool(kwargs.get("dry_run"))

    options: dict[str, Any] = {
        "encoder_backend": backend,
        "output_format": output_format,
        "generate_mipmaps": bool(kwargs.get("generate_mipmaps", True)),
        "scale_factor": scale_factor,
        # 档位已在 _validate_scale_factor 卡死 → 这里的「是否缩放」与管理器
        # _get_scale_factor_percent 的判定必然同真：
        # 原尺寸档 overwrite=false → 增量补缺，true → 全量重编；
        # 缩放档沿用 GUI 默认 scaled_only_overwrite（缩放计划内的条目重编）。
        "process_mode": (
            ("all_overwrite" if overwrite else "all_skip_existing")
            if (scale_factor <= 0 or abs(scale_factor - 1.0) <= 1e-6)
            else ("all_overwrite" if overwrite else "scaled_only_overwrite")
        ),
        **workers,
    }

    res = api.texture_start_convert(mod_paths, options, dry_run=dry_run)
    if res.get("status") != "success":
        raise _tool_error(
            str(res.get("message") or "贴图转换启动失败"),
            [
                "先调 get_texture_encoder_status 确认编码器可用（缺 bin 用 download_texture_encoder）",
                "用 dry_run:true 预演看待转换量与将选用的 backend",
            ],
        )
    data = res.get("data") or {}
    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "summary": data.get("summary") or {},
            "mods": data.get("mods") or [],
            "backend": data.get("backend") or {},
        }
    return {
        "status": "success",
        "task_id": str(data.get("task_id") or data.get("id") or ""),
        "message": str(res.get("message") or ""),
        "backend": backend,
        "hint": "用 get_texture_convert_tasks 轮询进度；需要中止用 cancel_texture_convert",
    }


def _handle_get_texture_convert_tasks(api: Any, **kwargs: Any) -> Any:
    res = api.texture_get_tasks()
    if res.get("status") != "success":
        raise _tool_error(
            str(res.get("message") or "读取贴图任务状态失败"),
            ["稍后重试", "查看 list_log_files → read_logs 中的后端错误"],
        )
    tasks: list[dict[str, Any]] = []
    for payload in res.get("data") or []:
        metrics = dict(payload.get("metrics") or {})
        projected_metrics = {key: value for key, value in metrics.items() if key not in _TEXTURE_TASK_METRICS_STRIPPED}
        failed_items = metrics.get("failed_items")
        if isinstance(failed_items, list) and failed_items:
            projected_metrics["failed_items"] = failed_items[:_TEXTURE_FAILED_ITEMS_MAX]
            if len(failed_items) > _TEXTURE_FAILED_ITEMS_MAX:
                projected_metrics["failed_items_truncated"] = len(failed_items)
        tasks.append(
            {
                "task_id": str(payload.get("task_id") or payload.get("id") or ""),
                "action": payload.get("action"),
                "status": payload.get("status"),
                "progress": payload.get("progress"),
                "message": payload.get("message"),
                "user_message": payload.get("user_message") or "",
                "updated_at": payload.get("updated_at"),
                "summary": payload.get("summary") or {},
                "metrics": projected_metrics,
                "error": payload.get("error") or "",
            }
        )
    return {
        "status": "success",
        "count": len(tasks),
        "tasks": tasks,
        "hint": "任务结束（success/failed/cancelled）后保留 120 秒可查；最终统计看 summary",
    }


def _handle_cancel_texture_convert(api: Any, **kwargs: Any) -> Any:
    task_id = str(kwargs.get("task_id") or "").strip()
    if not task_id:
        raise _tool_error("task_id 不能为空（来自 start_texture_convert 响应）", _PARAM_ACTIONS)
    mgr = getattr(api, "texture_mgr", None)
    if mgr is None:
        raise _tool_error(
            "贴图管理器未初始化",
            ["确认 RimCrow 已完全启动", "稍后重试"],
        )
    from backend.managers.texture_encoders import TextureOptError

    try:
        payload = mgr.cancel_task(task_id)
    except TextureOptError as exc:
        raise _tool_error(
            str(exc),
            ["用 get_texture_convert_tasks 查看现存任务后重试"],
        ) from exc
    return {
        "status": "success",
        "task_id": task_id,
        "task_status": payload.get("status"),
        "message": "已请求取消（协作式：进行中的编码子进程会在数秒内退出）",
    }
