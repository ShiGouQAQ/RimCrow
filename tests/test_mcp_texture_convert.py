# tests/test_mcp_texture_convert.py
"""贴图转换工具层测试：转换任务的提交、轮询与取消。

覆盖三个 handler：

- ``start_texture_convert``：mod_paths 空 / 超上限 50 / 全无效的因果错误、
  backend 与 output_format 值域、scale_factor 数值性、workers 1..12 边界、
  **process_mode 档位映射**（原尺寸 / 缩放 × overwrite 四组合 + 边界档）、
  dry_run 透传与 dry_run 响应形状、api 返回 error → 因果错误、成功形状；
- ``get_texture_convert_tasks``：大载荷投影（剥 ``metrics.summary`` /
  ``metrics.final_mods``）与 failed_items 截 5 + ``failed_items_truncated``；
- ``cancel_texture_convert``：task_id 空 / 任务不存在（``TextureOptError``）→
  因果错误、贴图管理器未初始化、成功形状。

另覆盖两个直接委托工具（``get_texture_encoder_status`` /
``download_texture_encoder``）的绑定可达性。工具总数与快照一致性在
``test_mcp_server.py`` / ``test_tool_cache_generator.py``。
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from backend.mcp_handlers_texture import (
    _handle_cancel_texture_convert,
    _handle_get_texture_convert_tasks,
    _handle_start_texture_convert,
)
from backend.mcp_tool_specs import TOOL_SPECS


def _tool_error_json(exc: Exception) -> dict[str, Any]:
    """ToolError 产物是单个 JSON 字符串（因果错误格式）。"""
    return json.loads(str(exc))


class FakeTextureMgr:
    """最小贴图管理器替身：只实现 handler 真正调到的 ``cancel_task``。"""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.raise_for: dict[str, str] = {}
        self.payloads: dict[str, dict[str, Any]] = {}

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        if task_id in self.raise_for:
            from backend.managers.texture_encoders import TextureOptError

            raise TextureOptError(self.raise_for[task_id])
        self.cancelled.append(task_id)
        return dict(self.payloads.get(task_id) or {"id": task_id, "task_id": task_id, "status": "cancelling"})


class FakeApi:
    """最小后端替身：只实现贴图转换 handler 真正调用到的面。"""

    def __init__(self) -> None:
        self.texture_mgr = FakeTextureMgr()
        self.start_calls: list[dict[str, Any]] = []
        self.tasks_calls = 0
        self.start_result: dict[str, Any] | None = None
        self.tasks_result: dict[str, Any] | None = None

    def texture_start_convert(self, mod_paths, options=None, dry_run=False):
        self.start_calls.append(
            {"mod_paths": list(mod_paths), "options": dict(options or {}), "dry_run": bool(dry_run)}
        )
        if self.start_result is not None:
            return dict(self.start_result)
        return {"status": "success", "message": "贴图优化任务已加入队列", "data": {"task_id": "task-1"}}

    def texture_get_tasks(self):
        self.tasks_calls += 1
        if self.tasks_result is not None:
            return dict(self.tasks_result)
        return {"status": "success", "data": []}


def _paths(count: int) -> list[str]:
    return [f"E:/Mods/Mod{i:03d}" for i in range(count)]


class TestStartTextureConvertValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()

    def _start(self, **kwargs: Any):
        kwargs.setdefault("mod_paths", _paths(1))
        return _handle_start_texture_convert(self.api, **kwargs)

    def test_missing_mod_paths_is_causal(self):
        from fastmcp.exceptions import ToolError

        for bad in ([], None, "", "E:/Mods/One"):
            with self.subTest(mod_paths=bad):
                with self.assertRaises(ToolError) as ctx:
                    _handle_start_texture_convert(self.api, mod_paths=bad)
                self.assertIn("mod_paths", str(ctx.exception))
        self.assertEqual(self.api.start_calls, [])  # 校验在 api 之前

    def test_over_limit_is_causal_with_batching_action(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            self._start(mod_paths=_paths(51))
        parsed = _tool_error_json(ctx.exception)
        self.assertIn("上限 50", parsed["message"])
        self.assertIn("51", parsed["message"])
        self.assertTrue(any("分批" in action for action in parsed["nextActions"]))
        self.assertEqual(self.api.start_calls, [])

    def test_all_blank_paths_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            self._start(mod_paths=["", "   ", None])
        self.assertIn("没有有效路径", str(ctx.exception))
        self.assertEqual(self.api.start_calls, [])

    def test_blank_paths_are_dropped_and_valid_ones_forwarded(self):
        self._start(mod_paths=["  ", "E:/Mods/Kept", "", "E:/Mods/Second"])
        self.assertEqual(self.api.start_calls[0]["mod_paths"], ["E:/Mods/Kept", "E:/Mods/Second"])

    def test_invalid_backend_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            self._start(backend="vulkan")
        self.assertIn("backend", str(ctx.exception))
        self.assertEqual(self.api.start_calls, [])

    def test_invalid_output_format_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            self._start(output_format="ktx2")
        self.assertIn("output_format", str(ctx.exception))
        self.assertEqual(self.api.start_calls, [])

    def test_non_numeric_scale_factor_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            self._start(scale_factor="half")
        self.assertIn("scale_factor", str(ctx.exception))
        self.assertEqual(self.api.start_calls, [])

    def test_worker_bounds_are_enforced(self):
        from fastmcp.exceptions import ToolError

        for key in ("gpu_workers", "cpu_workers"):
            for bad in (0, 13, -1):
                with self.subTest(key=key, value=bad):
                    with self.assertRaises(ToolError) as ctx:
                        self._start(**{key: bad})
                    self.assertIn(key, str(ctx.exception))
            with self.subTest(key=key, value="abc"):
                with self.assertRaises(ToolError) as ctx:
                    self._start(**{key: "abc"})
                self.assertIn(key, str(ctx.exception))
        self._start(gpu_workers=12)  # 上界合法，不抛
        self.assertEqual(len(self.api.start_calls), 1)


    def test_backend_enum_covers_three_tiers_and_auto(self):
        for backend in ("auto", "todds", "nvtt", "texconv"):
            with self.subTest(backend=backend):
                _handle_start_texture_convert(self.api, mod_paths=_paths(1), backend=backend)
                self.assertEqual(self.api.start_calls[-1]["options"]["encoder_backend"], backend)
        self.assertEqual(len(self.api.start_calls), 4)


class TestStartProcessModeMapping(unittest.TestCase):
    """与管理器 scale 档位语义逐字对齐（原尺寸 / 缩放 × overwrite 四组合）。"""

    def setUp(self) -> None:
        self.api = FakeApi()

    def _mode(self, *, scale_factor: float, overwrite: bool) -> str:
        _handle_start_texture_convert(
            self.api, mod_paths=_paths(1), scale_factor=scale_factor, overwrite=overwrite
        )
        return self.api.start_calls[-1]["options"]["process_mode"]

    def test_full_scale_matrix(self):
        self.assertEqual(self._mode(scale_factor=1.0, overwrite=False), "all_skip_existing")
        self.assertEqual(self._mode(scale_factor=1.0, overwrite=True), "all_overwrite")
        self.assertEqual(self._mode(scale_factor=0.5, overwrite=False), "scaled_only_overwrite")
        self.assertEqual(self._mode(scale_factor=0.5, overwrite=True), "all_overwrite")

    def test_zero_and_negative_scale_treated_as_original_size(self):
        # <= 0 或 ≈1.0 都是「不缩放」档（沿用管理器既有档位语义）
        self.assertEqual(self._mode(scale_factor=0, overwrite=False), "all_skip_existing")
        self.assertEqual(self._mode(scale_factor=0, overwrite=True), "all_overwrite")
        self.assertEqual(self._mode(scale_factor=-0.5, overwrite=False), "all_skip_existing")
        self.assertEqual(self._mode(scale_factor=1.0000001, overwrite=False), "all_skip_existing")  # ≈1.0

    def test_options_carry_backend_format_and_workers(self):
        _handle_start_texture_convert(
            self.api,
            mod_paths=_paths(2),
            backend="texconv",
            output_format="zstd",
            generate_mipmaps=False,
            gpu_workers=2,
            cpu_workers=4,
            scale_factor=1.0,
        )
        call = self.api.start_calls[0]
        self.assertEqual(call["mod_paths"], _paths(2))
        self.assertEqual(
            call["options"],
            {
                "encoder_backend": "texconv",
                "output_format": "zstd",
                "generate_mipmaps": False,
                "scale_factor": 1.0,
                "process_mode": "all_skip_existing",
                "gpu_workers": 2,
                "cpu_workers": 4,
            },
        )

    def test_defaults_match_tool_signature(self):
        _handle_start_texture_convert(self.api, mod_paths=_paths(1))
        options = self.api.start_calls[0]["options"]
        self.assertEqual(options["encoder_backend"], "auto")
        self.assertEqual(options["output_format"], "dds")
        self.assertTrue(options["generate_mipmaps"])
        self.assertEqual(options["scale_factor"], 1.0)
        self.assertEqual(options["process_mode"], "all_skip_existing")
        self.assertNotIn("gpu_workers", options)  # 未指定 = 走机器自适应
        self.assertNotIn("cpu_workers", options)


class TestScaleFactorContract(unittest.TestCase):
    """scale_factor 只接受管理器真正支持的档位，杜绝「静默按原尺寸转换」。

    管理器 ``_get_scale_factor_percent`` 对非档位值返回 ``None`` → 条目无
    ``scale_percent`` → 不缩放也报成功；``needs_scaling`` 同时变 False 让 auto 落到
    texconv。校验必须与该函数同源，否则两边会再次分叉。
    """

    def setUp(self) -> None:
        self.api = FakeApi()

    def test_non_step_scale_factor_is_causal(self):
        from fastmcp.exceptions import ToolError

        for bad in (0.9, 0.3, 0.55, 1.5, 0.99, 0.05):
            with self.subTest(scale_factor=bad):
                with self.assertRaises(ToolError) as ctx:
                    _handle_start_texture_convert(self.api, mod_paths=_paths(1), scale_factor=bad)
                parsed = _tool_error_json(ctx.exception)
                self.assertIn("档位", parsed["message"])
                self.assertTrue(any("原尺寸" in action for action in parsed["nextActions"]))
                self.assertTrue(any("75%" in action for action in parsed["nextActions"]))
        self.assertEqual(self.api.start_calls, [])  # 校验在 api 之前

    def test_every_supported_step_is_accepted(self):
        for step in (20, 25, 40, 50, 60, 75, 80):
            with self.subTest(step=step):
                _handle_start_texture_convert(self.api, mod_paths=_paths(1), scale_factor=step / 100)
        self.assertEqual(len(self.api.start_calls), 7)

    def test_validation_stays_in_lockstep_with_manager(self):
        from backend.managers.mgr_texture_opt import TextureOptimizationManager as Manager

        # 放行值必须真能缩放（否则校验不生效）
        for step in (20, 25, 40, 50, 60, 75, 80):
            self.assertEqual(Manager._get_scale_factor_percent({"scale_factor": step / 100}), step)
        # 原尺寸档解析成 None = 不缩放，与 handler 的 process_mode 判定同真
        for value in (1.0, 0, -0.2):
            self.assertIsNone(Manager._get_scale_factor_percent({"scale_factor": value}))
        # 拦掉的值若放行，全都拿不到可执行的缩放档位 —— 既不缩放、任务还报成功，
        # 这就是拦截的理由。分两种走法：非档位值返回 None（needs_scaling 变 False → auto
        # 还会落到不支持缩放的 texconv）；>1 走保留原尺寸分支返回 100（同样不在档位序列里）。
        for value in (0.9, 0.3, 0.55, 0.99):
            self.assertIsNone(Manager._get_scale_factor_percent({"scale_factor": value}))
        self.assertEqual(Manager._get_scale_factor_percent({"scale_factor": 1.5}), 100)
        for value in (0.9, 0.3, 0.55, 1.5, 0.99):
            self.assertEqual(
                Manager._iter_scale_step_candidates({"scale_factor": value}),
                tuple(),
                msg=f"scale_factor={value} 不该解析出可执行缩放档位",
            )


class TestStartTextureConvertOutcome(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()

    def test_dry_run_flag_is_forwarded_and_projection_returned(self):
        self.api.start_result = {
            "status": "success",
            "message": "预演完成（未写盘）",
            "data": {
                "summary": {"mods": 2, "pending": 17, "bytes": 1024},
                "mods": [{"mod_path": "E:/M1", "pending": 9}],
                "backend": {"name": "texconv", "gpu_workers": 3, "cpu_workers": 6},
                "noise": "ignored",
            },
        }
        result = _handle_start_texture_convert(self.api, mod_paths=_paths(2), dry_run=True)
        self.assertTrue(self.api.start_calls[0]["dry_run"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["summary"], {"mods": 2, "pending": 17, "bytes": 1024})
        self.assertEqual(result["mods"], [{"mod_path": "E:/M1", "pending": 9}])
        self.assertEqual(result["backend"]["name"], "texconv")
        self.assertNotIn("noise", result)  # 只透传三个投影字段

    def test_dry_run_defaults_to_false(self):
        _handle_start_texture_convert(self.api, mod_paths=_paths(1))
        self.assertFalse(self.api.start_calls[0]["dry_run"])

    def test_success_returns_task_id_backend_and_hint(self):
        result = _handle_start_texture_convert(self.api, mod_paths=_paths(1), backend="texconv")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["backend"], "texconv")
        self.assertEqual(result["message"], "贴图优化任务已加入队列")
        self.assertIn("get_texture_convert_tasks", result["hint"])
        self.assertIn("cancel_texture_convert", result["hint"])

    def test_task_id_falls_back_to_id_field(self):
        self.api.start_result = {"status": "success", "message": "ok", "data": {"id": "legacy-7"}}
        result = _handle_start_texture_convert(self.api, mod_paths=_paths(1))
        self.assertEqual(result["task_id"], "legacy-7")

    def test_api_error_maps_to_causal_error(self):
        from fastmcp.exceptions import ToolError

        self.api.start_result = {"status": "error", "message": "启动贴图任务失败"}
        with self.assertRaises(ToolError) as ctx:
            _handle_start_texture_convert(self.api, mod_paths=_paths(1))
        parsed = _tool_error_json(ctx.exception)
        self.assertIn("启动贴图任务失败", parsed["message"])
        self.assertTrue(any("get_texture_encoder_status" in action for action in parsed["nextActions"]))
        self.assertTrue(any("dry_run" in action for action in parsed["nextActions"]))

    def test_missing_message_degrades_to_default_cause(self):
        from fastmcp.exceptions import ToolError

        self.api.start_result = {"status": "error"}
        with self.assertRaises(ToolError) as ctx:
            _handle_start_texture_convert(self.api, mod_paths=_paths(1))
        self.assertIn("贴图转换启动失败", str(ctx.exception))


class TestGetTextureConvertTasks(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()

    @staticmethod
    def _task(task_id: str = "task-1", **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": task_id,
            "action": "optimize",
            "status": "running",
            "progress": 42,
            "message": "正在转换",
            "user_message": "",
            "updated_at": 1759000000000,
            "summary": {"optimized": 3, "skipped": 1, "failed": 0},
            "metrics": {
                "summary": {"total": 4},
                "final_mods": [{"mod_path": "E:/M1", "count": 9}],
                "processed": 3,
                "total": 4,
            },
            "error": "",
        }
        payload.update(overrides)
        return payload

    def test_strips_big_payload_metrics_but_keeps_counters(self):
        self.api.tasks_result = {"status": "success", "data": [self._task()]}
        result = _handle_get_texture_convert_tasks(self.api)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 1)
        metrics = result["tasks"][0]["metrics"]
        self.assertNotIn("summary", metrics)  # 剥 metrics 聚合
        self.assertNotIn("final_mods", metrics)  # 剥逐模组明细
        self.assertEqual(metrics, {"processed": 3, "total": 4})
        self.assertEqual(result["tasks"][0]["summary"], {"optimized": 3, "skipped": 1, "failed": 0})  # 顶层 summary 保留
        self.assertIn("120 秒", result["hint"])
        self.assertIn("summary", result["hint"])

    def test_failed_items_truncated_to_five_with_marker(self):
        task = self._task(
            metrics={
                "summary": {"total": 9},
                "failed_items": [{"rel_path": f"Textures/f{i}.png", "error": "boom"} for i in range(9)],
                "processed": 8,
            }
        )
        self.api.tasks_result = {"status": "success", "data": [task]}
        metrics = _handle_get_texture_convert_tasks(self.api)["tasks"][0]["metrics"]
        self.assertEqual(len(metrics["failed_items"]), 5)
        self.assertEqual(metrics["failed_items"][0]["rel_path"], "Textures/f0.png")
        self.assertEqual(metrics["failed_items_truncated"], 9)
        self.assertEqual(metrics["processed"], 8)

    def test_failed_items_within_limit_are_not_marked_truncated(self):
        task = self._task(metrics={"failed_items": [{"rel_path": f"t{i}.png"} for i in range(5)]})
        self.api.tasks_result = {"status": "success", "data": [task]}
        metrics = _handle_get_texture_convert_tasks(self.api)["tasks"][0]["metrics"]
        self.assertEqual(len(metrics["failed_items"]), 5)
        self.assertNotIn("failed_items_truncated", metrics)

    def test_empty_and_absent_failed_items_pass_through(self):
        self.api.tasks_result = {"status": "success", "data": [self._task(metrics={}), self._task(metrics={"failed_items": []})]}
        result = _handle_get_texture_convert_tasks(self.api)
        self.assertEqual(result["count"], 2)
        metrics = [task["metrics"] for task in result["tasks"]]
        self.assertEqual(metrics[0], {})  # 无 metrics = 空投影
        self.assertEqual(metrics[1], {"failed_items": []})  # 空清单原样，不做截断标记
        for projected in metrics:
            self.assertNotIn("failed_items_truncated", projected)
            self.assertNotIn("summary", projected)
            self.assertNotIn("final_mods", projected)

    def test_task_id_falls_back_to_id_and_missing_fields_default(self):
        self.api.tasks_result = {
            "status": "success",
            "data": [{"id": "legacy-9", "action": "optimize", "status": "success"}],
        }
        task = _handle_get_texture_convert_tasks(self.api)["tasks"][0]
        self.assertEqual(task["task_id"], "legacy-9")
        self.assertEqual(task["user_message"], "")
        self.assertEqual(task["summary"], {})
        self.assertEqual(task["error"], "")
        self.assertIsNone(task["progress"])

    def test_empty_task_list_returns_zero_count(self):
        result = _handle_get_texture_convert_tasks(self.api)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["tasks"], [])
        self.assertEqual(self.api.tasks_calls, 1)

    def test_api_error_maps_to_causal_error(self):
        from fastmcp.exceptions import ToolError

        self.api.tasks_result = {"status": "error", "message": "读取贴图任务状态失败"}
        with self.assertRaises(ToolError) as ctx:
            _handle_get_texture_convert_tasks(self.api)
        parsed = _tool_error_json(ctx.exception)
        self.assertIn("读取贴图任务状态失败", parsed["message"])
        self.assertTrue(any("read_logs" in action for action in parsed["nextActions"]))


class TestCancelTextureConvert(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()

    def test_empty_task_id_is_causal(self):
        from fastmcp.exceptions import ToolError

        for bad in ("", None, "   "):
            with self.subTest(task_id=bad):
                with self.assertRaises(ToolError) as ctx:
                    _handle_cancel_texture_convert(self.api, task_id=bad)
                self.assertIn("task_id", str(ctx.exception))
        self.assertEqual(self.api.texture_mgr.cancelled, [])

    def test_unknown_task_is_causal(self):
        from fastmcp.exceptions import ToolError

        self.api.texture_mgr.raise_for["nope"] = "未找到对应的贴图优化任务"
        with self.assertRaises(ToolError) as ctx:
            _handle_cancel_texture_convert(self.api, task_id="nope")
        parsed = _tool_error_json(ctx.exception)
        self.assertIn("未找到对应的贴图优化任务", parsed["message"])
        self.assertTrue(any("get_texture_convert_tasks" in action for action in parsed["nextActions"]))

    def test_missing_manager_is_causal(self):
        from fastmcp.exceptions import ToolError

        self.api.texture_mgr = None
        with self.assertRaises(ToolError) as ctx:
            _handle_cancel_texture_convert(self.api, task_id="task-1")
        self.assertIn("未初始化", str(ctx.exception))

    def test_success_returns_task_status_and_cooperative_hint(self):
        self.api.texture_mgr.payloads["task-1"] = {"id": "task-1", "task_id": "task-1", "status": "cancelling"}
        result = _handle_cancel_texture_convert(self.api, task_id="task-1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["task_status"], "cancelling")
        self.assertIn("协作式", result["message"])
        self.assertEqual(self.api.texture_mgr.cancelled, ["task-1"])


class TestDelegateBindings(unittest.TestCase):
    """贴图转换两个直接委托 + 三个 handler 覆盖的绑定一致性（防两处写法不一致）。"""

    def test_delegate_tools_bind_to_api_methods(self):
        methods = {spec.name: spec.method for spec in TOOL_SPECS if spec.method is not None}
        self.assertEqual(methods["get_texture_encoder_status"], "texture_get_encoder_status")
        self.assertEqual(methods["download_texture_encoder"], "texture_prepare_encoder_download")

    def test_handler_tools_are_not_double_registered(self):
        methods = {spec.name: spec.method for spec in TOOL_SPECS}
        from backend.mcp_server import _HANDLER_OVERRIDES

        for name in ("start_texture_convert", "get_texture_convert_tasks", "cancel_texture_convert"):
            self.assertIsNone(methods.get(name), f"{name} 应走 handler 覆盖而非直接委托 API")
            self.assertIn(name, _HANDLER_OVERRIDES)

    def test_validate_bindings_green_against_real_api_class(self):
        """validate_bindings 第 3 条（委托可达）在真实 API 上必须全绿。

        传**类**而非实例：所有 ``_API_METHODS`` 目标都是 ``API`` 上的普通方法，
        无需初始化后端即可校验映射是否漂移。
        """
        from backend.api import API
        from backend.mcp_server import validate_bindings

        validate_bindings(API)
        self.assertTrue(callable(API.texture_get_encoder_status))
        self.assertTrue(callable(API.texture_prepare_encoder_download))


if __name__ == "__main__":
    unittest.main()
