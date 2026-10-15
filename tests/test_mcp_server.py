# tests/test_mcp_server.py
"""MCP 工具层测试。

覆盖：注册表完整性、fork schema 对齐、工具 desc 与 backend.ai 字段级一致性、
语义护栏（元素边界二分 / 合法 JSON / 防注入）、分页返回结构、read_logs 过滤、
apply_load_order_ops 全链路（token/dry_run/原子拒绝）、wait_event 事件桥、
初始化期因果错误、错误格式（nextActions）。

**schema 偏差记录（逐条说明与 fork 不一致之处）**：

1. ``list_mods``：payload 重塑为分页返回结构（items=all_mods + is_active 标注）；
   fork 的 disabled_mods/groups/active_load_order 不随行返回——分别由
   get_load_order / get_groups 定向查询（定向小页查询）。
2. ``get_load_order``：带 filter/fields/limit/offset（``get_load_order {limit:50}``），
   fork 为零参全量。
3. ``save_load_order``：desc 带「大载荷，推荐 apply_ops」告警。
4. ``read_logs``：带 levels/since/until/line_from/line_to/keyword/tail
   读日志参数，fork 仅 5 个分页参数。
5. ``get_log_context``：带 log_type/filename 日志源选择（MCP 无前端选中上下文，
   缺省取最新日志文件）；AI arguments_str 仍严格等于上游 pydantic 字段。
6. 7 个只读工具 desc 带 ``[只读] `` 前缀（全量工具风险标注一致性），
   正文与 ``backend.ai`` 的 llm_description 逐字对齐（本文件动态断言）。
7. ``wait_event`` desc 用 fork 的函数 docstring 版（fork 注册行另有一句短版，
   两者语义一致），含按等待者隔离的事件缓冲 / 端点就绪前事件捕获不到的边界说明。
8. ``list_profiles``：默认剥离 ``inactive_mods_order``/``temp_mods_order`` 整序数组
   （大载荷——单档案约 130KB，宜按需定向查询）；激活顺序的真相源是
   get_load_order。

其余 16 个 fork 工具的 desc / 签名 / 风险前缀与 fork 逐字对齐（下表断言）。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.mcp_adapters_query import (
    _handle_get_groups,
    _handle_get_load_order,
    _handle_get_settings_overview,
    _handle_list_mods,
    _handle_list_profiles,
    _handle_read_logs,
)
from backend.mcp_guard import (
    DEFAULT_MAX_OUTPUT,
    TRUNCATED_KEY,
    build_error_text,
    guard_payload,
    max_output,
    page_envelope,
)
from backend.mcp_handlers_features import (
    _handle_apply_load_order_ops,
    _handle_get_mod_dependencies,
)
from backend.mcp_server import (
    ManifestOnlyApi,
    _handle_wait_event,
    create_mcp_app,
    install_event_tap,
    restore_event_tap,
)
from backend.mcp_tool_specs import (
    RISK_HIGH,
    RISK_READ,
    RISK_WRITE,
    SERVER_INSTRUCTIONS,
    TOOL_SPECS,
    validate_registry,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _list_tools(mcp) -> list:
    return list(asyncio.run(mcp.list_tools()))


# fork 侧 desc 原文（取自 fork 分支的 backend/mcp_server.py TOOL_SPECS，复制为对齐基准
# 固化在测试里——fork 文件在仓库外，不能成为运行期依赖）。
FORK_DESCRIPTIONS: dict[str, str] = {
    "get_initial_data": (
        "[只读] 获取全量启动数据(设置/模组列表/分组/加载顺序等)。"
        "输出体积较大,首次了解项目状态时使用;之后请用 list_mods / get_load_order 等定向查询。"
    ),
    "get_load_order": "[只读] 获取当前档案的加载顺序信息。",
    "list_backups": "[只读] 列出所有备份文件(可按 profile_id 过滤)。",
    "list_profiles": "[只读] 列出所有游戏档案(profile)。",
    "read_logs": (
        "[只读] 分页读取系统日志(JSONL),适合错误排查与 AI 诊断。"
        "参数 log_type / filename 从 list_log_files 等来源获取,缺省按 active 档案。"
    ),
    "get_settings_overview": "[只读] 获取模组设置概览。",
    "get_groups": "[只读] 获取全部分组及其模组归属(含 group_id/name/color/sort_index/mod_ids)。",
    "scan_mods": (
        "[写] 触发后台模组扫描,立即返回已启动状态;"
        "完成后可用 wait_event(\"scan-complete\") 等待,或轮询 list_mods 确认结果。"
        "specific_paths 为空时使用设置中的默认路径。"
    ),
    "sort_mods": "[写] 对传入的激活模组 ID 列表执行自动排序,返回 sorted_ids / warnings。",
    "save_load_order": (
        "[写] 保存激活列表到 ModsConfig.xml,带磁盘冲突保护(token 校验)。"
        "active_ids 为完整的目标激活列表;base_version_token 需传此前读取到的版本 token,否则可能冲突。"
    ),
    "update_mod_data": (
        "[写] 更新某个模组的用户自定义数据(别名/标签/备注/颜色等),data_dict 为要合并写入的字段。"
    ),
    "create_group": "[写] 创建模组分组,name 为分组名,color 为颜色(十六进制如 #FF0000)。",
    "update_group": "[写] 更新分组属性(重命名/改色/折叠),updates 可含 name/color/is_expanded 字段。",
    "add_mods_to_group": "[写] 把模组 ID 列表加入指定分组(mod_ids 用 package_id)。",
    "remove_mods_from_group": "[写] 从指定分组移除模组 ID 列表(mod_ids 用 package_id)。",
    "reorder_group_content": "[写] 重排分组内模组的显示顺序,mod_id_list 为该分组完整的目标顺序。",
    "reorder_groups": "[写] 重排分组列表的显示顺序,group_id_list 为完整的目标顺序。",
    "create_backup": (
        "[高风险] 把加载顺序备份为文件(ModsConfig.xml 风格)到指定目录。"
        "会写磁盘文件,请确认 target_dir 正确后再调用。"
    ),
    "activate_profile": (
        "[高风险] 切换当前游戏档案(profile),会影响后续所有操作的作用域,"
        "切换后请用 list_mods / get_load_order 重新读取状态。"
    ),
    "delete_group": "[高风险] 删除指定分组(含其模组归属关系),不可恢复,请先 get_groups 确认 group_id。",
    # list_mods 有意改写（payload 重塑，见模块 docstring 偏差 1），单独断言。
}

# 与 fork 有意偏离的 desc：键 → 理由（见模块 docstring）。
DESC_DEVIATIONS: dict[str, str] = {
    "list_mods": "payload 重塑为分页返回结构，fork 原文的返回清单不再成立",
    "get_load_order": "分页参数说明",
    "save_load_order": "大载荷告警",
    "read_logs": "分级/范围参数说明",
    "list_profiles": "剥离 inactive/temp 整序数组（大载荷，单档案实测 ~130KB）",
    "wait_event": "按等待者隔离的事件缓冲/端点就绪前事件捕获不到的边界说明",
}


class FakeModScanner:
    def __init__(self, scanning: bool = False) -> None:
        self.is_scanning = scanning


class FakeLoadOrderManager:
    def __init__(self, ids: list[str] | None = None, stale: bool = False) -> None:
        self.ids = list(ids or ["ludeon.rimworld", "brrainz.harmony"])
        self.stale = stale
        self.saved: list[tuple[list[str], dict | None]] = []

    def is_version_token_stale(self, token):
        return self.stale, {"path": "x", "mtime_ms": 1, "size": 2, "active_hash": "h"}

    def read_active_mods(self):
        return {"active_mods": list(self.ids)}


class FakeApi:
    """最小后端替身：只实现适配器真正调用到的面（object.__new__ 惯例的轻量版）。"""

    def __init__(self, *, mods: int = 790) -> None:
        self.scanner = FakeModScanner()
        self.active_context = SimpleNamespace(profile_id="test")
        self.load_order_mgr = FakeLoadOrderManager()
        self.game_log_mgr = None
        self.all_mods = [
            {
                "package_id": f"fake.mod{i:04d}",
                "name": f"Fake Mod {i:04d} EVIL_NAME_MARKER",
                "workshop_id": 100000 + i,
            }
            for i in range(mods)
        ]
        self.active_ids = [m["package_id"] for m in self.all_mods[:10]]

    # ---- list_mods / get_load_order 数据源 ----
    def get_mod_list_core(self):
        return {
            "status": "success",
            "data": {
                "context_healthy": True,
                "all_mods": self.all_mods,
                "active_load_order": self.active_ids,
                "groups": [{"group_id": "g1", "mod_ids": []}],
            },
        }

    def load_order_get(self):
        ids = ["ludeon.rimworld", "brrainz.harmony"]
        return {
            "status": "success",
            "data": {
                "file": "/tmp/ModsConfig.xml",
                "active_ids": ids,
                "mod_names": ["RimWorld", "Harmony"],
                "mod_steam_workshop_ids": [None, 200940],
                "modify_time": 1758800000000,
                "version_token": {
                    "path": "/tmp/ModsConfig.xml",
                    "mtime_ms": 1758800000000,
                    "size": 42,
                    "active_hash": "abc",
                },
                "warnings": [],
                "errors": [],
            },
        }

    def profiles_get(self):
        return {"status": "success", "data": [{"profile_id": "p1", "name": "测试档案"}]}

    def groups_get(self):
        return {"status": "success", "data": [{"group_id": "g1", "name": "常用", "mod_ids": []}]}

    def load_order_save_with_token(self, active_ids, base_version_token=None, **kwargs):
        self.load_order_mgr.saved.append((list(active_ids), base_version_token))
        return {
            "status": "success",
            "data": {"saved": True, "version_token": {"active_hash": "new"}, "modify_time": 2},
        }


class TestSpecRegistry(unittest.TestCase):
    """注册表自检与 instructions 用语。"""

    def test_registry_is_valid_and_complete(self):
        validate_registry()  # 重复名/孤儿映射会直接抛
        # 66 = 管理面 33（32 + set_mod_rule）+ 排障/整理/获取 13 + 补缺 3 + 贴图 5
        #      + 获取通路补全 12（Steam 订阅 2 + WebAPI 订阅 2 + Git 仓库 6 + 本地模组包 2）
        self.assertEqual(len(TOOL_SPECS), 66)
        names = [spec.name for spec in TOOL_SPECS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_tool_has_handler_or_api_method(self):
        import backend.mcp_server as mcp_server

        for spec in TOOL_SPECS:
            # 不变量：每个工具要么有自实现处理器，要么有 API 方法可默认委托（二选一或兼有）。
            self.assertTrue(
                spec.name in mcp_server._HANDLER_OVERRIDES or spec.method is not None,
                f"{spec.name} 既无 handler 覆盖也无 API 方法",
            )

    def test_all_descriptions_carry_risk_prefix(self):
        for spec in TOOL_SPECS:
            self.assertIn(spec.risk, (RISK_READ, RISK_WRITE, RISK_HIGH), spec.name)
            self.assertTrue(spec.desc.startswith(("[只读]", "[写]", "[高风险]")), spec.name)

    def test_instructions_cover_four_disciplines(self):
        # 用语与 MCP/通用工程术语对齐（分页 / dry run / 乐观锁 / 摘要返回），
        # 不用自造说法。
        for keyword in ("分页", "dry_run", "version_token", "tail"):
            self.assertIn(keyword, SERVER_INSTRUCTIONS)

    # ---- 绑定不变量（孤儿 handler / 双注册 / 直接委托可达）------------------

    def test_handler_overrides_have_no_orphans(self):
        """覆盖表的键必须都挂在清单上——孤儿 handler 注册不到，静默失效。"""
        import backend.mcp_server as mcp_server

        names = {spec.name for spec in TOOL_SPECS}
        self.assertEqual(sorted(set(mcp_server._HANDLER_OVERRIDES) - names), [])

    def test_no_tool_is_bound_twice(self):
        """一个工具只允许一条绑定路径（覆盖表 ∩ _API_METHODS = ∅）。

        双注册时「注册表 + handler 体内 ``api.<method>``」是两份手工维护的清单，漏改其一
        没有测试会报错——适配工具只在覆盖表、不进 ``_API_METHODS``，本用例断言两者互斥；
        反向再断言「带分页参数（offset）的工具必须走适配 handler、不得直接委托」
        （直接委托会把 offset 原样传给不认识它的后端方法）。
        """
        import inspect

        import backend.mcp_tool_specs as mcp_tool_specs
        import backend.mcp_server as mcp_server

        dual = sorted(
            set(mcp_server._HANDLER_OVERRIDES) & set(mcp_tool_specs._API_METHODS)
        )
        self.assertEqual(dual, [])
        for spec in TOOL_SPECS:
            if "offset" in inspect.signature(spec.func).parameters:
                self.assertIn(spec.name, mcp_server._HANDLER_OVERRIDES, spec.name)
                self.assertIsNone(spec.method, spec.name)

    def test_validate_bindings_fails_fast_on_broken_bindings(self):
        """validate_bindings 三条不变量各自可触发（孤儿 / 双注册 / 委托方法缺失）。"""
        import backend.mcp_server as mcp_server

        manifest = ManifestOnlyApi()
        mcp_server.validate_bindings(manifest)  # 健康态不抛（check 3 对 manifest 跳过）
        # ① 孤儿 handler
        with mock.patch.dict(
            mcp_server._HANDLER_OVERRIDES, {"no_such_tool": lambda api, **kw: None}
        ):
            with self.assertRaises(RuntimeError):
                mcp_server.validate_bindings(manifest)
        # ② 双注册（同一工具同时进覆盖表与 _API_METHODS）
        with mock.patch.dict(
            mcp_server._HANDLER_OVERRIDES, {"create_group": lambda api, **kw: None}
        ):
            with self.assertRaises(RuntimeError):
                mcp_server.validate_bindings(manifest)
        # ③ 直接委托的 API 方法在 api 上不存在 → 建 app 期就炸，不留到首次调用
        with self.assertRaises(RuntimeError):
            create_mcp_app(SimpleNamespace())

    def test_missing_override_fails_loudly_not_silently_delegated(self):
        """适配工具（method=None）忘了 override → 建 app 期直接 RuntimeError。

        ``_API_METHODS`` 里的条目若被删掉而覆盖表项缺失，会静默降级成直接委托
        （返回未适配的原始载荷且无测试报错）；移除条目后同一改动变成响亮的启动失败。
        """
        import backend.mcp_server as mcp_server

        with mock.patch.dict(mcp_server._HANDLER_OVERRIDES):
            del mcp_server._HANDLER_OVERRIDES["list_profiles"]
            with self.assertRaises(RuntimeError):
                create_mcp_app(ManifestOnlyApi())

    # ---- 锁语义 ---------------------------------------------------------

    def test_invoke_takes_call_lock_except_wait_event(self):
        """``_invoke``：普通工具在 ``_CALL_LOCK`` 内执行，wait_event 豁免。

        探针用锁的公开 acquire/release：主线程持锁时，普通工具的调用线程必须阻塞
        在锁上（拿不到锁 = 在锁内执行），wait_event 的调用线程必须照常完成；
        松锁后普通工具继续跑通。不依赖锁的内部结构（``_is_owned`` 之类）。
        """
        import backend.mcp_server as mcp_server

        spec_plain = next(s for s in TOOL_SPECS if s.name == "create_group")
        spec_wait = next(s for s in TOOL_SPECS if s.name == "wait_event")
        self.assertNotIn(spec_plain.name, mcp_server._NO_LOCK_TOOLS)
        api = FakeApi()
        outcome: dict[str, object] = {}

        def handler(api_obj, **kwargs):  # noqa: ANN001 - _invoke 传入
            return {"status": "success"}

        def runner(key: str, spec) -> None:  # noqa: ANN001
            try:
                outcome[key] = json.loads(mcp_server._invoke(spec, handler, api, (), {}))
            except BaseException as exc:  # noqa: BLE001 - 失败要进断言，不让线程静默死
                outcome[f"{key}_error"] = exc

        self.assertTrue(mcp_server._CALL_LOCK.acquire(blocking=False))
        try:
            plain = threading.Thread(target=runner, args=("plain", spec_plain), daemon=True)
            plain.start()
            plain.join(0.5)
            self.assertTrue(plain.is_alive(), "普通工具未阻塞在 _CALL_LOCK 上（锁语义失效）")

            wait = threading.Thread(target=runner, args=("wait", spec_wait), daemon=True)
            wait.start()
            wait.join(2.0)
            self.assertFalse(wait.is_alive(), "wait_event 不应进入 _CALL_LOCK")
        finally:
            mcp_server._CALL_LOCK.release()

        plain.join(5.0)
        self.assertFalse(plain.is_alive())
        self.assertNotIn("plain_error", outcome, outcome.get("plain_error"))
        self.assertNotIn("wait_error", outcome, outcome.get("wait_error"))
        self.assertEqual(outcome.get("plain", {}).get("status"), "success")
        self.assertEqual(outcome.get("wait", {}).get("status"), "success")

    def test_scan_gated_and_lock_exempt_tools_are_registered(self):
        """扫描门控集合与锁豁免集合必须都是真实工具名（拼错 = 规则静默失效）。"""
        import backend.mcp_server as mcp_server

        names = {spec.name for spec in TOOL_SPECS}
        self.assertEqual(sorted(mcp_server._SCAN_GATED_TOOLS - names), [])
        self.assertEqual(sorted(mcp_server._NO_LOCK_TOOLS - names), [])
        # 语义锚点：扫描门控要覆盖首扫查询主路径，wait_event 必须豁免锁
        self.assertIn("list_mods", mcp_server._SCAN_GATED_TOOLS)
        self.assertIn("wait_event", mcp_server._NO_LOCK_TOOLS)


class TestForkSchemaAlignment(unittest.TestCase):
    """desc / 签名与 fork 逐字对齐，任何偏离都会让本用例变红。"""

    def _spec(self, name: str):
        for spec in TOOL_SPECS:
            if spec.name == name:
                return spec
        raise AssertionError(name)

    def test_descriptions_match_fork_verbatim_or_documented(self):
        for name, expected in FORK_DESCRIPTIONS.items():
            spec = self._spec(name)
            if name in DESC_DEVIATIONS:
                self.assertIn("（MCP 扩展）", spec.desc, name)
                continue
            # fork 的 desc 是单行连续文本；本仓 docstring 按行折行，比对时去掉换行。
            self.assertTrue(
                spec.desc.replace("\n", "").startswith(expected),
                f"{name} desc 与 fork 不一致",
            )

    def test_every_deviation_is_documented(self):
        # 反向：未登记的 desc 不得带「（MCP 扩展）」尾巴
        allowed = set(DESC_DEVIATIONS) | {"get_log_context"}
        for spec in TOOL_SPECS:
            if "（MCP 扩展）" in spec.desc:
                self.assertIn(spec.name, allowed, f"{spec.name} 的 MCP 扩展 desc 未登记偏差")

    def test_signatures_sampled_from_fork(self):
        import inspect

        by_name = {spec.name: spec for spec in TOOL_SPECS}

        sig = inspect.signature(by_name["create_group"].func)
        self.assertEqual(list(sig.parameters), ["name", "color"])
        self.assertEqual(list(sig.parameters.values())[0].default, inspect.Parameter.empty)

        sig = inspect.signature(by_name["delete_group"].func)
        self.assertEqual(list(sig.parameters), ["group_id"])

        sig = inspect.signature(by_name["save_load_order"].func)
        self.assertEqual(
            list(sig.parameters),
            ["active_ids", "is_dirty", "base_version_token"],
        )
        self.assertIs(sig.parameters["base_version_token"].default, None)

        sig = inspect.signature(by_name["scan_mods"].func)
        self.assertEqual(
            list(sig.parameters),
            ["specific_paths", "forced_update", "size_check_override", "size_check_paths"],
        )
        self.assertIs(sig.parameters["forced_update"].default, False)

        sig = inspect.signature(by_name["create_backup"].func)
        self.assertEqual(list(sig.parameters), ["path", "target_dir", "profile_id"])
        self.assertIs(sig.parameters["profile_id"].default, None)

    def test_wait_event_signature(self):
        import inspect

        spec = next(s for s in TOOL_SPECS if s.name == "wait_event")
        sig = inspect.signature(spec.func)
        self.assertEqual(list(sig.parameters), ["event_name", "timeout"])
        self.assertEqual(sig.parameters["timeout"].default, 60.0)
        self.assertTrue(spec.desc.startswith("[只读] 等待后端事件"))


class TestAIExportAlignment(unittest.TestCase):
    """AI 桥接工具：desc 与 backend.ai 的 llm_description 逐字对齐、签名与 args 模型同构。"""

    AI_TOOLS = (
        "get_log_context",
        "search_mods",
        "get_active_mod_list",
        "get_mod_info",
        "get_mod_rules",
        "get_mod_user_data",
        "get_group_mods",
    )

    def test_desc_matches_builtin_llm_description(self):
        from backend.ai.ai_tools import TOOL_DEFINITIONS

        by_name = {spec.name: spec for spec in TOOL_SPECS}
        for name in self.AI_TOOLS:
            llm = TOOL_DEFINITIONS[name].llm_description
            expected_prefix = f"[只读] {llm}"
            desc = by_name[name].desc
            self.assertTrue(
                desc == expected_prefix or desc.startswith(expected_prefix),
                f"{name} desc 与内置 llm_description 不一致",
            )

    def test_signature_matches_args_model_fields(self):
        import inspect

        from backend.ai.ai_tools import TOOL_DEFINITIONS

        by_name = {spec.name: spec for spec in TOOL_SPECS}
        mcp_extras = {"get_log_context": {"log_type", "filename"}}
        for name in self.AI_TOOLS:
            model_fields = set(TOOL_DEFINITIONS[name].args_model.model_fields)
            params = set(inspect.signature(by_name[name].func).parameters)
            self.assertEqual(
                params - mcp_extras.get(name, set()),
                model_fields,
                f"{name} 参数与上游 args 模型不一致",
            )
            # 必填集合也必须一致（MCP 扩展参数只允许是可选的）
            required_model = {
                f for f, meta in TOOL_DEFINITIONS[name].args_model.model_fields.items()
                if meta.is_required()
            }
            sig = inspect.signature(by_name[name].func)
            required_sig = {
                p for p, meta in sig.parameters.items() if meta.default is inspect.Parameter.empty
            }
            self.assertEqual(required_sig, required_model, name)

    def test_ai_bridge_handlers_match_ai_tools_and_are_read_only(self):
        """AI 桥绑定一致性：

        覆盖表里由 ``_make_ai_handler`` 产出的工具集合必须**恰等于** ``AI_TOOLS``
        （多一个/少一个都说明桥接走样或上游工具被漏桥），且这些工具的风险分级锁在
        ``[只读]``——L2 只读复用边界，不得有写工具混进 AI 桥。
        """
        import backend.mcp_server as mcp_server

        by_name = {spec.name: spec for spec in TOOL_SPECS}
        bridged: set[str] = set()
        for name, handler in mcp_server._HANDLER_OVERRIDES.items():
            freevars = getattr(getattr(handler, "__code__", None), "co_freevars", ())
            if "ai_name" not in freevars:
                continue  # 非 _make_ai_handler 产出（适配器 / 其他自实现 handler）
            ai_name = handler.__closure__[freevars.index("ai_name")].cell_contents
            self.assertEqual(ai_name, name, "AI 桥 handler 的 ai_name 必须等于工具名")
            bridged.add(name)

        self.assertEqual(bridged, set(self.AI_TOOLS))
        self.assertTrue(set(self.AI_TOOLS) <= set(mcp_server._HANDLER_OVERRIDES))
        for name in self.AI_TOOLS:
            self.assertEqual(by_name[name].risk, RISK_READ, name)
            self.assertTrue(by_name[name].desc.startswith("[只读] "), name)


class TestToolSchemas(unittest.TestCase):
    """tools/list 形态：清单、inputSchema、输出 schema、annotations、护栏注册。"""

    @classmethod
    def setUpClass(cls):
        cls.mcp = create_mcp_app(ManifestOnlyApi())
        cls.tools = _list_tools(cls.mcp)
        cls.by_name = {tool.name: tool for tool in cls.tools}

    def test_manifest_order_and_completeness(self):
        self.assertEqual(
            [tool.name for tool in self.tools], [spec.name for spec in TOOL_SPECS]
        )

    def test_apply_ops_input_schema(self):
        schema = self.by_name["apply_load_order_ops"].parameters
        self.assertEqual(schema["type"], "object")
        self.assertEqual(set(schema["required"]), {"base_version_token", "ops"})
        self.assertFalse(schema["additionalProperties"])
        self.assertIs(schema["properties"]["dry_run"]["default"], False)

    def test_list_mods_pagination_params(self):
        schema = self.by_name["list_mods"].parameters
        self.assertEqual(
            set(schema["properties"]), {"filter", "fields", "limit", "offset", "scope"}
        )
        self.assertIs(schema["properties"]["limit"]["default"], None)
        self.assertEqual(schema["properties"]["offset"]["default"], 0)
        self.assertEqual(schema["properties"]["scope"]["default"], "all")
        self.assertEqual(schema["properties"]["scope"]["enum"], ["all", "active", "inactive"])

    def test_enum_params(self):
        scope = self.by_name["get_mod_info"].parameters["properties"]["scope"]
        self.assertEqual(scope["enum"], ["simple", "all", "specific"])
        dep = self.by_name["get_mod_dependencies"].parameters["properties"]
        self.assertEqual(dep["direction"]["enum"], ["both", "up", "down"])
        self.assertEqual(dep["format"]["enum"], ["edges", "tree"])
        self.assertEqual(dep["depth"]["default"], 1)

    def test_output_schema_suppressed_for_single_copy_payloads(self):
        for tool in self.tools:
            self.assertIsNone(tool.output_schema, tool.name)

    def test_annotations_follow_risk(self):
        for tool in self.tools:
            spec = next(s for s in TOOL_SPECS if s.name == tool.name)
            if spec.risk == RISK_READ:
                self.assertTrue(tool.annotations.read_only_hint, tool.name)
                self.assertFalse(tool.annotations.destructive_hint, tool.name)
            elif spec.risk == RISK_HIGH:
                self.assertTrue(tool.annotations.destructive_hint, tool.name)

    def test_instructions_and_response_limiting_registered(self):
        self.assertEqual(self.mcp.instructions, SERVER_INSTRUCTIONS)
        from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware

        limiters = [
            mw for mw in self.mcp.middleware
            if isinstance(mw, ResponseLimitingMiddleware)
        ]
        self.assertEqual(len(limiters), 1)
        self.assertEqual(limiters[0].max_size, max_output())


class TestGuardPayload(unittest.TestCase):
    """guard_payload 语义层：上限内原样往返、超限按元素边界二分截断且结果为合法 JSON、截断提示防注入。"""

    def test_under_cap_returns_roundtrippable_json(self):
        payload = {"status": "success", "data": {"n": 1}}
        text = guard_payload(payload, max_bytes=DEFAULT_MAX_OUTPUT)
        self.assertEqual(json.loads(text), payload)

    def test_dict_truncates_on_element_boundary(self):
        items = [
            {"package_id": f"mod{i:05d}", "name": f"Name {i} {'x' * 40}", "pad": "y" * 40}
            for i in range(790)
        ]
        payload = {"total": 790, "items": items, "next_offset": None, "hint": "翻页"}
        cap = 8192
        text = guard_payload(payload, max_bytes=cap)
        self.assertLessEqual(len(text.encode("utf-8")), cap)
        parsed = json.loads(text)  # 合法 JSON（保险层做不到这点）
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertEqual(parsed["total_items"], 790)
        self.assertIsInstance(parsed["items"], list)
        kept = parsed["items"]
        # 元素边界：保留下来的必须是原列表的真实前缀
        self.assertEqual(kept, items[: len(kept)])
        # 二分最优：带着返回结构字段再塞一个元素就超限
        probe = dict(parsed)
        probe["items"] = items[: len(kept) + 1]
        bigger = json.dumps(probe, ensure_ascii=False, separators=(",", ":"))
        self.assertGreater(len(bigger.encode("utf-8")), cap)
        # 防注入：截断提示为常量，不含任何模组名
        self.assertNotIn("EVIL_NAME_MARKER", text)

    def test_top_level_list_wraps_with_envelope(self):
        items = [{"v": i, "pad": "z" * 50} for i in range(500)]
        text = guard_payload(items, max_bytes=4096)
        parsed = json.loads(text)
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertEqual(parsed["total_items"], 500)
        self.assertEqual(parsed["items"], items[: len(parsed["items"])])

    def test_scalar_payload_falls_back_to_minimal_envelope(self):
        payload = {"blob": "q" * 10000, "n": 1}
        text = guard_payload(payload, max_bytes=512)
        parsed = json.loads(text)
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertIn("blob", parsed.get("truncated_fields", []))

    def test_env_override(self):
        import os
        from unittest import mock as _mock

        with _mock.patch.dict(os.environ, {"RIMCROW_MCP_MAX_OUTPUT": "4096"}):
            self.assertEqual(max_output(), 4096)
        with _mock.patch.dict(os.environ, {"RIMCROW_MCP_MAX_OUTPUT": "not-a-number"}):
            self.assertEqual(max_output(), DEFAULT_MAX_OUTPUT)

    def test_error_text_is_json_with_next_actions(self):
        text = build_error_text("RimCrow 未运行", ["启动 RimCrow 后重试"], extra={"conflicts": []})
        parsed = json.loads(text)
        self.assertEqual(parsed["status"], "error")
        self.assertEqual(parsed["nextActions"], ["启动 RimCrow 后重试"])
        self.assertIn("conflicts", parsed)

    def test_semantic_cap_is_derived_below_insurance_cap(self):
        """语义 cap 从保险 cap 派生且显著更低（否则整帧转义会切破合法 JSON）。"""
        import os
        from unittest import mock as _mock

        from backend.mcp_guard import SEMANTIC_RATIO, semantic_max_output

        self.assertLess(semantic_max_output(), max_output())
        self.assertEqual(semantic_max_output(), int(max_output() * SEMANTIC_RATIO))
        # env 缩放保险层时语义层同比缩放（两层关系是派生，不是两个独立常数）
        with _mock.patch.dict(os.environ, {"RIMCROW_MCP_MAX_OUTPUT": "4096"}):
            self.assertEqual(max_output(), 4096)
            self.assertEqual(semantic_max_output(), int(4096 * SEMANTIC_RATIO))

    def test_default_cap_output_fits_inside_insurance_frame(self):
        """语义层默认产物的**整帧**成本（含转义）必须 ≤ 保险层 cap。"""
        from backend.mcp_guard import frame_cost

        items = [{"package_id": f"author.mod{i:04d}", "pad": "x" * 60} for i in range(6000)]
        text = guard_payload({"items": items, "total": 6000, "next_offset": None})
        parsed = json.loads(text)
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertLessEqual(frame_cost(text), max_output())
        # 判别方式：单字段形态 total_items 是 int，且没有 truncated_fields
        self.assertIsInstance(parsed["total_items"], int)
        self.assertNotIn("truncated_fields", parsed)


class TestListModsAdapter(unittest.TestCase):
    """list_mods 适配器：filter / fields / limit / offset + 分页返回结构。"""

    def setUp(self):
        self.api = FakeApi(mods=790)

    def test_filter_is_case_insensitive_substring(self):
        envelope = _handle_list_mods(self.api, filter="FAKE.MOD0001")
        self.assertEqual(envelope["total"], 1)
        self.assertEqual(envelope["items"][0]["package_id"], "fake.mod0001")

    def test_filter_matches_workshop_id(self):
        # workshop_id 是 int（上游 all_mods 原样给），归一成字符串后子串匹配——
        # 名称/包名里都没有 "100003"，命中只可能来自 workshop_id。
        envelope = _handle_list_mods(self.api, filter="100003")
        self.assertEqual(envelope["total"], 1)
        self.assertEqual(envelope["items"][0]["package_id"], "fake.mod0003")
        self.assertEqual(envelope["items"][0]["workshop_id"], 100003)
        # 与 scope 正交：按工坊 id 找模组照样能叠加启用态过滤
        active = _handle_list_mods(self.api, filter="100003", scope="active")
        self.assertEqual(active["total"], 1)
        self.assertTrue(active["items"][0]["is_active"])

    def test_paging_envelope(self):
        envelope = _handle_list_mods(self.api, limit=50, offset=100)
        self.assertEqual(envelope["total"], 790)
        self.assertEqual(len(envelope["items"]), 50)
        self.assertEqual(envelope["next_offset"], 150)
        self.assertIn("hint", envelope)
        last = _handle_list_mods(self.api, limit=50, offset=740)
        self.assertIsNone(last["next_offset"])

    def test_fields_projection_and_is_active_annotation(self):
        envelope = _handle_list_mods(self.api, fields=["package_id"], limit=5)
        for item in envelope["items"]:
            self.assertEqual(set(item), {"package_id", "is_active"})
        active = {i["package_id"] for i in envelope["items"] if i["is_active"]}
        self.assertTrue(active.issubset(set(self.api.active_ids)))

    # ---- scope（启用态过滤，适配层实现，不改上游 all_mods 返回）------------

    def test_scope_all_is_default_and_unfiltered(self):
        self.assertEqual(_handle_list_mods(self.api)["total"], 790)
        self.assertEqual(_handle_list_mods(self.api, scope="all")["total"], 790)

    def test_scope_active_returns_only_enabled(self):
        envelope = _handle_list_mods(self.api, scope="active", fields=["package_id"], limit=1000)
        self.assertEqual(envelope["total"], len(self.api.active_ids))
        self.assertTrue(all(item["is_active"] for item in envelope["items"]))
        self.assertEqual(
            {i["package_id"] for i in envelope["items"]},
            set(self.api.active_ids),
        )

    def test_scope_inactive_returns_the_complement(self):
        envelope = _handle_list_mods(self.api, scope="inactive", fields=["package_id"], limit=1000)
        self.assertEqual(envelope["total"], 790 - len(self.api.active_ids))
        self.assertTrue(all(not item["is_active"] for item in envelope["items"]))

    def test_scope_combines_with_filter_and_pagination(self):
        envelope = _handle_list_mods(self.api, scope="active", filter="FAKE.MOD0001")
        self.assertEqual(envelope["total"], 1)
        self.assertTrue(envelope["items"][0]["is_active"])
        # 分页作用在过滤之后
        paged = _handle_list_mods(self.api, scope="inactive", limit=5, offset=0)
        self.assertEqual(len(paged["items"]), 5)
        self.assertTrue(all(not i["is_active"] for i in paged["items"]))

    def test_scope_invalid_value_is_causal_error(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_list_mods(self.api, scope="enabled")
        self.assertIn("scope", str(ctx.exception))

    def test_without_limit_returns_full_list(self):
        envelope = _handle_list_mods(self.api)
        self.assertEqual(envelope["total"], 790)
        self.assertEqual(len(envelope["items"]), 790)
        self.assertIsNone(envelope["next_offset"])

    def test_error_payload_passes_through(self):
        self.api.get_mod_list_core = lambda: {"status": "error", "message": "上下文不健康"}
        payload = _handle_list_mods(self.api)
        self.assertEqual(payload["status"], "error")


class TestGetLoadOrderAdapter(unittest.TestCase):
    """get_load_order 适配器：分页 + version_token 透传。"""

    def test_envelope_with_token(self):
        api = FakeApi()
        envelope = _handle_get_load_order(api, limit=1, offset=0)
        self.assertEqual(envelope["total"], 2)
        self.assertEqual(len(envelope["items"]), 1)
        self.assertEqual(envelope["items"][0]["package_id"], "ludeon.rimworld")
        self.assertEqual(envelope["next_offset"], 1)
        tail = _handle_get_load_order(api, limit=1, offset=1)
        self.assertIsNone(tail["next_offset"])  # 最后一页收尾
        self.assertEqual(envelope["version_token"]["active_hash"], "abc")
        self.assertIn("modify_time", envelope)

    def test_filter_by_name(self):
        api = FakeApi()
        envelope = _handle_get_load_order(api, filter="harmony")
        self.assertEqual(envelope["total"], 1)
        self.assertEqual(envelope["items"][0]["name"], "Harmony")

    # ---- fields 投影：取了 _project 就必须真的调用 --------------------------

    def test_fields_projection_keeps_envelope_extra(self):
        envelope = _handle_get_load_order(FakeApi(), fields=["package_id"])
        self.assertEqual(envelope["total"], 2)
        for item in envelope["items"]:
            self.assertEqual(set(item), {"package_id"})
        # version_token/file/modify_time 在返回结构顶层，投影管不到它们
        self.assertEqual(envelope["version_token"]["active_hash"], "abc")
        self.assertEqual(envelope["file"], "/tmp/ModsConfig.xml")
        self.assertIn("modify_time", envelope)

    def test_fields_projection_applies_after_filter(self):
        envelope = _handle_get_load_order(
            FakeApi(), filter="harmony", fields=["package_id"]
        )
        self.assertEqual(envelope["total"], 1)
        self.assertEqual(envelope["items"], [{"package_id": "brrainz.harmony"}])

    def test_unknown_fields_silently_dropped_like_list_mods(self):
        # _project 对未知字段是静默忽略（不报错、不保留其余字段）——两个工具必须同语义
        envelope = _handle_get_load_order(FakeApi(), fields=["nope"])
        self.assertEqual(envelope["items"], [{}, {}])
        listed = _handle_list_mods(FakeApi(mods=3), fields=["nope"], limit=3)
        for item in listed["items"]:
            self.assertEqual(set(item), {"is_active"})  # list_mods 额外补 is_active 标注


class TestGetGroupsPaging(unittest.TestCase):
    """get_groups：limit/offset 分页返回结构；不传 limit 保持全量（向后兼容）。"""

    GROUP_COUNT = 25

    def _api(self, groups=None):
        if groups is None:
            groups = [
                {
                    "group_id": f"g{i:02d}",
                    "name": f"分组{i:02d}",
                    "mod_ids": [f"fake.mod{i:04d}"],
                }
                for i in range(self.GROUP_COUNT)
            ]
        return SimpleNamespace(groups_get=lambda: {"status": "success", "data": groups})

    def test_without_limit_returns_full_list(self):
        # 向后兼容：不传 limit → 全量返回 + next_offset 为 None
        envelope = _handle_get_groups(self._api())
        self.assertEqual(envelope["total"], self.GROUP_COUNT)
        self.assertEqual(len(envelope["items"]), self.GROUP_COUNT)
        self.assertIsNone(envelope["next_offset"])
        self.assertIn("hint", envelope)

    def test_paging_envelope(self):
        envelope = _handle_get_groups(self._api(), limit=10, offset=5)
        self.assertEqual(envelope["total"], self.GROUP_COUNT)
        self.assertEqual(
            [group["group_id"] for group in envelope["items"]],
            [f"g{i:02d}" for i in range(5, 15)],
        )
        self.assertEqual(envelope["next_offset"], 15)
        self.assertIn("hint", envelope)
        last = _handle_get_groups(self._api(), limit=10, offset=20)
        self.assertEqual(len(last["items"]), 5)
        self.assertIsNone(last["next_offset"])

    def test_spec_exposes_paging_params(self):
        import inspect

        spec = next(s for s in TOOL_SPECS if s.name == "get_groups")
        sig = inspect.signature(spec.func)
        self.assertEqual(list(sig.parameters), ["limit", "offset"])
        self.assertIsNone(sig.parameters["limit"].default)
        self.assertEqual(sig.parameters["offset"].default, 0)
        self.assertIsNone(spec.method)  # 只走适配 handler，不在 _API_METHODS

    def test_error_payload_passes_through(self):
        api = SimpleNamespace(groups_get=lambda: {"status": "error", "message": "分组读取失败"})
        payload = _handle_get_groups(api, limit=5)
        self.assertEqual(payload["status"], "error")


class TestGetSettingsOverviewAdapter(unittest.TestCase):
    """get_settings_overview：package_id 定向过滤（缺省=全量透传，向后兼容）。"""

    @staticmethod
    def _payload() -> dict:
        return {
            "status": "success",
            "data": {
                "config_path": "C:/cfg",
                "total_files": 3,
                "matched_mod_count": 2,
                "unknown_file_count": 1,
                "cleanup_candidate_paths": [],
                "mod_groups": [
                    {
                        "package_id": "a.first.mod",
                        "mod_name": "First",
                        "status": "enabled",
                        "setting_groups": [{"files": [{"file_path": "C:/cfg/Mod_a.xml"}]}],
                    },
                    {
                        "package_id": "b.second.mod",
                        "mod_name": "Second",
                        "status": "disabled",
                        "setting_groups": [{"files": [{"file_path": "C:/cfg/Mod_b.xml"}]}],
                    },
                    {"package_id": "", "mod_name": "未知模组", "status": "unknown"},
                ],
            },
        }

    def _api(self, payload: dict | None = None):
        body = self._payload() if payload is None else payload
        return SimpleNamespace(mod_settings_get_overview=lambda: body)

    def test_without_package_id_passes_full_overview_through(self):
        payload = self._payload()
        result = _handle_get_settings_overview(self._api(payload))
        self.assertEqual(result, payload)  # 逐字段等价：不传 package_id → 原样透传全量响应
        self.assertEqual(len(result["data"]["mod_groups"]), 3)
        self.assertEqual(result["data"]["total_files"], 3)

    def test_package_id_filters_mod_groups_case_insensitively(self):
        result = _handle_get_settings_overview(self._api(), package_id="A.FIRST.MOD")
        self.assertEqual(result["status"], "success")
        data = result["data"]
        # 只回 config_path + 回显包名 + 命中组（扫描级统计量按全量计算，不随行）
        self.assertEqual(set(data), {"config_path", "package_id", "mod_groups"})
        self.assertEqual(data["package_id"], "A.FIRST.MOD")
        self.assertEqual(data["config_path"], "C:/cfg")
        self.assertEqual([g["package_id"] for g in data["mod_groups"]], ["a.first.mod"])
        # 条目本体（设置文件实例明细）原样保留
        self.assertEqual(
            data["mod_groups"][0]["setting_groups"][0]["files"][0]["file_path"],
            "C:/cfg/Mod_a.xml",
        )

    def test_unknown_package_id_returns_empty_groups_with_hint(self):
        data = _handle_get_settings_overview(self._api(), package_id="no.such.mod")["data"]
        self.assertEqual(data["mod_groups"], [])
        self.assertIn("hint", data)

    def test_error_payload_passes_through(self):
        payload = {"status": "error", "message": "读取模组设置总览失败"}
        result = _handle_get_settings_overview(self._api(payload), package_id="a.first.mod")
        self.assertEqual(result["status"], "error")

    def test_spec_is_handler_bound_with_package_id_param(self):
        import inspect

        import backend.mcp_server as mcp_server

        # 互斥规则：带适配参数的工具必须只走适配 handler，不进 _API_METHODS
        spec = next(s for s in TOOL_SPECS if s.name == "get_settings_overview")
        sig = inspect.signature(spec.func)
        self.assertEqual(list(sig.parameters), ["package_id"])
        self.assertEqual(sig.parameters["package_id"].default, "")
        self.assertIsNone(spec.method)
        self.assertIs(
            mcp_server._HANDLER_OVERRIDES["get_settings_overview"],
            _handle_get_settings_overview,
        )


class TestReadLogsAdapter(unittest.TestCase):
    """read_logs：分级/尾读/行范围过滤 + filename 缺省解析。"""

    def _api(self) -> FakeApi:
        api = FakeApi()

        def read_log_page(log_type, filename, page=1, page_size=1000, profile_scope="active"):
            blocks = [
                {"level": "ERROR", "timestamp": "2026-09-27 03:00:00.000",
                 "message": f"boom {i}", "details": "", "raw_lines": [i + 1]}
                for i in range((page - 1) * 10, (page - 1) * 10 + 10)
            ]
            blocks.append(
                {"level": "INFO", "timestamp": "2026-09-27 03:00:01.000",
                 "message": "fine", "details": "", "raw_lines": [999]}
            )
            return {
                "status": "success",
                "data": {"blocks": blocks, "has_more": page < 3, "total_pages": 3,
                         "current_page": page},
            }

        api.read_log_page = read_log_page
        api.get_log_files = lambda log_type="game", profile_scope="active": {
            "status": "success",
            "data": [{"name": "Player.log", "path": "/tmp/Player.log", "size": 1,
                      "mtime": "2026-09-27 03:00:00"}],
        }
        return api

    def test_levels_filter_and_envelope(self):
        envelope = _handle_read_logs(self._api(), levels=["error"], limit=5)
        self.assertEqual(len(envelope["items"]), 5)
        self.assertTrue(all(b["level"] == "ERROR" for b in envelope["items"]))
        self.assertEqual(envelope["filename"], "Player.log")
        self.assertIn("scanned_pages", envelope)

    def test_tail_early_stop_reads_fewer_pages(self):
        api = self._api()
        calls = []
        original = api.read_log_page

        def counting(*args, **kwargs):
            calls.append(args[2] if len(args) > 2 else kwargs.get("page"))
            return original(*args, **kwargs)

        api.read_log_page = counting
        envelope = _handle_read_logs(api, tail=3, levels=["error"])
        self.assertEqual(len(envelope["items"]), 3)
        self.assertEqual(envelope["next_offset"], None)  # 尾读不翻页
        self.assertLessEqual(len(calls), 2)  # 提前停止：不该扫满 3 页

    def test_line_range_filter(self):
        envelope = _handle_read_logs(self._api(), line_from=5, line_to=7, levels=["error"])
        # 匹配流恒为最新优先（页反转 + 页内倒序），行号因此降序
        self.assertEqual(
            [b["raw_lines"][0] for b in envelope["items"]], [7, 6, 5]
        )

    def test_invalid_since_is_causal_tool_error(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_read_logs(self._api(), since="not-a-time")
        parsed = json.loads(str(ctx.exception))
        self.assertEqual(parsed["status"], "error")
        self.assertIn("nextActions", parsed)

    def test_missing_log_file_is_causal(self):
        from fastmcp.exceptions import ToolError

        api = self._api()
        api.get_log_files = lambda *a, **k: {"status": "success", "data": []}
        with self.assertRaises(ToolError) as ctx:
            _handle_read_logs(api)
        self.assertIn("没有可读日志文件", str(ctx.exception))

    def test_page_size_is_fetch_batch_not_result_count(self):
        # 语义：page_size 只决定每次向后端要多少块（扫描批次），
        # 返回条数只由 limit/tail 决定——传 page_size=5 不会让结果变短。
        api = self._api()
        seen: list[int] = []
        original = api.read_log_page

        def spying(log_type, filename, page=1, page_size=1000, profile_scope="active"):
            seen.append(page_size)
            return original(log_type, filename, page, page_size, profile_scope)

        api.read_log_page = spying
        envelope = _handle_read_logs(api, page_size=5, levels=["error"], limit=3)
        self.assertEqual(len(envelope["items"]), 3)
        self.assertTrue(seen, "read_log_page 未被调用")
        self.assertEqual(seen[0], 5)  # page_size 确实传到了后端读取批次

    def test_tail_marks_paging_and_non_tail_does_not(self):
        # 尾读返回结构带 paging:"tail" + next_offset=None（一次性取尾，不可翻页）——
        # 语义层 _realign_page_offset 据此跳过重算，否则会把 None 复活成分页坐标；
        # 非 tail 绝不带 paging 键，否则正常翻页的重算也会被误跳过。
        tail_env = _handle_read_logs(self._api(), tail=3, levels=["error"])
        self.assertEqual(tail_env["paging"], "tail")
        self.assertIsNone(tail_env["next_offset"])

        page_env = _handle_read_logs(self._api(), levels=["error"], limit=5)
        self.assertNotIn("paging", page_env)


class TestApplyLoadOrderOps(unittest.TestCase):
    """save_load_order：token 保护、dry_run、全有全无、new_version_token 续链。"""

    TOKEN = {"path": "/tmp/ModsConfig.xml", "mtime_ms": 1, "size": 2, "active_hash": "h"}

    def setUp(self):
        self.api = FakeApi()
        self.api.load_order_mgr = FakeLoadOrderManager(
            ids=["ludeon.rimworld", "brrainz.harmony"]
        )

    def _call(self, **kwargs):
        kwargs.setdefault("base_version_token", dict(self.TOKEN))
        kwargs.setdefault("ops", [{"op": "move", "id": "brrainz.harmony", "to": {"index": 0}}])
        return _handle_apply_load_order_ops(self.api, **kwargs)

    def test_missing_token_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            _handle_apply_load_order_ops(self.api, ops=[{"op": "remove", "ids": ["x"]}])
        parsed = json.loads(str(ctx.exception))
        self.assertIn("base_version_token", parsed["message"])
        self.assertTrue(any("get_load_order" in a for a in parsed["nextActions"]))

    def test_stale_token_rejected_without_write(self):
        from fastmcp.exceptions import ToolError

        self.api.load_order_mgr.stale = True
        with self.assertRaises(ToolError) as ctx:
            self._call()
        parsed = json.loads(str(ctx.exception))
        self.assertIn("已被外部修改", parsed["message"])
        self.assertEqual(len(parsed["nextActions"]), 3)
        self.assertEqual(self.api.load_order_mgr.saved, [])

    def test_dry_run_validates_without_writing(self):
        result = self._call(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["moved"], 1)
        self.assertEqual(self.api.load_order_mgr.saved, [])

    def test_success_returns_new_token_and_applied(self):
        result = self._call()
        self.assertTrue(result["ok"])
        self.assertEqual(result["new_version_token"], {"active_hash": "new"})
        self.assertEqual(result["applied"], ["move brrainz.harmony -> index 0"])
        # 单次落盘：目标顺序与 token 续链
        saved_ids, saved_token = self.api.load_order_mgr.saved[0]
        self.assertEqual(saved_ids, ["brrainz.harmony", "ludeon.rimworld"])
        self.assertEqual(saved_token, self.TOKEN)

    def test_conflict_is_atomic_with_detail(self):
        from fastmcp.exceptions import ToolError

        ops = [
            {"op": "insert", "id": "already.there"},
            {"op": "remove", "ids": ["does.not.exist"]},
        ]
        self.api.load_order_mgr.ids.append("already.there")
        with self.assertRaises(ToolError) as ctx:
            self._call(ops=ops)
        parsed = json.loads(str(ctx.exception))
        self.assertIn("整体拒绝", parsed["message"])
        self.assertEqual(len(parsed["conflicts"]), 2)
        self.assertEqual(parsed["conflicts"][0]["index"], 0)
        self.assertEqual(self.api.load_order_mgr.saved, [])

    def test_structural_ops_misuse_is_causal(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            self._call(ops=[])
        self.assertIn("ops", str(ctx.exception))


class TestDependenciesHandler(unittest.TestCase):
    """依赖图工具的错误映射与透传。"""

    def test_result_passthrough(self):
        api = FakeApi()
        with mock.patch(
            "backend.mcp_deps.query_dependencies",
            return_value={"start": "a.b", "edges": [], "cycles": [], "total_edges": 0},
        ):
            result = _handle_get_mod_dependencies(
                api, packageId="a.b", direction="up", depth=2, format="edges"
            )
        self.assertEqual(result["start"], "a.b")

    def test_value_error_maps_to_causal_error(self):
        from fastmcp.exceptions import ToolError

        api = FakeApi()
        with mock.patch(
            "backend.mcp_deps.query_dependencies",
            side_effect=ValueError("未找到此模组: ghost.mod"),
        ):
            with self.assertRaises(ToolError) as ctx:
                _handle_get_mod_dependencies(api, packageId="ghost.mod")
        parsed = json.loads(str(ctx.exception))
        self.assertIn("ghost.mod", parsed["message"])
        self.assertTrue(any("search_mods" in a for a in parsed["nextActions"]))


class TestScanReadinessGate(unittest.TestCase):
    """首扫进行中且无数据 → 因果「初始化扫描中」，不给半成品。"""

    def _wrapper(self, api):
        import backend.mcp_server as mcp_server

        spec = next(s for s in TOOL_SPECS if s.name == "list_mods")
        handler = mcp_server._HANDLER_OVERRIDES[spec.name]
        return mcp_server._bind(spec, handler, api)

    def test_gated_while_first_scan_running(self):
        from fastmcp.exceptions import ToolError

        api = FakeApi()
        api.scanner = FakeModScanner(scanning=True)
        wrapper = self._wrapper(api)
        with mock.patch(
            "backend.database.dao.ModDAO.get_profile_mods", return_value=[]
        ):
            with self.assertRaises(ToolError) as ctx:
                wrapper()
        parsed = json.loads(str(ctx.exception))
        self.assertIn("初始化扫描中", parsed["message"])
        self.assertIn("nextActions", parsed)

    def test_served_when_data_present(self):
        api = FakeApi()
        api.scanner = FakeModScanner(scanning=True)
        with mock.patch(
            "backend.database.dao.ModDAO.get_profile_mods",
            return_value=[{"package_id": "x"}],
        ):
            payload = json.loads(self._wrapper(api)())
        self.assertEqual(payload["total"], 790)

    def test_served_when_idle(self):
        payload = json.loads(self._wrapper(FakeApi())())
        self.assertEqual(payload["total"], 790)


class TestUpstreamStatusContract(unittest.TestCase):
    """通用委托的上游 error/warning → isError+nextActions。

    需覆盖的场景：save_load_order 冲突（warning + 全量 disk_order
    被语义护栏截成不可读壳）、create_backup 参数错（error 直接返回）均未置 isError。
    """

    def _wrapper(self, payload, name="save_load_order"):
        import backend.mcp_server as mcp_server

        spec = next(s for s in TOOL_SPECS if s.name == name)

        def handler(api, **kwargs):  # noqa: ANN001 - _invoke 传入
            return payload

        return mcp_server._bind(spec, handler, FakeApi())

    def _call_err(self, payload, name="save_load_order"):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as ctx:
            self._wrapper(payload, name)()
        return json.loads(str(ctx.exception))

    def test_error_becomes_causal_error(self):
        parsed = self._call_err({"status": "error", "message": "另存备份失败"})
        self.assertEqual(parsed["status"], "error")
        self.assertIn("另存备份失败", parsed["message"])
        self.assertTrue(parsed["nextActions"])

    def test_warning_drops_huge_data_but_keeps_causality(self):
        parsed = self._call_err(
            {
                "status": "warning",
                "message": "磁盘加载顺序已被外部修改，请先处理冲突。",
                "data": {"status": "conflict", "disk_order": [{"x": "y" * 100}]},
            }
        )
        text = json.dumps(parsed, ensure_ascii=False)
        self.assertNotIn("disk_order", text)
        self.assertIn(
            "version_token",
            json.dumps(parsed["nextActions"], ensure_ascii=False),
        )
        self.assertIn("磁盘加载顺序", parsed["message"])

    def test_success_passthrough(self):
        payload = json.loads(self._wrapper({"status": "success", "data": {"ok": 1}})())
        self.assertEqual(payload["status"], "success")

    def test_business_status_passthrough(self):
        # wait_event 的 timeout 是业务状态，不是错误
        payload = json.loads(self._wrapper({"status": "timeout"}, name="wait_event")())
        self.assertEqual(payload["status"], "timeout")

    # ---- 通用错误响应（未处理异常）→ 从 detail.traceback 提取单行真实错误原因 ----

    def _shell_payload(self, traceback: str | None, *, code: str = "API.CALL.UNHANDLED_EXCEPTION") -> dict:
        """复刻 backend/api.py log_api_call 吞异常后的通用错误响应结构。

        仅用于**合成边界形态**（末行非异常行等）；常规用例改走下面的
        ``_real_shell_payload``（真实产物），api.py 改字段名时本文件同样会红。
        """
        payload = {
            "status": "error",
            "message": "API 调用发生未处理异常",
            "user_message": "操作未完成。软件内部接口执行异常，请稍后重试或重启软件。",
            "error_type": "API",
            "error_code": code,
        }
        if traceback is not None:
            payload["detail"] = {"traceback": traceback, "exception_type": "ValueError"}
        return payload

    def _real_shell_payload(self, detail=None) -> dict:
        """真实产物：复现 api.py log_api_call 吞异常时的那次 ``ApiResponse.error`` 调用。

        返回结构（字段名 / 文案 / error_code 派生）由 api.py + error_contract **现场生成**，
        不手工复制——api.py 改字段名会让本类用例直接变红。只复制调用实参（code/message，
        即 api.py 侧冻结的字面量），两者与常量的对齐由
        ``test_real_shell_payload_matches_detector_contract`` 锁定。
        ``ApiResponse.error`` 是 classmethod，纯类方法调用：不需要 API 实例、不碰 DB。
        """
        from backend.api import ApiResponse

        return ApiResponse.error(
            "API 调用发生未处理异常",
            code="API.CALL.UNHANDLED_EXCEPTION",
            detail=detail,
        )

    def test_upstream_direct_cause_gets_business_actions(self):
        """主路径：api.py 装饰器对 ValueError 保留 message（非壳）→ 真因果直传 MCP。

        错误原因来源（见 _raise_upstream_failure docstring）：message 就是因果，不从 traceback 里提取；
        nextActions 必须按业务特征分流，不得误导去核对 schema。
        """
        payload = {
            "status": "error",
            "message": "删除分组失败：目标分组不存在。",
            "user_message": "请求参数无效。请检查配置、输入内容或接口兼容性。",
            "error_type": "SYSTEM",
            "error_code": "SYSTEM.INVALID_INPUT",
        }
        parsed = self._call_err(payload, name="delete_group")
        self.assertEqual(parsed["message"], "删除分组失败：目标分组不存在。")
        actions = json.dumps(parsed["nextActions"], ensure_ascii=False)
        self.assertNotIn("schema", actions)  # schema 根本没错，不得误导
        self.assertIn("get_groups", actions)  # 指向「先查现状再重试」

    def test_generic_shell_unwraps_single_line_cause(self):
        # 真实产物：在与 api.py 同款的 except 分支里 raise → 拿到带帧的真实 traceback
        try:
            raise ValueError("删除分组失败：目标分组不存在。")
        except ValueError as exc:
            payload = self._real_shell_payload(exc)
        parsed = self._call_err(payload, name="delete_group")
        self.assertIn("ValueError: 删除分组失败：目标分组不存在。", parsed["message"])
        self.assertIn("API.CALL.UNHANDLED_EXCEPTION", parsed["message"])
        # 防注入 + 限制体积：只回单行，堆栈任何一行都不能外泄
        self.assertNotIn("\n", parsed["message"])
        self.assertNotIn("Traceback", parsed["message"])
        self.assertNotIn("dao.py", parsed["message"])
        # 真实帧来自本测试文件，同样不得外泄（若手工复制 traceback，这条例子锁不住）
        self.assertNotIn("test_mcp_server.py", parsed["message"])
        self.assertNotIn("raise ValueError", parsed["message"])
        # 业务错误 → 指向「先查现状」，不再误导 agent 去核对 schema
        actions = json.dumps(parsed["nextActions"], ensure_ascii=False)
        self.assertNotIn("schema", actions)
        self.assertIn("get_groups", actions)

    def test_real_shell_payload_matches_detector_contract(self):
        """三方一致性：api.py 真实产物 ↔ error_contract 常量 ↔ mcp_server 检测器。

        直接用真实返回值比对 MCP 检测器读的键与判据常量——api.py 改字段名、
        或任一侧常量漂移，本用例都会先变红。
        """
        import backend.mcp_server as mcp_server
        from backend.utils.error_contract import (
            UNHANDLED_EXCEPTION_CODE,
            UNHANDLED_EXCEPTION_MESSAGE,
        )

        payload = self._real_shell_payload(ValueError("boom"))
        for key in ("status", "message", "user_message", "error_code", "detail"):
            self.assertIn(key, payload)
        self.assertEqual(payload["message"], UNHANDLED_EXCEPTION_MESSAGE)
        self.assertEqual(payload["error_code"], UNHANDLED_EXCEPTION_CODE)
        self.assertEqual(mcp_server._GENERIC_SHELL_CODE, payload["error_code"])
        self.assertIn(mcp_server._GENERIC_SHELL_TEXT, payload["message"])
        self.assertIn("traceback", payload["detail"])

    def test_cause_line_truncated_to_300_chars(self):
        try:
            raise ValueError("x" * 500)
        except ValueError as exc:
            payload = self._real_shell_payload(exc)
        parsed = self._call_err(payload, name="update_group")
        cause = parsed["message"].split("（上游", 1)[0]
        self.assertEqual(len(cause), 300)

    def test_missing_traceback_keeps_legacy_behavior(self):
        # 提取失败（真实产物不带 detail）→ 不能比现在更差：文案与 nextActions 逐字保持原样
        parsed = self._call_err(self._real_shell_payload(None), name="update_group")
        self.assertEqual(parsed["message"], "API 调用发生未处理异常")
        self.assertIn("schema", json.dumps(parsed["nextActions"], ensure_ascii=False))

    def test_non_exception_last_line_keeps_legacy_behavior(self):
        # 末行不是 `SomeError: 消息`（多行消息续行）→ 不猜、不外传
        traceback = (
            "Traceback (most recent call last):\n"
            "ValueError: 删除分组失败：目标分组不存在。\n"
            "- 附加说明行\n"
        )
        parsed = self._call_err(self._shell_payload(traceback), name="update_group")
        self.assertEqual(parsed["message"], "API 调用发生未处理异常")

    def test_version_token_branch_keeps_priority_over_cause_extraction(self):
        payload = self._shell_payload("ValueError: 磁盘加载顺序已被外部修改\n")
        payload["message"] = "磁盘加载顺序已被外部修改，base_version_token 过期"
        parsed = self._call_err(payload, name="save_load_order")
        self.assertIn("磁盘加载顺序", parsed["message"])  # 文案未被错误原因改写
        self.assertNotIn("ValueError", parsed["message"])
        actions = json.dumps(parsed["nextActions"], ensure_ascii=False)
        self.assertIn("version_token", actions)


class TestListProfilesProjection(unittest.TestCase):
    """档案列表默认剥离整序数组（单档案实测 ~130KB，返回体积要小）。"""

    def test_heavy_order_arrays_stripped_others_kept(self):
        api = FakeApi()
        api.profiles_get = lambda: {
            "status": "success",
            "data": [
                {
                    "id": "p1",
                    "name": "元神",
                    "inactive_mods_order": [f"fake.mod{i:05d}" for i in range(790)],
                    "temp_mods_order": [f"temp.mod{i:05d}" for i in range(790)],
                    "run_commands": [],
                    "msg": "",
                }
            ],
        }
        envelope = _handle_list_profiles(api)
        self.assertEqual(envelope["total"], 1)
        profile = envelope["items"][0]
        self.assertNotIn("inactive_mods_order", profile)
        self.assertNotIn("temp_mods_order", profile)
        self.assertEqual(profile["id"], "p1")
        self.assertEqual(profile["name"], "元神")
        # 体积断言：剥离后单档案条目应远小于整序数组本体
        self.assertLess(len(json.dumps(profile, ensure_ascii=False).encode()), 1024)

    def test_non_dict_entries_pass_through(self):
        api = FakeApi()
        api.profiles_get = lambda: {"status": "success", "data": ["odd-entry"]}
        envelope = _handle_list_profiles(api)
        self.assertEqual(envelope["items"], ["odd-entry"])


class TestStdioIsolation(unittest.TestCase):
    """isolate_stdout 只认领控制台流，且认领记录可跨「stderr 重建」重入。"""

    def setUp(self):
        import logging
        import tempfile

        from backend.mcp_stdio import _CLAIMED_CONSOLE_HANDLERS

        self.logging = logging
        self._claimed_backup = list(_CLAIMED_CONSOLE_HANDLERS)
        _CLAIMED_CONSOLE_HANDLERS.clear()
        self.logger = logging.getLogger("mcp-iso-probe")
        self.logger.propagate = False
        self.logger.handlers.clear()
        self._tmp_files: list[str] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from pathlib import Path

        from backend.mcp_stdio import _CLAIMED_CONSOLE_HANDLERS

        self.logger.handlers.clear()
        _CLAIMED_CONSOLE_HANDLERS.clear()
        _CLAIMED_CONSOLE_HANDLERS.extend(self._claimed_backup)
        for name in self._tmp_files:
            Path(name).unlink(missing_ok=True)

    def _make_file_handler(self):
        import tempfile

        path = tempfile.NamedTemporaryFile(delete=False, suffix=".log").name
        self._tmp_files.append(path)
        handle = open(path, "w", encoding="utf-8")
        self.addCleanup(handle.close)
        return self.logging.StreamHandler(handle)

    def test_claims_stdout_console_handler_only(self):
        import sys

        from backend.mcp_stdio import isolate_stdout

        console = self.logging.StreamHandler(sys.stdout)
        file_handler = self._make_file_handler()
        file_stream_before = file_handler.stream
        self.logger.addHandler(console)
        self.logger.addHandler(file_handler)
        isolate_stdout(self.logger)
        self.assertIs(console.stream, sys.stderr)
        self.assertIs(file_handler.stream, file_stream_before)  # 文件流绝不被劫持

    def test_reclaim_after_stderr_swap(self):
        """认领过的 handler 在「stderr 被重建为新对象」后再 isolate 必须换到新对象。"""
        import sys

        from backend.mcp_stdio import isolate_stdout

        console = self.logging.StreamHandler(sys.stdout)
        self.logger.addHandler(console)
        isolate_stdout(self.logger)
        self.assertIs(console.stream, sys.stderr)
        # 宿主在认领后替换过 stderr 对象：handler 指到旧目标后，
        # 第二次认领必须真实发生（重指逻辑为防御而设）。
        placeholder = type("FakeOldStderr", (), {"buffer": None})()
        console.stream = placeholder
        isolate_stdout(self.logger)
        self.assertIs(console.stream, sys.stderr)

    def test_ensure_protocol_stdio_reports_status_without_win32_rebuild(self):
        """验证 ``ensure_protocol_stdio`` 当前行为：只报告状态，不做 Win32 句柄重建。

        不存在冻结窗口化 stdio exe 的入口（pack_*.py 对 mcp_server 零引用），
        Win32 句柄重建无适用场景。本测试同时守住
        「占位流不被原地改写」这一行为。
        """
        import io
        import os

        from backend.mcp_stdio import ensure_protocol_stdio

        class _WithBuffer:
            buffer = object()

        saved_in, saved_out = sys.stdin, sys.stdout
        try:
            sys.stdin = sys.stdout = _WithBuffer()
            self.assertEqual(ensure_protocol_stdio(), "inplace")

            placeholder = io.StringIO()  # 无 .buffer 的占位流
            sys.stdin = sys.stdout = placeholder
            expected = "unsupported" if os.name != "nt" else "no-handle"
            self.assertEqual(ensure_protocol_stdio(), expected)
            # 占位流原样保留，不再重建
            self.assertIs(sys.stdout, placeholder)
        finally:
            sys.stdin, sys.stdout = saved_in, saved_out


class TestErrorContract(unittest.TestCase):
    """意外异常 → isError + 因果文案（不外泄第三方内容）+ nextActions。"""

    def test_unexpected_exception_is_masked_and_causal(self):
        import backend.mcp_server as mcp_server
        from fastmcp.exceptions import ToolError

        spec = next(s for s in TOOL_SPECS if s.name == "list_mods")

        def boom(api, **kwargs):
            raise RuntimeError("secret-third-party-detail")

        wrapper = mcp_server._bind(spec, boom, FakeApi())
        with self.assertRaises(ToolError) as ctx:
            wrapper()
        text = str(ctx.exception)
        parsed = json.loads(text)
        self.assertEqual(parsed["status"], "error")
        self.assertNotIn("secret-third-party-detail", text)
        self.assertIn("nextActions", parsed)


class TestAIHandlerBridge(unittest.TestCase):
    """AI 桥接：参数序列化、日志源注入、错误状态映射。"""

    def test_search_mods_arguments_serialized(self):
        import backend.mcp_server as mcp_server

        spec = next(s for s in TOOL_SPECS if s.name == "search_mods")
        api = FakeApi()
        result = SimpleNamespace(
            ok=True, error="", summary="已找到 2 个候选模组",
            data={"matched": [{"package_id": "a"}, {"package_id": "b"}]},
        )
        with mock.patch("backend.ai.ai_tools.AIToolExecutor") as executor_cls:
            executor_cls.return_value.execute_structured.return_value = result
            wrapper = mcp_server._bind(spec, mcp_server._HANDLER_OVERRIDES[spec.name], api)
            payload = json.loads(wrapper(keyword="ratkin", limit=5))
        name, arguments_str = executor_cls.return_value.execute_structured.call_args[0]
        self.assertEqual(name, "search_mods")
        self.assertEqual(json.loads(arguments_str), {"keyword": "ratkin", "limit": 5})
        self.assertEqual(payload["status"], "success")
        self.assertEqual(len(payload["data"]["matched"]), 2)

    def test_log_context_pops_source_params_into_payload(self):
        import backend.mcp_server as mcp_server

        spec = next(s for s in TOOL_SPECS if s.name == "get_log_context")
        api = FakeApi()
        api.game_log_mgr = object()
        result = SimpleNamespace(ok=True, error="", summary="", data={"representative_line": 42})
        with mock.patch("backend.ai.ai_tools.AIToolExecutor") as executor_cls:
            executor_cls.return_value.execute_structured.return_value = result
            wrapper = mcp_server._bind(spec, mcp_server._HANDLER_OVERRIDES[spec.name], api)
            payload = json.loads(wrapper(target_line=42, log_type="game", filename="Player.log"))
        _context, payload_arg, reader = executor_cls.call_args[0]
        self.assertEqual(payload_arg, {"log_source_type": "game", "filename": "Player.log"})
        self.assertIs(reader, api.game_log_mgr)
        name, arguments_str = executor_cls.return_value.execute_structured.call_args[0]
        self.assertEqual(json.loads(arguments_str), {"target_line": 42})
        self.assertEqual(payload["status"], "success")

    def test_log_context_app_reader_is_module_singleton(self):
        """app 日志读取器是 backend.utils.logger 的模块级单例，不是 API 属性。

        getattr(api, "app_log_reader", None) 恒为 None → get_log_context
        {log_type:"app"]} 必然报「不支持按行反查」；reader 必须是模块单例。
        """
        import backend.mcp_server as mcp_server
        from backend.utils.logger import app_log_reader

        spec = next(s for s in TOOL_SPECS if s.name == "get_log_context")
        api = FakeApi()  # 故意不提供任何 app_log_reader 属性（真实 API 也没有）
        result = SimpleNamespace(ok=True, error="", summary="", data={"representative_line": 7})
        with mock.patch("backend.ai.ai_tools.AIToolExecutor") as executor_cls:
            executor_cls.return_value.execute_structured.return_value = result
            wrapper = mcp_server._bind(spec, mcp_server._HANDLER_OVERRIDES[spec.name], api)
            payload = json.loads(wrapper(target_line=7, log_type="app", filename="app.log"))
        reader = executor_cls.call_args[0][2]
        self.assertIs(reader, app_log_reader)
        self.assertEqual(payload["status"], "success")

    def test_builtin_error_dict_maps_to_causal_error(self):
        # 上游 error 不直接透传，统一转为 isError + nextActions，映射语义保留在 ToolError 文本里。
        from fastmcp.exceptions import ToolError

        import backend.mcp_server as mcp_server

        spec = next(s for s in TOOL_SPECS if s.name == "get_mod_rules")
        result = SimpleNamespace(
            ok=True, error="", summary="", data={"error": "未找到此模组: ghost"}
        )
        with mock.patch("backend.ai.ai_tools.AIToolExecutor") as executor_cls:
            executor_cls.return_value.execute_structured.return_value = result
            wrapper = mcp_server._bind(spec, mcp_server._HANDLER_OVERRIDES[spec.name], FakeApi())
            with self.assertRaises(ToolError) as ctx:
                wrapper(package_id="ghost")
        payload = json.loads(str(ctx.exception))
        self.assertEqual(payload["status"], "error")
        self.assertIn("未找到此模组", payload["message"])
        self.assertTrue(payload["nextActions"])


class TestWaitEventBridge(unittest.TestCase):
    """wait_event：事件 tap 记录 + 原样转发 + 还原（零改 backend/utils）。

    全套测试里既有用例会改写 ``EventBus.emit``（如 ai_assistant_runtime 直接赋值
    lambda 再还原成绑定方法），因此本类**自带 spy**：先接管 emit 再装 tap，断言
    完全自洽，与执行顺序无关；tearDown 逐位还原进入前的状态。
    """

    def setUp(self):
        from backend.utils.event_bus import EventBus

        restore_event_tap()  # 防御：清掉任何遗留 tap
        self.EventBus = EventBus
        self._saved_emit = EventBus.__dict__.get("emit")
        self.delegated: list[tuple] = []

        def spy(event_name, data=None):
            self.delegated.append((event_name, data))

        EventBus.emit = spy  # type: ignore[method-assign]
        self._pending_backup = list(EventBus._pending_events)
        EventBus._pending_events.clear()

    def tearDown(self):
        restore_event_tap()
        if self._saved_emit is not None:
            self.EventBus.emit = self._saved_emit  # type: ignore[method-assign]
        self.EventBus._pending_events[:] = self._pending_backup

    def test_tap_records_and_delegates(self):
        self.assertTrue(install_event_tap())
        self.assertFalse(install_event_tap())  # 幂等
        # 等待开始前入队的事件会被丢弃 → 必须在等待期间发射才命中
        timer = threading.Timer(
            0.05, self.EventBus.emit, args=("scan-complete", {"status": "success"})
        )
        timer.start()
        try:
            payload = _handle_wait_event(FakeApi(), event_name="scan-complete", timeout=3.0)
        finally:
            timer.join(timeout=2)
        # 委托保留：原 emit（此处为 spy）被原样调用 → GUI 行为不变
        self.assertEqual(self.delegated, [("scan-complete", {"status": "success"})])
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["data"]["event"], "scan-complete")

    def test_stale_event_queued_before_wait_is_discarded(self):
        # 调用方全程未触发扫描时，历史 scan-complete 不能被当成交付命中
        install_event_tap()
        self.EventBus.emit("scan-complete", {"status": "success", "seq": "stale"})
        payload = _handle_wait_event(FakeApi(), event_name="scan-complete", timeout=0.3)
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(payload["data"], {"event": "scan-complete"})

    def test_event_arriving_during_wait_is_delivered(self):
        install_event_tap()
        timer = threading.Timer(
            0.05, self.EventBus.emit, args=("scan-complete", {"status": "success"})
        )
        timer.start()
        try:
            payload = _handle_wait_event(FakeApi(), event_name="scan-complete", timeout=3.0)
        finally:
            timer.join(timeout=2)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["data"]["event"], "scan-complete")

    def test_zero_timeout_consumes_nothing(self):
        # timeout<=0 是即刻超时的空轮询：不应消费队列里的既有事件
        import queue as queue_mod

        from backend.mcp_server import _EVENT_QUEUE

        install_event_tap()
        while True:  # 防御：清掉任何用例遗留，保证本断言确定
            try:
                _EVENT_QUEUE.get_nowait()
            except queue_mod.Empty:
                break
        self.EventBus.emit("scan-complete", {"status": "success"})
        payload = _handle_wait_event(FakeApi(), event_name="scan-complete", timeout=0)
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(_EVENT_QUEUE.qsize(), 1)  # 陈旧事件原封未动

    def test_timeout_status(self):
        install_event_tap()
        payload = _handle_wait_event(
            FakeApi(), event_name="never-emitted", timeout=0.2
        )
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(payload["data"], {"event": "never-emitted"})

    def test_restore_puts_back_exact_previous_object(self):
        spy_before = self.EventBus.__dict__["emit"]
        install_event_tap()
        self.assertIsNot(self.EventBus.__dict__["emit"], spy_before)
        restore_event_tap()
        self.assertIs(self.EventBus.__dict__["emit"], spy_before)  # 逐位还原


class TestPageEnvelope(unittest.TestCase):
    def test_next_offset_semantics(self):
        env = page_envelope([1, 2], total=10, offset=4, limit=2)
        self.assertEqual(env["next_offset"], 6)
        end = page_envelope([9, 10], total=10, offset=8, limit=2)
        self.assertIsNone(end["next_offset"])
        self.assertEqual(end["total"], 10)


class TestActiveTokenNormalization(unittest.TestCase):
    """active_load_order 条目是包 token（带 _steam/_local 后缀），
    须经 parse_package_token 归一后再与 package_id 比对——直接比字符串会让
    is_active 对全部联接/本地模组恒为 False。"""

    def test_is_active_matches_suffixed_tokens(self):
        api = FakeApi(mods=5)
        api.active_ids = ["fake.mod0000_steam", "fake.mod0001_local", "FAKE.MOD0002"]
        envelope = _handle_list_mods(api, limit=10)
        flagged = {item["package_id"] for item in envelope["items"] if item["is_active"]}
        self.assertEqual(flagged, {"fake.mod0000", "fake.mod0001", "fake.mod0002"})

    def test_is_active_false_for_inactive_mods(self):
        api = FakeApi(mods=5)
        api.active_ids = ["fake.mod0000_steam"]
        envelope = _handle_list_mods(api, limit=10)
        flagged = {item["package_id"] for item in envelope["items"] if item["is_active"]}
        self.assertEqual(flagged, {"fake.mod0000"})


class TestGuardPageOffset(unittest.TestCase):
    """分页返回结构的 items 被截后重算 next_offset，翻页不跳条目。"""

    def _items(self, count: int) -> list[dict]:
        return [{"package_id": f"mod{i:05d}", "pad": "x" * 40} for i in range(count)]

    def test_next_offset_realigned_when_limit_given(self):
        # 真实返回结构形态：items 是页切片（limit=100 的第 1 页），next_offset = offset + len(page)
        full = self._items(300)
        payload = {"total": 300, "items": full[:100], "next_offset": 100, "hint": "翻页"}
        parsed = json.loads(guard_payload(payload, max_bytes=4096))
        self.assertTrue(parsed[TRUNCATED_KEY])
        kept = len(parsed["items"])
        self.assertLess(kept, 100)
        self.assertEqual(parsed["next_offset"], kept)  # offset(0) + kept
        # 翻页起点恰好是被截掉的第一条：无重叠、无跳漏
        self.assertEqual(full[kept]["package_id"], full[parsed["next_offset"]]["package_id"])

    def test_next_offset_realigned_when_no_limit(self):
        items = self._items(300)
        payload = {"total": 300, "items": items, "next_offset": None, "hint": "翻页"}
        parsed = json.loads(guard_payload(payload, max_bytes=4096))
        kept = len(parsed["items"])
        self.assertEqual(parsed["next_offset"], kept)  # offset = total - original_len = 0
        self.assertEqual(parsed["total_items"], 300)

    def test_last_page_truncation_points_at_tail(self):
        # 不含 paging 字段的末页：next_offset=None 是「本页已是最后」而非「尾读约定」，
        # 截断后必须复活成翻页坐标（不能被 tail 例外误伤）。
        items = self._items(300)
        payload = {"total": 300, "items": items[250:], "next_offset": None, "hint": "翻页"}
        parsed = json.loads(guard_payload(payload, max_bytes=2048))
        kept = len(parsed["items"])
        self.assertLess(kept, 50)
        self.assertEqual(parsed["next_offset"], 250 + kept)
        self.assertEqual(parsed["items"][0]["package_id"], "mod00250")

    def test_tail_envelope_keeps_next_offset_none(self):
        """read_logs 尾读返回结构（paging="tail"）截断后 next_offset 恒为 None。

        尾读的 None 是约定（一次性取尾、不再翻页），与「末页被截」的 None 在载荷上
        无法区分，靠工具层补的 paging 自描述字段消歧；该字段对 agent 是自描述信息，
        护栏保留不剥。
        """
        items = self._items(300)
        payload = {
            "total": 300,
            "items": items,
            "next_offset": None,
            "hint": "翻页",
            "paging": "tail",
            "filename": "Player.log",
        }
        parsed = json.loads(guard_payload(payload, max_bytes=2048))
        self.assertTrue(parsed[TRUNCATED_KEY])
        kept = len(parsed["items"])
        self.assertGreater(kept, 0)
        self.assertLess(kept, 300)  # 真的被截过，才谈得上「不许复活」
        self.assertIsNone(parsed["next_offset"])
        self.assertEqual(parsed["paging"], "tail")
        self.assertEqual(parsed["filename"], "Player.log")
        # 被截条数仍如实上报（单字段形态：int total_items）
        self.assertEqual(parsed["total_items"], 300)

    def test_read_logs_tail_through_real_handler_stays_none(self):
        """两侧约定的集成校验：真实 ``_handle_read_logs(tail=…)`` 载荷过护栏后仍不复活。

        证明 read_logs 确实走 ``_invoke → guard_payload`` 这条路，且工具层补的
        ``paging: "tail"`` 能一路传到 :func:`_realign_page_offset`。
        """
        api = FakeApi(mods=1)

        def read_log_page(log_type, filename, page=1, page_size=1000, profile_scope="active"):
            blocks = [
                {
                    "level": "ERROR",
                    "timestamp": "2026-09-27 03:00:00.000",
                    "message": f"boom {i}",
                    "details": "",
                    "raw_lines": [i + 1],
                }
                for i in range((page - 1) * 10, (page - 1) * 10 + 10)
            ]
            return {
                "status": "success",
                "data": {
                    "blocks": blocks,
                    "has_more": page < 3,
                    "total_pages": 3,
                    "current_page": page,
                },
            }

        api.read_log_page = read_log_page
        envelope = _handle_read_logs(api, filename="Player.log", tail=5)
        self.assertIsNone(envelope["next_offset"])
        self.assertEqual(envelope["paging"], "tail")
        original = len(envelope["items"])
        self.assertGreater(original, 0)

        parsed = json.loads(guard_payload(envelope, max_bytes=600))
        self.assertTrue(parsed[TRUNCATED_KEY])
        kept = len(parsed["items"])
        self.assertGreater(kept, 0)
        self.assertLess(kept, original)  # 真的截过，才验证得了 realign 分支
        self.assertIsNone(parsed["next_offset"])  # ← 尾读约定
        self.assertEqual(parsed["paging"], "tail")


class TestGuardMultiListTruncation(unittest.TestCase):
    """多个大列表同时超时时按路径迭代截断（不整体退化成只含通用错误信息的响应）。

    预算策略：候选 = **有界深度的列表路径**（顶层 key 与 ``data.all_mods`` 这类
    ≤3 段路径），按初始字节占用从大到小依次分配（大字段优先拿最长前缀），未处理
    字段按「将置空」计；小列表/标量不主动截断。路径字符串同时作
    ``total_items`` / ``truncated_fields`` 的键。
    """

    def test_two_oversized_lists_get_iterative_truncation(self):
        payload = {
            "big": [{"n": i, "pad": "x" * 80} for i in range(200)],
            "long": [{"n": i, "pad": "y" * 60} for i in range(150)],
            "scalar": "ok",
        }
        cap = 2048
        text = guard_payload(payload, max_bytes=cap)
        self.assertLessEqual(len(text.encode("utf-8")), cap)
        parsed = json.loads(text)  # 恒合法 JSON
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertEqual(parsed["total_items"], {"big": 200, "long": 150})
        self.assertEqual(parsed["truncated_fields"], ["big", "long"])
        self.assertGreater(len(parsed["big"]), 0)  # 大字段优先拿预算，保留非空前缀
        self.assertEqual(parsed["scalar"], "ok")

    @staticmethod
    def _initial_data_payload() -> dict:
        """get_initial_data 真实形状：ApiResponse.success() 包裹，列表全在 data.* 下。"""
        return {
            "status": "success",
            "message": "载入完成",
            "data": {
                "app_version": "0.24.7",
                "all_mods": [{"n": i, "pad": "x" * 90} for i in range(200)],
                "disabled_mods": [{"n": i, "pad": "y" * 90} for i in range(200)],
                "groups": [{"g": i} for i in range(10)],
                "settings": {"theme": "dark"},
            },
        }

    def test_primary_list_keeps_data_where_single_path_gave_up(self):
        """get_initial_data 截断：顶层无列表，截断必须打到 data.* 路径。

        真实载荷 ``{status, message, data:{all_mods, …}}`` 走不到任何顶层列表，
        只报字段名会退化成 151B 的空响应；本测试断言 data 子树保留真实数据、
        键是路径字符串。
        """
        payload = self._initial_data_payload()
        cap = 8192
        text = guard_payload(payload, max_bytes=cap)
        self.assertLessEqual(len(text.encode("utf-8")), cap)
        parsed = json.loads(text)
        self.assertTrue(parsed[TRUNCATED_KEY])
        # 路径字符串作键
        self.assertEqual(
            parsed["total_items"], {"data.all_mods": 200, "data.disabled_mods": 200}
        )
        self.assertEqual(
            parsed["truncated_fields"], ["data.all_mods", "data.disabled_mods"]
        )
        # 顶层返回结构字段与 data 子树（不是只报字段名的空响应）
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["message"], "载入完成")
        data = parsed["data"]
        self.assertEqual(data["app_version"], "0.24.7")
        self.assertEqual(data["settings"], {"theme": "dark"})
        self.assertGreater(len(data["all_mods"]), 0)
        # 预算按「字节降序、大字段先拿满」分配（既有策略）：主字段吃完预算后
        # 次字段可能只剩 0 条，但路径必须在 total_items/truncated_fields 里如实上报
        self.assertIsInstance(data["disabled_mods"], list)
        # 元素边界：保留的是原列表真实前缀
        self.assertEqual(
            data["all_mods"], payload["data"]["all_mods"][: len(data["all_mods"])]
        )
        # 粗筛只选「凑够超限额」的最少路径：小列表 groups 不进预算，原样保留
        self.assertEqual(len(data["groups"]), 10)

    def test_nested_single_big_list_reports_path_key(self):
        """嵌套载荷只有一条大列表时同样走路径键（不会退化成 int 或空响应）。"""
        payload = {"status": "success", "data": {"all_mods": [{"n": i, "pad": "x" * 90} for i in range(200)]}}
        text = guard_payload(payload, max_bytes=4096)
        parsed = json.loads(text)
        self.assertLessEqual(len(text.encode("utf-8")), 4096)
        self.assertEqual(parsed["total_items"], {"data.all_mods": 200})
        self.assertEqual(parsed["truncated_fields"], ["data.all_mods"])
        self.assertGreater(len(parsed["data"]["all_mods"]), 0)

    def test_nested_payload_all_lists_emptied_falls_back(self):
        """嵌套载荷全是标量超限 → 仍回落 _minimal_fallback。"""
        payload = {"status": "success", "data": {"blob": "q" * 3000}}
        parsed = json.loads(guard_payload(payload, max_bytes=512))
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertIn("data", parsed.get("truncated_fields", []))

    def test_paths_deeper_than_cap_are_not_traversed(self):
        """深度上限（路径 ≤3 段）之外不遍历 → 最小返回结构（防指数膨胀的边界）。"""
        payload = {"a": {"b": {"c": {"d": [{"n": i} for i in range(200)]}}}}
        parsed = json.loads(guard_payload(payload, max_bytes=512))
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertEqual(parsed["truncated_fields"], ["a"])

    def test_all_lists_emptied_still_over_limit_falls_back(self):
        payload = {"blob": "q" * 3000, "big": [{"n": i, "pad": "x" * 80} for i in range(50)]}
        parsed = json.loads(guard_payload(payload, max_bytes=512))
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertIn("blob", parsed.get("truncated_fields", []))


class TestSetModRule(unittest.TestCase):
    """set_mod_rule：单条用户规则合并写入/删除（AI MOD_RULE 动作的同源转发）。

    核心义务：update_user_mod_rule 是整包替换语义，适配层必须先读后合并，
    只动 relation 指向的一条，绝不丢该模组的其它既有规则。
    """

    def _api(self, rules: dict | None = None):
        import backend.mcp_handlers_features as mcp_handlers_features

        self._features = mcp_handlers_features
        api = FakeApi()
        self._current_rules = rules if rules is not None else {}
        api.sorter = SimpleNamespace(rule_mgr=SimpleNamespace(user_mod_rules=self._current_rules))
        self.saved: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        api.rule_update_user_mod = self._record_update
        api.rule_delete_user_mod = self._record_delete
        return api

    def _record_update(self, package_id, rule_content):
        self.saved.append((package_id, rule_content))
        return {"status": "success"}

    def _record_delete(self, package_id):
        # 镜像真实 RuleManager.delete_user_mod_rule：内存表同步移除 + 落盘
        self.deleted.append(package_id)
        self._current_rules.pop(package_id.casefold(), None)
        return {"status": "success"}

    def test_upsert_merges_and_preserves_existing_rules(self):
        api = self._api(
            {
                "solaris.ratkin": {
                    "loadAfter": {"brrainz.harmony": {"comment": "keep"}},
                    "loadTop": {"value": True, "comment": "x"},
                }
            }
        )
        payload = self._features._handle_set_mod_rule(
            api, package_id="Solaris.Ratkin", relation="incompatibleWith", target_id="other.mod"
        )
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["action"], "upserted")
        # 整包写回的是合并结果：既有 loadAfter / loadTop 原样保留
        self.assertEqual(len(self.saved), 1)
        saved_pid, saved_content = self.saved[0]
        self.assertEqual(saved_pid, "Solaris.Ratkin")
        self.assertEqual(saved_content["loadAfter"]["brrainz.harmony"]["comment"], "keep")
        self.assertEqual(saved_content["loadTop"]["value"], True)
        self.assertEqual(saved_content["incompatibleWith"], {"other.mod": {}})

    def test_remove_only_touches_target_relation(self):
        api = self._api({"a.mod": {"loadAfter": {"x": {}}, "loadBefore": {"y": {}}}})
        payload = self._features._handle_set_mod_rule(
            api, package_id="A.MOD", relation="loadAfter", target_id="X", remove=True
        )
        self.assertTrue(payload["removed"])
        remaining = api.sorter.rule_mgr.user_mod_rules["a.mod"]
        self.assertNotIn("loadAfter", remaining)
        self.assertEqual(remaining["loadBefore"], {"y": {}})

    def test_removing_last_relation_deletes_whole_entry(self):
        api = self._api({"a.mod": {"loadBefore": {"y": {}}}})
        payload = self._features._handle_set_mod_rule(
            api, package_id="a.mod", relation="loadBefore", target_id="Y", remove=True
        )
        self.assertEqual(payload["action"], "removed")
        self.assertNotIn("a.mod", api.sorter.rule_mgr.user_mod_rules)
        self.assertEqual(self.deleted, ["a.mod"])

    def test_noop_short_circuits_without_save(self):
        api = self._api({"a.mod": {"loadAfter": {"x": {}}}})

        def _must_not_save(*args, **kwargs):
            raise AssertionError("noop must not write the rules file")

        api.rule_update_user_mod = _must_not_save
        api.rule_delete_user_mod = _must_not_save
        payload = self._features._handle_set_mod_rule(
            api, package_id="a.mod", relation="loadAfter", target_id="X"
        )
        self.assertEqual(payload["action"], "noop")
        self.assertFalse(payload["removed"])

    def test_legacy_rules_wrapper_is_preserved(self):
        api = self._api({"a.mod": {"rules": {"loadAfter": {"x": {}}}, "loadTop": {"value": True}}})
        payload = self._features._handle_set_mod_rule(
            api, package_id="a.mod", relation="loadBefore", target_id="y"
        )
        self.assertEqual(payload["action"], "upserted")
        content = self.saved[0][1]
        self.assertEqual(content["rules"]["loadBefore"], {"y": {}})
        self.assertEqual(content["rules"]["loadAfter"], {"x": {}})
        self.assertEqual(content["loadTop"], {"value": True})

    def test_legacy_wrapper_removed_to_empty_deletes_whole_entry(self):
        # 旧包装路径：内层删到空必须先弹掉 "rules" 键再整条删除——
        # 否则会把 {"rules": {}} 空响应整包写回规则文件（判据
        # `container is current` 在包装路径下恒假，删空分支永远走不到）。
        api = self._api({"a.mod": {"rules": {"loadBefore": {"y": {}}}}})
        payload = self._features._handle_set_mod_rule(
            api, package_id="a.mod", relation="loadBefore", target_id="Y", remove=True
        )
        self.assertEqual(payload["action"], "removed")
        self.assertTrue(payload["removed"])
        self.assertEqual(self.saved, [])  # 走整条删除，没有任何 update 写回（含空值）
        self.assertEqual(self.deleted, ["a.mod"])
        self.assertNotIn("a.mod", api.sorter.rule_mgr.user_mod_rules)

    def test_legacy_wrapper_emptied_keeps_sibling_keys(self):
        # 包装内层删空但外层还有兄弟键 → 只弹掉 "rules"，其余原样写回（不误删整条）。
        api = self._api(
            {"a.mod": {"rules": {"loadBefore": {"y": {}}}, "loadTop": {"value": True}}}
        )
        payload = self._features._handle_set_mod_rule(
            api, package_id="a.mod", relation="loadBefore", target_id="Y", remove=True
        )
        self.assertEqual(payload["action"], "removed")
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.saved[0][1], {"loadTop": {"value": True}})

    def test_validation_errors(self):
        from fastmcp.exceptions import ToolError

        api = self._api()
        bad_calls = (
            {"package_id": "", "target_id": "x", "relation": "loadAfter"},
            {"package_id": "a", "target_id": "a", "relation": "loadAfter"},
            {"package_id": "a", "target_id": "b", "relation": "bogus"},
        )
        for kwargs in bad_calls:
            with self.assertRaises(ToolError):
                self._features._handle_set_mod_rule(api, **kwargs)

    def test_missing_rule_engine_is_causal_error(self):
        from fastmcp.exceptions import ToolError

        import backend.mcp_handlers_features as mcp_handlers_features

        api = FakeApi()  # 不带 sorter/rule_mgr
        with self.assertRaises(ToolError) as ctx:
            mcp_handlers_features._handle_set_mod_rule(
                api, package_id="a", relation="loadAfter", target_id="b"
            )
        self.assertIn("规则引擎未初始化", str(ctx.exception))


class TestAIToolArgsError(unittest.TestCase):
    """验证：AI 工具参数校验错误必须带参数名（否则文案空洞，无从知道缺哪个参数）。"""

    def test_validation_error_names_parameters(self):
        from backend.ai.ai_tools import AIToolExecutor

        executor = AIToolExecutor(None, {}, None)
        result = executor.execute_structured(
            "search_mods", json.dumps({"keyword": 123, "limit": "x"})
        )
        self.assertFalse(result.ok)
        self.assertIn("keyword", result.error)
        self.assertIn("limit", result.error)
        self.assertNotIn("请重新组织输入", result.error)  # 错误须点名参数，不落回笼统文案「请重新组织输入」


if __name__ == "__main__":
    unittest.main()
