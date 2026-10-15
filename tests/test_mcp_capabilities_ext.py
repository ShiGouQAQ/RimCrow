# tests/test_mcp_capabilities_ext.py
"""排障 / 整理 / 获取三组工具的工具层测试。

覆盖：export_load_order（双形态 / 路径必须显式传入 / 因果错误）、parse_load_order
（双来源 / dialog 阻塞防御 / import_check 透传 / 空 warning 降级）、
smart_insert_mods（token 保护 / dry_run / 锚点 diff / auto_activated）、
resolve_missing_dependencies（缺省自读激活列表）、launch_game（两阶段确认）、
get_game_status、search_mod_text（事件聚合 / 截断 / failed→因果错误）、
cancel_task 类型白名单、read_mod_file 模组根边界、
get_workshop_mod_details 批量上限、github_subscribe_repo（info 组装 / fetch 失败
不落订阅 / 因果边界）、委托绑定可达性。工具总数与快照一致性在
test_mcp_server.py 与 test_tool_cache_generator.py。
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.mcp_adapters_query import (
    _handle_get_game_status,
    _handle_parse_load_order,
)
from backend.mcp_handlers_features import (
    _CANCEL_TASK_TYPES,
    _GITHUB_INSTALL_TYPES,
    _handle_cancel_task,
    _handle_export_load_order,
    _handle_get_workshop_mod_details,
    _handle_github_subscribe_repo,
    _handle_launch_game,
    _handle_read_mod_file,
    _handle_resolve_missing_dependencies,
    _handle_smart_insert_mods,
)
from backend.mcp_server import (
    _handle_search_mod_text,
    install_event_tap,
    restore_event_tap,
)
from backend.mcp_tool_specs import TOOL_SPECS


class FakeLoadOrderManager:
    def __init__(self, ids: list[str] | None = None, stale: bool = False) -> None:
        self.ids = list(ids or ["ludeon.rimworld", "brrainz.harmony"])
        self.stale = stale
        self.saved: list[tuple[list[str], dict | None]] = []

    def is_version_token_stale(self, token):
        return self.stale, {"path": "x", "mtime_ms": 1, "size": 2, "active_hash": "h"}

    def read_active_mods(self, path=None):
        return {"active_mods": list(self.ids)}


class FakeApi:
    """最小后端替身：只实现被测 handler 真正调用到的面。"""

    def __init__(self) -> None:
        self.active_context = SimpleNamespace(profile_id="test")
        self.load_order_mgr = FakeLoadOrderManager()
        self.share_export_calls: list[tuple[list[str], str | None]] = []
        self.export_calls: list[dict] = []
        self.smart_insert_calls: list[tuple[list[str], list[str]]] = []
        self.resolve_calls: list[list[str]] = []
        self.launch_calls: list[str] = []
        self.confirm_calls: list[tuple[str, str]] = []

    # ---- 整理类工具（load order 导出/导入/插入）----
    def load_order_share_export(self, active_ids, list_name=None):
        self.share_export_calls.append((list(active_ids), list_name))
        return {
            "status": "success",
            "data": {"share_code": "RC:x", "format": "share_code", "count": len(active_ids)},
        }

    def load_order_export(self, active_ids, target_path=None, trigger_dialog=True, export_format="modsconfig", list_name=None, remember_dialog_dir=False, use_raw_package_ids=None):
        self.export_calls.append(
            {
                "ids": list(active_ids),
                "path": target_path,
                "trigger_dialog": trigger_dialog,
                "format": export_format,
            }
        )
        return {"status": "success"}

    def smart_insert_mod_in_actives(self, package_ids, current_active_ids):
        self.smart_insert_calls.append((list(package_ids), list(current_active_ids)))
        # 引擎行为模拟：目标插到最前，另有一个联锁项被自动激活
        final = ["zz.interlock.auto", *package_ids, *current_active_ids]
        return {"status": "success", "data": final}

    def load_order_save_with_token(self, active_ids, base_version_token=None, **kwargs):
        self.load_order_mgr.saved.append((list(active_ids), base_version_token))
        return {
            "status": "success",
            "data": {"version_token": {"active_hash": "new"}, "modify_time": 2},
        }

    def lifecycle_resolve_dependencies(self, active_package_ids):
        self.resolve_calls.append(list(active_package_ids))
        return {
            "status": "success",
            "data": {"missing": [{"workshop_id": "3011", "name": "Harmony", "preview_url": ""}]},
        }

    # ---- 排障类工具（游戏状态 / 日志检索）----
    def game_launch(self, profile_id):
        self.launch_calls.append(str(profile_id or ""))
        return self.launch_result

    def game_launch_resolve_warning(self, profile_id, action):
        self.confirm_calls.append((str(profile_id), str(action)))
        return self.confirm_result


def _ok(data=None, message=""):
    return {"status": "success", "data": data if data is not None else {}, "message": message}


class TestExportLoadOrder(unittest.TestCase):
    """export_load_order：路径必须显式传入 + 分享码内联 + 因果错误。"""

    def setUp(self):
        self.api = FakeApi()

    def test_share_code_inlines_and_passes_active_ids(self):
        result = _handle_export_load_order(self.api, export_format="share_code")
        self.assertEqual(result["data"]["share_code"], "RC:x")
        ids, list_name = self.api.share_export_calls[0]
        self.assertEqual(ids, ["ludeon.rimworld", "brrainz.harmony"])
        self.assertIsNone(list_name)

    def test_share_code_rejects_path(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_export_load_order(self.api, export_format="share_code", path="C:/x.xml")
        self.assertIn("path 必须为空", str(ctx.exception))

    def test_unknown_format_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_export_load_order(self.api, export_format="xlsx")
        self.assertIn("export_format", str(ctx.exception))

    def test_file_export_requires_path(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_export_load_order(self.api, export_format="modlist")
        parsed = json.loads(str(ctx.exception))
        self.assertIn("path", parsed["message"])
        self.assertTrue(any("share_code" in a for a in parsed["nextActions"]))

    def test_file_export_rejects_directory(self):
        from fastmcp.exceptions import ToolError

        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(ToolError) as ctx:
                _handle_export_load_order(
                    self.api, export_format="modsconfig", path=tmp_dir
                )
        self.assertIn("目录", str(ctx.exception))
        self.assertEqual(self.api.export_calls, [])

    def test_file_export_success_forces_no_dialog(self):
        result = _handle_export_load_order(
            self.api, export_format="modsconfig", path="E:/out/ModsConfig.xml", list_name="我的列表"
        )
        self.assertEqual(result["path"], "E:/out/ModsConfig.xml")
        self.assertEqual(result["count"], 2)
        call = self.api.export_calls[0]
        self.assertFalse(call["trigger_dialog"])  # 验证：绝不弹对话框
        self.assertEqual(call["format"], "modsconfig")

    def test_empty_actives_is_causal(self):
        from fastmcp.exceptions import ToolError

        self.api.load_order_mgr.ids = []
        with self.assertRaises(ToolError) as ctx:
            _handle_export_load_order(self.api, export_format="share_code")
        self.assertIn("激活列表为空", str(ctx.exception))

    def test_missing_manager_is_causal(self):
        from fastmcp.exceptions import ToolError

        self.api.load_order_mgr = None
        with self.assertRaises(ToolError) as ctx:
            _handle_export_load_order(self.api, export_format="share_code")
        self.assertIn("未初始化", str(ctx.exception))


class TestParseLoadOrder(unittest.TestCase):
    """parse_load_order：零写入解析 + dialog 阻塞防御。"""

    def setUp(self):
        self.api = FakeApi()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 存在性校验是真实行为（防 dialog 阻塞），用例必须给真实文件
        self.modlist_path = os.path.join(self._tmp.name, "modlist.xml")
        self.empty_path = os.path.join(self._tmp.name, "empty.xml")
        self.bad_path = os.path.join(self._tmp.name, "bad.xml")
        for p in (self.modlist_path, self.empty_path, self.bad_path):
            with open(p, "w", encoding="utf-8") as handle:
                handle.write("<modList />")

    def _file_payload(self, *, status="success", ids=None):
        ids = ["a.mod", "b.mod"] if ids is None else ids
        return {
            "status": status,
            "message": "文件已读取，但没有识别到可导入的排序列表。",
            "data": {
                "file": "E:/share/modlist.xml",
                "active_ids": ids,
                "format": "modlist",
                "list_name": "分享列表",
                "warnings": ["w1"],
                "errors": [],
                "import_check": {"summary": {"missing": 1}, "items": [{"package_id": "x"}]},
            },
        }

    def test_requires_one_source(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_parse_load_order(self.api)
        self.assertIn("至少给一个", str(ctx.exception))

    def test_nonexistent_path_rejected_before_dialog(self):
        from fastmcp.exceptions import ToolError

        missing = os.path.join(self._tmp.name, "no_such_file.xml")
        with self.assertRaises(ToolError) as ctx:
            _handle_parse_load_order(self.api, path=missing)
        self.assertIn("不是已存在的文件", str(ctx.exception))

    def test_file_parse_passthrough(self):
        self.api.load_order_file_open = lambda p: self._file_payload()
        result = _handle_parse_load_order(self.api, path=self.modlist_path)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source"], "path")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["import_check"]["summary"], {"missing": 1})
        self.assertEqual(result["list_name"], "分享列表")

    def test_share_parse_passthrough(self):
        captured = {}

        def fake_import(code):
            captured["code"] = code
            payload = self._file_payload()
            payload["data"]["share_code_ref"] = "share://RC"
            payload["data"]["format"] = "share_code"
            return payload

        self.api.load_order_share_import = fake_import
        result = _handle_parse_load_order(self.api, share_code="RC:abc")
        self.assertEqual(captured["code"], "RC:abc")
        self.assertEqual(result["source"], "share_code")
        self.assertEqual(result["share_code_ref"], "share://RC")
        self.assertNotIn("file", result)

    def test_empty_parse_warning_degrades_into_warning_field(self):
        self.api.load_order_file_open = lambda p: self._file_payload(status="warning", ids=[])
        result = _handle_parse_load_order(self.api, path=self.empty_path)
        self.assertEqual(result["status"], "success")  # warning 不再是 isError
        self.assertIn("没有识别到", result["warning"])
        self.assertEqual(result["total"], 0)

    def test_error_payload_returns_raw_for_pipeline(self):
        self.api.load_order_file_open = lambda p: {"status": "error", "message": "解析文件出错。"}
        result = _handle_parse_load_order(self.api, path=self.bad_path)
        self.assertEqual(result["status"], "error")  # 管线负责转成统一错误格式


class TestSmartInsertMods(unittest.TestCase):
    """smart_insert_mods：token 保护 + dry_run + 锚点 diff + auto_activated。"""

    TOKEN = {"path": "/tmp/ModsConfig.xml", "mtime_ms": 1, "size": 2, "active_hash": "h"}

    def setUp(self):
        self.api = FakeApi()

    def _call(self, **kwargs):
        kwargs.setdefault("package_ids", ["new.mod.a"])
        kwargs.setdefault("base_version_token", dict(self.TOKEN))
        return _handle_smart_insert_mods(self.api, **kwargs)

    def test_missing_token_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_smart_insert_mods(self.api, package_ids=["new.mod.a"])
        self.assertIn("base_version_token", str(ctx.exception))

    def test_stale_token_rejected_without_write(self):
        from fastmcp.exceptions import ToolError

        self.api.load_order_mgr.stale = True
        with self.assertRaises(ToolError) as ctx:
            self._call()
        parsed = json.loads(str(ctx.exception))
        self.assertIn("已被外部修改", parsed["message"])
        self.assertEqual(self.api.load_order_mgr.saved, [])

    def test_empty_and_over_limit_ids_rejected(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError):
            self._call(package_ids=[])
        with self.assertRaises(ToolError):
            self._call(package_ids=[f"m{i}" for i in range(101)])

    def test_dry_run_reports_anchors_without_write(self):
        result = self._call(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["summary"]["inserted"], 2)  # 目标 + 联锁
        self.assertEqual(result["auto_activated"], ["zz.interlock.auto"])
        anchor = result["applied"][0]
        self.assertEqual(anchor["index"], 0)
        self.assertIsNone(anchor["after"])
        self.assertEqual(self.api.load_order_mgr.saved, [])

    def test_success_saves_final_order_and_renews_token(self):
        result = self._call()
        self.assertEqual(result["new_version_token"], {"active_hash": "new"})
        saved_ids, saved_token = self.api.load_order_mgr.saved[0]
        self.assertEqual(saved_ids[0], "zz.interlock.auto")
        self.assertIn("new.mod.a", saved_ids)
        self.assertEqual(saved_token, self.TOKEN)
        # 引擎收到的当前列表与服务端自读一致
        _, current_ids = self.api.smart_insert_calls[0]
        self.assertEqual(current_ids, ["ludeon.rimworld", "brrainz.harmony"])

    def test_upstream_error_maps_to_causal(self):
        from fastmcp.exceptions import ToolError

        self.api.smart_insert_mod_in_actives = lambda *a, **k: {
            "status": "error", "message": "智能插入失败"
        }
        with self.assertRaises(ToolError) as ctx:
            self._call()
        self.assertIn("智能插入失败", str(ctx.exception))


class TestResolveMissingDependencies(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()

    def test_defaults_to_current_actives(self):
        result = _handle_resolve_missing_dependencies(self.api)
        self.assertEqual(self.api.resolve_calls[0], ["ludeon.rimworld", "brrainz.harmony"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["missing"][0]["workshop_id"], "3011")
        self.assertIn("download_workshop_mods", result["hint"])

    def test_empty_actives_without_ids_is_causal(self):
        from fastmcp.exceptions import ToolError

        self.api.load_order_mgr.ids = []
        with self.assertRaises(ToolError) as ctx:
            _handle_resolve_missing_dependencies(self.api)
        self.assertIn("激活列表为空", str(ctx.exception))

    def test_explicit_ids_passthrough(self):
        result = _handle_resolve_missing_dependencies(self.api, package_ids=["x.mod"])
        self.assertEqual(self.api.resolve_calls[0], ["x.mod"])
        self.assertEqual(result["count"], 1)

    def test_complete_deps_message(self):
        self.api.lifecycle_resolve_dependencies = lambda ids: {"status": "success", "data": {"missing": []}}
        result = _handle_resolve_missing_dependencies(self.api, package_ids=["a"])
        self.assertEqual(result["missing"], [])
        self.assertIn("完整", result["message"])

    def test_upstream_error_maps_to_causal(self):
        from fastmcp.exceptions import ToolError

        self.api.lifecycle_resolve_dependencies = lambda ids: {"status": "error", "message": "检测失败"}
        with self.assertRaises(ToolError):
            _handle_resolve_missing_dependencies(self.api, package_ids=["a"])


class TestLaunchGame(unittest.TestCase):
    """launch_game：两阶段启动确认。"""

    def setUp(self):
        self.api = FakeApi()
        self.api.launch_result = _ok({"runtime_session": {"id": "s1"}}, "已发起游戏启动")
        self.api.confirm_result = _ok({"runtime_session": {"id": "s2"}}, "已发起游戏启动")

    def test_success_returns_session_and_hint(self):
        result = _handle_launch_game(self.api, profile_id="p1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["runtime_session"], {"id": "s1"})
        self.assertIn("get_game_status", result["hint"])
        self.assertEqual(self.api.launch_calls, ["p1"])

    def test_confirmation_warning_becomes_causal_error(self):
        from fastmcp.exceptions import ToolError

        self.api.launch_result = {
            "status": "warning",
            "message": "检测到 Steam 已在运行，需要先确认工坊链接冲突后再继续启动。",
            "data": {
                "action": "confirm_direct_launch",
                "profile_id": "p1",
                "steam_running": True,
                "reason": "steam_running_workshop_conflict",
            },
        }
        with self.assertRaises(ToolError) as ctx:
            _handle_launch_game(self.api, profile_id="p1")
        parsed = json.loads(str(ctx.exception))
        self.assertTrue(any("confirm_action" in a for a in parsed["nextActions"]))
        self.assertEqual(parsed["reason"], "steam_running_workshop_conflict")
        self.assertEqual(parsed["profile_id"], "p1")

    def test_confirm_rejects_invalid_action(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_launch_game(self.api, profile_id="p1", confirm_action="yolo")
        self.assertIn("confirm_action", str(ctx.exception))

    def test_confirm_requires_profile_id(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_launch_game(self.api, confirm_action="continue")
        self.assertIn("profile_id", str(ctx.exception))

    def test_confirm_cancel_is_not_an_error(self):
        self.api.confirm_result = {"status": "warning", "message": "已取消启动"}
        result = _handle_launch_game(self.api, profile_id="p1", confirm_action="cancel")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.api.confirm_calls, [("p1", "cancel")])

    def test_confirm_wait_steam_exit_is_waiting(self):
        self.api.confirm_result = {
            "status": "warning",
            "message": "Steam 仍在运行，继续等待其退出。",
            "data": {"action": "wait_steam_exit", "steam_running": True},
        }
        result = _handle_launch_game(self.api, profile_id="p1", confirm_action="wait_steam_exit")
        self.assertEqual(result["status"], "waiting")
        self.assertTrue(result["steam_running"])

    def test_confirm_success_returns_session(self):
        result = _handle_launch_game(self.api, profile_id="p1", confirm_action="continue")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["runtime_session"], {"id": "s2"})


class TestGetGameStatus(unittest.TestCase):
    def _api(self, *, monitor=True, steam=True, probe_raises=False):
        api = FakeApi()
        api.game_monitor = (
            SimpleNamespace(is_game_running=True, get_runtime_session_data=lambda: {"id": "s1"})
            if monitor
            else None
        )
        api._get_runtime_session_data = lambda: {"id": "s1"}
        steam_mgr = SimpleNamespace()
        if probe_raises:
            steam_mgr.is_steam_running = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        else:
            steam_mgr.is_steam_running = lambda: steam
        api.steam_mgr = steam_mgr
        return api

    def test_full_status(self):
        result = _handle_get_game_status(self._api())
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["running"])
        self.assertTrue(result["steam_running"])
        self.assertEqual(result["runtime_session"], {"id": "s1"})

    def test_missing_monitor_reports_not_running(self):
        api = self._api(monitor=False)
        result = _handle_get_game_status(api)
        self.assertFalse(result["running"])
        self.assertEqual(result["runtime_session"], {"id": "s1"})

    def test_steam_probe_failure_reports_none(self):
        result = _handle_get_game_status(self._api(probe_raises=True))
        self.assertIsNone(result["steam_running"])


class TestSearchModText(unittest.TestCase):
    """search_mod_text：事件聚合 / 截断 / failed → 因果错误。

    事件经 tap → 队列到达 handler，而 tap 在 handler 内部（start_search 之前）才
    安装——因此 fake 的 start_search 用 timer 线程在返回后补发事件，与真实任务
    「启动后才 emit」的时序一致（参照 TestWaitEventBridge 的 timer 范式）。
    """

    def setUp(self):
        from backend.utils.event_bus import EventBus

        restore_event_tap()
        self.EventBus = EventBus
        self._saved_emit = EventBus.__dict__.get("emit")
        EventBus.emit = lambda name, data=None: None  # type: ignore[method-assign]
        self._pending_backup = list(EventBus._pending_events)
        EventBus._pending_events.clear()
        self._timers: list[threading.Timer] = []

    def tearDown(self):
        for timer in self._timers:
            timer.cancel()
            timer.join(timeout=2)
        restore_event_tap()
        if self._saved_emit is not None:
            self.EventBus.emit = self._saved_emit  # type: ignore[method-assign]
        self.EventBus._pending_events[:] = self._pending_backup

    def _api(self, task_id="t-search", plan=None):
        """plan: [(delay, kwargs), ...] —— start_search 返回后按延迟依次 emit。"""
        api = FakeApi()

        def start_search(payload):
            api.search_payload = dict(payload or {})
            for index, entry in enumerate(plan or []):
                delay, data = entry
                timer = threading.Timer(
                    delay,
                    self.EventBus.emit,
                    args=("file-search-results", {"task_id": task_id, **data}),
                )
                timer.start()
                self._timers.append(timer)
            return task_id

        api.file_search_mgr = SimpleNamespace(start_search=start_search)
        return api

    def _emit_later(self, delay: float, event: str, data: dict) -> None:
        """tap 安装后才会入队的事件（handler 内部才装 tap，提前 emit 进不了队列）。"""
        timer = threading.Timer(delay, self.EventBus.emit, args=(event, data))
        timer.start()
        self._timers.append(timer)

    def test_empty_query_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_search_mod_text(self._api(), query="  ")
        self.assertIn("query", str(ctx.exception))

    def test_aggregates_results_until_done(self):
        api = self._api(
            "t1",
            plan=[
                (
                    0.02,
                    {
                        "results": [
                            {"mod_name": "A", "file_path": "a.xml", "line_number": 1, "matched_line": "<def>"}
                        ],
                        "done": False,
                        "status": "running",
                        "matched_count": 2,
                    },
                ),
                (
                    0.08,
                    {
                        "results": [
                            {"mod_name": "B", "file_path": "b.xml", "line_number": 9, "matched_line": "err"},
                            {"mod_name": "C", "file_path": "c.xml", "line_number": 4, "matched_line": "x"},
                        ],
                        "done": True,
                        "status": "success",
                        "matched_count": 3,
                    },
                ),
            ],
        )
        result = _handle_search_mod_text(api, query="def")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["matched_count"], 3)
        self.assertEqual(result["returned"], 3)
        self.assertFalse(result["truncated"])
        self.assertEqual(api.search_payload["scope"], "current-active")

    def test_max_results_truncates(self):
        api = self._api(
            "t2",
            plan=[
                (
                    0.02,
                    {
                        "results": [
                            {"file_path": f"f{i}.xml", "line_number": i, "matched_line": "x"}
                            for i in range(5)
                        ],
                        "done": True,
                        "status": "success",
                        "matched_count": 5,
                    },
                )
            ],
        )
        result = _handle_search_mod_text(api, query="x", max_results=2)
        self.assertEqual(result["returned"], 2)
        self.assertTrue(result["truncated"])

    def test_failed_status_maps_to_causal_error(self):
        from fastmcp.exceptions import ToolError

        api = self._api(
            "t3",
            plan=[(0.02, {"results": [], "done": True, "status": "failed", "matched_count": 0, "message": "ripgrep 缺失"})],
        )
        with self.assertRaises(ToolError) as ctx:
            _handle_search_mod_text(api, query="x")
        self.assertIn("ripgrep 缺失", str(ctx.exception))

    def test_timeout_returns_partial(self):
        api = self._api(
            "t4",
            plan=[
                (0.02, {"results": [{"file_path": "f.xml", "line_number": 1, "matched_line": "x"}], "done": False, "status": "running", "matched_count": 1})
            ],
        )
        result = _handle_search_mod_text(api, query="x", timeout_seconds=1.0)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returned"], 1)
        self.assertIn("超时", result["message"])

    def test_foreign_and_stale_events_ignored(self):
        api = self._api(
            "t5",
            plan=[(0.30, {"results": [], "done": True, "status": "success", "matched_count": 0})],
        )
        # 两类干扰事件在 tap 安装后、t5 完成前到达：非本工具事件、非本任务事件
        self._emit_later(0.05, "scan-complete", {"status": "success"})
        self._emit_later(
            0.10,
            "file-search-results",
            {
                "task_id": "t-other",
                "results": [{"file_path": "noise.xml", "line_number": 0, "matched_line": "n"}],
                "done": True,
                "status": "success",
                "matched_count": 99,
            },
        )
        result = _handle_search_mod_text(api, query="x")
        self.assertEqual(result["matched_count"], 0)
        self.assertEqual(result["returned"], 0)

    # ---- file_types / filenames_only 透传（默认值＝上游内容检索原行为）-------

    def test_new_params_default_to_upstream_behaviour(self):
        api = self._api(
            "t7",
            plan=[(0.02, {"results": [], "done": True, "status": "success", "matched_count": 0})],
        )
        result = _handle_search_mod_text(api, query="def")
        self.assertFalse(api.search_payload.get("filenames_only"))
        self.assertNotIn("file_types", api.search_payload)  # 未指定＝上游默认 .xml
        self.assertFalse(result["filenames_only"])
        self.assertNotIn("file_types", result)

    def test_file_types_and_filenames_only_are_forwarded(self):
        api = self._api(
            "t8",
            plan=[(0.02, {"results": [], "done": True, "status": "success", "matched_count": 0})],
        )
        result = _handle_search_mod_text(
            api, query="BGPlanet", filenames_only=True, file_types=[".png"]
        )
        self.assertTrue(api.search_payload["filenames_only"])
        self.assertEqual(api.search_payload["file_types"], [".png"])
        self.assertTrue(result["filenames_only"])
        self.assertEqual(result["file_types"], [".png"])

    def test_file_types_must_be_a_list(self):
        from fastmcp.exceptions import ToolError

        api = self._api("t9", plan=[])
        with self.assertRaises(ToolError) as ctx:
            _handle_search_mod_text(api, query="x", file_types=".xml")
        self.assertIn("file_types", str(ctx.exception))


class TestCancelTask(unittest.TestCase):
    """cancel_task：统一取消入口 cancel_progress_task 的类型白名单 + 参数因果错误。"""

    def setUp(self) -> None:
        self.api = FakeApi()
        self.calls: list[tuple[str, str]] = []
        self.payload: dict = {
            "status": "success",
            "data": None,
            "message": "已请求取消下载任务",
        }

        def cancel(task_id, task_type):
            self.calls.append((task_id, task_type))
            return dict(self.payload)

        self.api.cancel_progress_task = cancel

    def test_success_forwards_normalized_args(self):
        result = _handle_cancel_task(self.api, task_type=" Download ", task_id=" t1 ")
        self.assertEqual(self.calls, [("t1", "download")])  # 归一化方式与上游 cancel_progress_task 一致
        self.assertEqual(result["status"], "success")

    def test_scan_allows_empty_task_id(self):
        result = _handle_cancel_task(self.api, task_type="scan")
        self.assertEqual(self.calls, [("", "scan")])
        self.assertEqual(result["status"], "success")

    def test_missing_task_id_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_cancel_task(self.api, task_type="file-search")
        parsed = json.loads(str(ctx.exception))
        self.assertIn("task_id", parsed["message"])
        self.assertTrue(any("task_id" in action for action in parsed["nextActions"]))
        self.assertEqual(self.calls, [])  # 校验失败不触碰后端

    def test_unknown_task_type_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_cancel_task(self.api, task_type="nuke", task_id="t1")
        parsed = json.loads(str(ctx.exception))
        self.assertIn("task_type", parsed["message"])
        self.assertIn("file-search", parsed["message"])  # 错误里给出可用取值
        self.assertEqual(self.calls, [])

    def test_upstream_failure_maps_to_causal_error(self):
        from fastmcp.exceptions import ToolError

        self.payload = {"status": "error", "message": "当前没有可取消的文件搜索任务"}
        with self.assertRaises(ToolError) as ctx:
            _handle_cancel_task(self.api, task_type="file-search", task_id="t9")
        parsed = json.loads(str(ctx.exception))
        self.assertIn("当前没有可取消", parsed["message"])
        self.assertTrue(any("get_download_tasks" in action for action in parsed["nextActions"]))

    def test_signature_enum_matches_handler_whitelist(self):
        """签名里的 Literal 与 handler 白名单必须始终一致。"""
        from typing import get_args, get_type_hints

        spec = next(spec for spec in TOOL_SPECS if spec.name == "cancel_task")
        allowed = get_args(get_type_hints(spec.func)["task_type"])
        self.assertEqual(set(allowed), set(_CANCEL_TASK_TYPES))
        self.assertEqual(len(allowed), 16)


class TestReadModFile(unittest.TestCase):
    """read_mod_file：模组根白名单边界（拒绝 .. 与越界）+ 上限/截断标记透传。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mod_root = os.path.join(self._tmp.name, "Mods")
        inside_dir = os.path.join(self.mod_root, "MyMod", "About")
        os.makedirs(inside_dir, exist_ok=True)
        self.inside_file = os.path.join(inside_dir, "About.xml")
        with open(self.inside_file, "w", encoding="utf-8") as handle:
            handle.write("<ModMetaData><name>测试模组</name></ModMetaData>")
        self.outside_file = os.path.join(self._tmp.name, "secret.txt")
        with open(self.outside_file, "w", encoding="utf-8") as handle:
            handle.write("outside")

        self.api = FakeApi()
        self.api.active_context = SimpleNamespace(profile_id="test")
        self.api._build_scan_paths_for_profile = lambda context: [self.mod_root]
        self.read_calls: list[tuple[str, int]] = []

        def read_text_file(path, max_bytes=2 * 1024 * 1024):
            self.read_calls.append((path, max_bytes))
            if not os.path.isfile(path):
                return {
                    "status": "error",
                    "message": "读取文本文件失败。请确认文件存在、编码可读取且未超过大小限制。",
                }
            with open(path, "rb") as handle:
                raw = handle.read(max_bytes + 1)
            return {
                "status": "success",
                "data": {
                    "path": os.path.abspath(path),
                    "encoding": "utf-8",
                    "truncated": len(raw) > max_bytes,
                    "file_size": os.path.getsize(path),
                    "content": raw[:max_bytes].decode("utf-8", "replace"),
                },
                "message": "",
            }

        self.api.path_read_text_file = read_text_file

        # 模组个体路径来源（第二个白名单来源）默认置空，用例按需覆盖
        patcher = mock.patch(
            "backend.database.dao.ModDAO.get_profile_mods", return_value=[]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_file_inside_mod_root(self):
        result = _handle_read_mod_file(self.api, path=self.inside_file)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["file_path"], os.path.abspath(self.inside_file))
        self.assertIn("测试模组", result["content"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["max_bytes"], 65536)
        # 上游按本工具的收窄上限读（不是默认 2 MB）
        self.assertEqual(self.read_calls[0][1], 65536)

    def test_path_outside_roots_rejected_before_io(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_read_mod_file(self.api, path=self.outside_file)
        parsed = json.loads(str(ctx.exception))
        self.assertIn("模组根目录", parsed["message"])
        self.assertTrue(parsed["nextActions"])
        self.assertEqual(self.read_calls, [])  # 边界先于 IO：不碰 path_read_text_file

    def test_dotdot_path_rejected(self):
        from fastmcp.exceptions import ToolError

        escaped = os.path.join(self.mod_root, "..", "secret.txt")
        with self.assertRaises(ToolError) as ctx:
            _handle_read_mod_file(self.api, path=escaped)
        self.assertIn("..", str(ctx.exception))
        self.assertEqual(self.read_calls, [])

    def test_empty_path_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_read_mod_file(self.api, path="  ")
        self.assertIn("path", str(ctx.exception))

    def test_missing_file_inside_root_is_causal(self):
        from fastmcp.exceptions import ToolError

        missing = os.path.join(self.mod_root, "MyMod", "About", "Nope.xml")
        with self.assertRaises(ToolError) as ctx:
            _handle_read_mod_file(self.api, path=missing)
        self.assertIn("读取文本文件失败", str(ctx.exception))

    def test_mod_record_path_widens_roots(self):
        other_root = os.path.join(self._tmp.name, "Elsewhere")
        os.makedirs(other_root, exist_ok=True)
        other_file = os.path.join(other_root, "extra.xml")
        with open(other_file, "w", encoding="utf-8") as handle:
            handle.write("<x/>")
        with mock.patch(
            "backend.database.dao.ModDAO.get_profile_mods",
            return_value=[{"path": other_root}],
        ):
            result = _handle_read_mod_file(self.api, path=other_file)
        self.assertEqual(result["file_path"], os.path.abspath(other_file))

    def test_no_roots_is_causal(self):
        from fastmcp.exceptions import ToolError

        self.api._build_scan_paths_for_profile = lambda context: []
        with self.assertRaises(ToolError) as ctx:
            _handle_read_mod_file(self.api, path=self.inside_file)
        self.assertIn("模组根目录", str(ctx.exception))


class TestGetWorkshopModDetails(unittest.TestCase):
    """get_workshop_mod_details：批量查询的入参校验与上游 mod_settings_workshop_details 透传。"""

    def setUp(self) -> None:
        self.api = FakeApi()
        self.calls: list[list[str]] = []
        self.payload: dict = {
            "status": "success",
            "data": {
                "2009463077": {
                    "workshop_id": "2009463077",
                    "package_id": "brrainz.harmony",
                    "title": "Harmony",
                    "detail_source": "database",
                    "available": True,
                }
            },
            "message": "",
        }

        def details(workshop_ids):
            self.calls.append(list(workshop_ids))
            return dict(self.payload)

        self.api.mod_settings_workshop_details = details

    def test_success_is_passed_through_with_cleaned_ids(self):
        result = _handle_get_workshop_mod_details(
            self.api, workshop_ids=[" 2009463077 ", "", "744674897"]
        )
        self.assertEqual(result, self.payload)  # 上游返回包装原样透传（未重塑）
        self.assertEqual(result["status"], "success")
        self.assertIn("detail_source", result["data"]["2009463077"])
        self.assertEqual(self.calls[0], ["2009463077", "744674897"])

    def test_non_list_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_get_workshop_mod_details(self.api, workshop_ids="2009463077")
        self.assertIn("workshop_ids", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_empty_ids_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_get_workshop_mod_details(self.api, workshop_ids=["", "  "])
        self.assertIn("workshop_ids", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_over_limit_is_causal(self):
        from fastmcp.exceptions import ToolError

        ids = [str(1000000 + index) for index in range(101)]
        with self.assertRaises(ToolError) as ctx:
            _handle_get_workshop_mod_details(self.api, workshop_ids=ids)
        parsed = json.loads(str(ctx.exception))
        self.assertIn("100", parsed["message"])
        self.assertEqual(self.calls, [])

    def test_upstream_error_maps_to_causal(self):
        from fastmcp.exceptions import ToolError

        self.payload = {"status": "error", "message": "获取工坊信息失败"}
        with self.assertRaises(ToolError) as ctx:
            _handle_get_workshop_mod_details(self.api, workshop_ids=["2009463077"])
        self.assertIn("获取工坊信息失败", str(ctx.exception))


class TestGithubSubscribeRepo(unittest.TestCase):
    """github_subscribe_repo：先取仓库信息再落订阅；info 组装与因果边界（取信息失败不得写订阅记录）。"""

    def setUp(self) -> None:
        self.api = FakeApi()
        self.fetch_calls: list[tuple[str, str]] = []
        self.subscribe_payloads: list[dict] = []
        self.fetch_result: dict = {
            "status": "success",
            "data": {
                "provider": "github",
                "host": "github.com",
                "owner": "o",
                "repo": "r",
                "default_branch": "main",
                "has_release": True,
                "latest_release_tag": "v1.2.3",
            },
        }
        self.subscribe_result: dict = {"status": "success", "data": []}

        def fetch_info(url, source_branch=""):
            self.fetch_calls.append((str(url), str(source_branch or "")))
            return dict(self.fetch_result)

        def subscribe(payload):
            self.subscribe_payloads.append(dict(payload))
            return dict(self.subscribe_result)

        self.api.github_fetch_info = fetch_info
        self.api.github_subscribe = subscribe

    def test_success_assembles_payload_with_info(self):
        result = _handle_github_subscribe_repo(self.api, url=" https://github.com/o/r ")
        self.assertEqual(self.fetch_calls, [("https://github.com/o/r", "")])
        payload = self.subscribe_payloads[0]
        self.assertEqual(payload["url"], "https://github.com/o/r")
        self.assertEqual(payload["install_type"], "source")
        self.assertEqual(payload["default_branch"], "main")  # 留空跟随仓库默认分支
        self.assertEqual(payload["info"]["latest_release_tag"], "v1.2.3")
        self.assertEqual(result["status"], "success")

    def test_release_install_type_and_version_passthrough(self):
        _handle_github_subscribe_repo(
            self.api,
            url="https://github.com/o/r",
            install_type="release",
            version="v1.0.0",
        )
        payload = self.subscribe_payloads[0]
        self.assertEqual(payload["install_type"], "release")
        self.assertEqual(payload["installed_version"], "v1.0.0")

    def test_explicit_branch_overrides_default(self):
        _handle_github_subscribe_repo(self.api, url="u", source_branch="dev")
        self.assertEqual(self.fetch_calls, [("u", "dev")])
        self.assertEqual(self.subscribe_payloads[0]["default_branch"], "dev")

    def test_empty_url_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_github_subscribe_repo(self.api, url="  ")
        parsed = json.loads(str(ctx.exception))
        self.assertIn("url", parsed["message"])
        self.assertEqual(self.subscribe_payloads, [])  # 校验失败不落订阅

    def test_invalid_install_type_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_github_subscribe_repo(self.api, url="u", install_type="clone")
        parsed = json.loads(str(ctx.exception))
        self.assertIn("install_type", parsed["message"])
        self.assertIn("release", parsed["message"])  # 错误里给出可用取值
        self.assertEqual(self.fetch_calls, [])  # 参数校验先于联网

    def test_fetch_failure_does_not_subscribe(self):
        from fastmcp.exceptions import ToolError

        self.fetch_result = {"status": "error", "message": "获取 Git 仓库信息失败"}
        with self.assertRaises(ToolError) as ctx:
            _handle_github_subscribe_repo(self.api, url="https://github.com/o/gone")
        parsed = json.loads(str(ctx.exception))
        self.assertIn("获取 Git 仓库信息失败", parsed["message"])
        self.assertEqual(self.subscribe_payloads, [])  # fetch 失败不落订阅

    def test_signature_enum_matches_handler_whitelist(self):
        """签名里的 Literal 与 handler 白名单必须始终一致。"""
        from typing import get_args, get_type_hints

        spec = next(spec for spec in TOOL_SPECS if spec.name == "github_subscribe_repo")
        allowed = get_args(get_type_hints(spec.func)["install_type"])
        self.assertEqual(set(allowed), set(_GITHUB_INSTALL_TYPES))
        self.assertEqual(len(allowed), 3)


class TestDelegateBindings(unittest.TestCase):
    """本组工具直接委托的 17 个方法必须真实存在于 backend.api.API（防映射写错）。"""

    def test_delegate_methods_exist_on_real_api(self):
        from backend.api import API

        delegated = {
            spec.name: spec.method
            for spec in TOOL_SPECS
            if spec.method is not None
        }
        expected = {
            "set_mod_absolute_position": "rule_set_user_mod_absolute_position",
            "get_residue_overview": "mod_residue_get_overview",
            "trace_install_sources": "get_install_sources_by_package_ids",
            "download_workshop_mods": "steamcmd_download",
            "get_download_tasks": "get_active_downloads",
            "check_mod_updates": "lifecycle_check_updates",
            # 订阅与部署批：Steam/WebAPI 订阅、Git 仓库订阅、本地模组包
            "subscribe_workshop_items": "steam_subscribe",
            "unsubscribe_workshop_items": "steam_unsubscribe",
            "webapi_subscribe_workshop": "workshop_webapi_subscribe",
            "webapi_unsubscribe_workshop": "workshop_webapi_unsubscribe",
            "github_fetch_repo_info": "github_fetch_info",
            "github_get_subscribed": "github_get_subscribed",
            "github_trigger_download": "github_trigger_download",
            "github_get_timeline": "github_get_timeline",
            "github_remove_subscription": "github_remove_subscription",
            "mod_package_prepare_import": "mod_package_prepare_import",
            "mod_package_import": "mod_package_import",
        }
        for name, method in expected.items():
            self.assertEqual(delegated.get(name), method, f"{name} 绑定漂移")
            self.assertTrue(
                callable(getattr(API, method, None)),
                f"API.{method} 不存在（{name} 的映射已失效）",
            )


if __name__ == "__main__":
    unittest.main()
