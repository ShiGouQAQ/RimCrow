"""backend/mcp_deps 单测：依赖图遍历核心 + 后端适配层 + 工具返回结构。

图核心（walk / build_tree）用手工构造的 RuleEdge 索引验证——不需要数据库；
适配层（load_edge_index）用 unittest.mock 替换 ModDAO / RuleManager /
LoadOrderManager，验证「只有生效规则入图、incompatible 排除、启用集合接线」。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.mcp_deps import (
    MAX_CYCLES,
    MAX_DEPTH,
    MAX_EDGES,
    TRUNCATED_HINT,
    RuleEdge,
    build_tree,
    load_edge_index,
    query_dependencies,
    walk,
)


def _edge(source: str, target: str, relation: str = "load_after", force: bool = False) -> RuleEdge:
    return RuleEdge(source=source, target=target, relation=relation, force=force)


def _index_source_only(edges: list[RuleEdge]) -> dict[str, list[RuleEdge]]:
    """只登记 source 键的索引：模拟最朴素的调用方，
    验证 walk 的 up 遍历不依赖 target 侧登记（内部会拉平邻接表）。"""
    index: dict[str, list[RuleEdge]] = {}
    for edge in edges:
        index.setdefault(edge.source.casefold(), []).append(edge)
    return index


def _index_both(edges: list[RuleEdge]) -> dict[str, list[RuleEdge]]:
    """source + target 双键登记（与 load_edge_index 的产出一致）。"""
    index: dict[str, list[RuleEdge]] = {}
    for edge in edges:
        index.setdefault(edge.source.casefold(), []).append(edge)
        index.setdefault(edge.target.casefold(), []).append(edge)
    return index


def _chain_edges(count: int, relation: str = "load_after") -> tuple[list[RuleEdge], list[str]]:
    """线性链 mod.a → mod.b → …（count 条边，count+1 个节点）。"""
    ids = [f"mod.{chr(ord('a') + i)}" for i in range(count + 1)]
    return [_edge(ids[i], ids[i + 1], relation) for i in range(count)], ids


def _mutual_pair() -> list[RuleEdge]:
    """互指对：A 声明 loadBefore B，B 声明 loadAfter A（loadBefore/After 可互指的典型冲突形状）。"""
    return [
        _edge("mod.a", "mod.b", "load_before"),
        _edge("mod.b", "mod.a", "load_after"),
    ]


class TestWalk(unittest.TestCase):
    """图核心 walk：深度、方向、规范化方向、环、截断、去重、确定性、校验。"""

    def test_depth_limits_bfs_expansion(self):
        """depth=1 只收直属边；depth=3 收到第 3 层且 depth 字段逐层递增。"""
        edges, _ = _chain_edges(4)
        index = _index_source_only(edges)

        shallow = walk(index, "mod.a", direction="down", depth=1)
        self.assertEqual([edge["depth"] for edge in shallow.edges], [1])
        self.assertEqual(shallow.visited, 2)  # 起点 + 第一层邻居
        self.assertFalse(shallow.missing_start)

        deep = walk(index, "mod.a", direction="down", depth=3)
        self.assertEqual(len(deep.edges), 3)
        self.assertEqual([edge["depth"] for edge in deep.edges], [1, 2, 3])
        self.assertEqual(deep.visited, 4)

    def test_direction_selects_traversal_side(self):
        """down=我声明的边；up=指向我的边；both=两者并集。"""
        index = _index_source_only([
            _edge("mod.a", "mod.b", "dependencies"),
            _edge("mod.a", "mod.c", "load_before"),
            _edge("x.mod", "mod.a", "load_after"),
        ])

        down = walk(index, "mod.a", direction="down", depth=1)
        self.assertEqual({edge["to"] for edge in down.edges}, {"mod.b", "mod.c"})

        up = walk(index, "mod.a", direction="up", depth=1)
        self.assertEqual([(edge["from"], edge["to"]) for edge in up.edges], [("x.mod", "mod.a")])

        both = walk(index, "mod.a", direction="both", depth=1)
        self.assertEqual(len(both.edges), 3)

    def test_up_edges_keep_canonical_orientation(self):
        """up 遍历收集的边仍是「from=声明方, to=被引用方」——遍历方向不翻转边。"""
        index = _index_source_only([_edge("x.mod", "mod.a", "load_after")])
        result = walk(index, "mod.a", direction="up", depth=1)
        self.assertEqual(result.edges, [
            {"from": "x.mod", "to": "mod.a", "relation": "load_after", "depth": 1},
        ])

    def test_mutual_pair_reports_cycle_without_hanging(self):
        """loadBefore/After 互指 → cycles 非空；遍历正常结束，不死循环。"""
        index = _index_source_only(_mutual_pair())
        result = walk(index, "mod.a", direction="both", depth=2)
        self.assertEqual(result.cycles, [["mod.a", "mod.b"]])
        self.assertEqual(len(result.edges), 2)
        self.assertFalse(result.truncated)

    def test_self_loop_reported_as_single_node_cycle(self):
        """自环（from==to）报成单节点环 [node]。"""
        index = _index_source_only([_edge("solo.mod", "solo.mod", "load_before")])
        result = walk(index, "solo.mod", direction="down", depth=1)
        self.assertEqual(result.cycles, [["solo.mod"]])
        self.assertEqual(len(result.edges), 1)

    def test_cycles_capped_at_max_cycles(self):
        """环数量硬上限 MAX_CYCLES：超出部分不回报，且顺序保持确定。"""
        edges: list[RuleEdge] = []
        for i in range(MAX_CYCLES + 4):
            node = f"loop{i:02d}.mod"
            edges.append(_edge("start.mod", node, "load_after"))
            edges.append(_edge(node, node, "load_before"))
        result = walk(_index_source_only(edges), "start.mod", direction="both", depth=3)
        self.assertEqual(len(result.cycles), MAX_CYCLES)
        self.assertEqual(result.cycles, sorted(result.cycles, key=tuple))

    def test_edge_cap_marks_truncated(self):
        """max_edges 是硬上限：收满即停并 truncated=True；不超限时不误报。"""
        edges, _ = _chain_edges(4)
        index = _index_source_only(edges)

        capped = walk(index, "mod.a", direction="down", depth=MAX_DEPTH, max_edges=2)
        self.assertEqual(len(capped.edges), 2)
        self.assertTrue(capped.truncated)

        full = walk(index, "mod.a", direction="down", depth=MAX_DEPTH)
        self.assertEqual(len(full.edges), 4)
        self.assertFalse(full.truncated)

    def test_duplicate_edges_are_deduped(self):
        """同一条边重复登记（双键 + 字面重复）只收集一次，direction=both 也不翻倍。"""
        edge = _edge("mod.a", "mod.b", "dependencies")
        index = _index_both([edge, edge])
        result = walk(index, "mod.a", direction="both", depth=2)
        self.assertEqual(len(result.edges), 1)
        self.assertEqual(result.cycles, [])

    def test_walk_order_is_deterministic(self):
        """索引登记顺序不同 → 输出边表与环报告完全一致（同层按 from,to,relation 定序）。"""
        edges = [
            _edge("mod.a", "mod.b", "load_after"),
            _edge("mod.a", "mod.c", "load_after"),
            _edge("mod.b", "mod.d", "load_before"),
            _edge("mod.c", "mod.d", "dependencies"),
        ]
        first = walk(_index_source_only(edges), "mod.a", direction="both", depth=3)
        second = walk(_index_source_only(list(reversed(edges))), "mod.a", direction="both", depth=3)
        self.assertEqual(first.edges, second.edges)
        self.assertEqual(first.cycles, second.cycles)

    def test_unknown_start_marks_missing_start(self):
        """起点不在索引 → missing_start=True、零边零环零访问，不抛异常。"""
        index = _index_source_only([_edge("mod.a", "mod.b")])
        result = walk(index, "ghost.mod", direction="both", depth=2)
        self.assertTrue(result.missing_start)
        self.assertEqual(result.edges, [])
        self.assertEqual(result.cycles, [])
        self.assertEqual(result.visited, 0)

    def test_unknown_direction_raises(self):
        """未知 direction → ValueError（中文短文案）。"""
        index = _index_source_only([_edge("mod.a", "mod.b")])
        with self.assertRaisesRegex(ValueError, "direction 无效"):
            walk(index, "mod.a", direction="sideways", depth=1)

    def test_depth_out_of_range_raises(self):
        """depth 越界（0 / 负数 / >MAX_DEPTH）→ ValueError。"""
        index = _index_source_only([_edge("mod.a", "mod.b")])
        for bad_depth in (0, -1, MAX_DEPTH + 1):
            with self.subTest(depth=bad_depth), self.assertRaisesRegex(ValueError, "depth 必须是"):
                walk(index, "mod.a", direction="down", depth=bad_depth)


class TestBuildTree(unittest.TestCase):
    """build_tree：背离起点的嵌套结构、路径环标记、层数上限。"""

    def test_tree_nests_children_away_from_start_with_cycle_marker(self):
        """A→B（depth1）、B→A（depth2）折树：B 的子节点是回指 A 的环标记叶子，不展开。"""
        edges = walk(_index_source_only(_mutual_pair()), "mod.a", direction="down", depth=2).edges
        tree = build_tree("mod.a", edges, depth=2)

        self.assertEqual(tree["id"], "mod.a")
        (child,) = tree["children"]
        self.assertEqual(child["id"], "mod.b")
        self.assertEqual(child["relation"], "load_before")
        self.assertEqual(child["depth"], 1)

        (grandchild,) = child["children"]
        self.assertEqual(grandchild["id"], "mod.a")
        self.assertTrue(grandchild["__cycle__"])
        self.assertEqual(grandchild["children"], [])  # 环标记不再展开，不会死循环

    def test_tree_stops_at_given_depth(self):
        """depth=1 只挂直属子节点；depth=2 才展开到孙节点。"""
        edges = walk(
            _index_source_only([
                _edge("mod.a", "mod.b", "load_after"),
                _edge("mod.b", "mod.c", "load_after"),
            ]),
            "mod.a",
            direction="down",
            depth=2,
        ).edges

        shallow = build_tree("mod.a", edges, depth=1)
        (child,) = shallow["children"]
        self.assertEqual(child["id"], "mod.b")
        self.assertEqual(child["children"], [])

        deep = build_tree("mod.a", edges, depth=2)
        self.assertEqual(deep["children"][0]["children"][0]["id"], "mod.c")


class TestQueryDependencies(unittest.TestCase):
    """工具入口：校验 → 返回结构组装（load_edge_index 被 patch 掉，不碰数据库）。"""

    def _fixture_index(self) -> dict[str, list[RuleEdge]]:
        return _index_both([
            _edge("Author.ModA", "Author.ModB", "load_after"),
            _edge("Author.ModA", "lib.core", "dependencies", force=True),
        ])

    def test_happy_path_returns_envelope_keys(self):
        """默认 edges 形态：返回键齐全、起点还原原始大小写、边形状固定 4 字段、无 hint。"""
        index = self._fixture_index()
        active = {"author.moda", "author.modb"}  # lib.core 未启用
        with patch("backend.mcp_deps.load_edge_index", return_value=(index, active)):
            result = query_dependencies(None, "AUTHOR.MODA", direction="down", depth=1)

        self.assertEqual(
            set(result),
            {"start", "direction", "depth", "total_edges", "truncated", "cycles", "edges"},
        )
        self.assertEqual(result["start"], "Author.ModA")  # 还原索引中的原始大小写
        self.assertEqual(result["direction"], "down")
        self.assertEqual(result["depth"], 1)
        self.assertEqual(result["total_edges"], 2)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["cycles"], [])
        self.assertNotIn("hint", result)

        # 边形状固定 {from,to,relation,depth}（force/规则明细归 get_mod_rules）
        for edge in result["edges"]:
            self.assertEqual(set(edge), {"from", "to", "relation", "depth"})

    def test_tree_format_returns_nested_tree(self):
        """format=tree → 附 tree 不附 edges，根 id 与 start 一致。"""
        index = self._fixture_index()
        with patch("backend.mcp_deps.load_edge_index", return_value=(index, set())):
            result = query_dependencies(None, "author.moda", fmt="tree", depth=2)

        self.assertIn("tree", result)
        self.assertNotIn("edges", result)
        self.assertEqual(result["tree"]["id"], "Author.ModA")
        self.assertEqual(len(result["tree"]["children"]), 2)

    def test_tree_format_depth_above_two_raises(self):
        """format=tree 且 depth>2 → ValueError（tree 只允许 depth<=2）。"""
        with self.assertRaisesRegex(ValueError, "depth<=2"):
            query_dependencies(None, "any.mod", fmt="tree", depth=3)

    def test_missing_start_raises_chinese_error(self):
        """起点既不在索引也不在启用集合 → 中文 ValueError 带原始输入。"""
        with patch("backend.mcp_deps.load_edge_index", return_value=({}, set())):
            with self.assertRaises(ValueError) as ctx:
                query_dependencies(None, "ghost.mod")
        self.assertIn("未找到此模组: ghost.mod", str(ctx.exception))

    def test_truncated_result_includes_constant_hint(self):
        """超过 MAX_EDGES → truncated=True 且附常量 hint，边数恰为上限。"""
        hub_edges = [_edge("hub.mod", f"ext.mod{i:04d}", "load_after") for i in range(MAX_EDGES + 100)]
        index = _index_source_only(hub_edges)
        with patch("backend.mcp_deps.load_edge_index", return_value=(index, {"hub.mod"})):
            result = query_dependencies(None, "hub.mod", direction="down", depth=1)

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["edges"]), MAX_EDGES)
        self.assertEqual(result["total_edges"], MAX_EDGES)
        self.assertEqual(result["hint"], TRUNCATED_HINT)  # 常量，不含任何模组名

    def test_invalid_direction_raises(self):
        with self.assertRaisesRegex(ValueError, "direction 无效"):
            query_dependencies(None, "x.mod", direction="sideways")

    def test_invalid_depth_raises(self):
        with self.assertRaisesRegex(ValueError, "depth 必须是"):
            query_dependencies(None, "x.mod", depth=0)
        with self.assertRaisesRegex(ValueError, "depth 必须是"):
            query_dependencies(None, "x.mod", depth=MAX_DEPTH + 1)

    def test_invalid_format_raises(self):
        with self.assertRaisesRegex(ValueError, "format 仅支持"):
            query_dependencies(None, "x.mod", fmt="json")


class TestLoadEdgeIndex(unittest.TestCase):
    """适配层：ModDAO + 规则引擎 + 启用列表的接线（全 mock，不碰数据库）。"""

    @staticmethod
    def _visible_mods() -> list[dict]:
        return [
            {"package_id": "Author.ModA", "name": "A"},
            {"package_id": "author.modb", "name": "B"},
        ]

    @staticmethod
    def _rules_by_mod() -> dict:
        return {
            "author.moda": {
                "dependencies": [
                    {"target_id": "lib.core", "type": "dependencies", "is_force": True,
                     "effective": True, "source": {"type": "native", "name": "原生依赖"}},
                    # 被更高优先级来源遮蔽（effective=False）：不得入图
                    {"target_id": "shadowed.mod", "type": "dependencies", "is_force": False,
                     "effective": False, "source": {"type": "community", "name": "社区依赖"}},
                ],
                "load_after": [
                    {"target_id": "author.modb", "type": "load_after", "is_force": False,
                     "effective": True, "source": {"type": "user", "name": "用户前置"}},
                ],
                "load_before": [],
                # 不兼容不是加载顺序边，必须被排除
                "incompatible": [
                    {"target_id": "enemy.mod", "type": "incompatible", "is_force": False,
                     "effective": True, "source": {"type": "community", "name": "社区冲突"}},
                ],
                "weight_info": {},
            },
            "author.modb": {
                "dependencies": [], "load_after": [], "load_before": [],
                "incompatible": [], "weight_info": {},
            },
        }

    @staticmethod
    def _fake_rule_mgr(rules_by_mod: dict) -> Mock:
        """规则引擎替身：resolve_effective_mod_rules 按包名返回预置规则（resolve_mod_rules 走这条路）。"""
        rule_mgr = Mock()
        rule_mgr.resolve_effective_mod_rules.side_effect = (
            lambda token, mod: (mod, rules_by_mod[str(token).casefold()])
        )
        return rule_mgr

    def test_only_effective_rules_become_edges_and_active_set_wired(self):
        """复用 sorter.rule_mgr：仅 effective 规则入图、incompatible 排除、
        悬空目标登记为已知节点、启用集合按包 token 归一。"""
        rule_mgr = self._fake_rule_mgr(self._rules_by_mod())
        api = SimpleNamespace(
            active_context=object(),
            sorter=SimpleNamespace(rule_mgr=rule_mgr),
            load_order_mgr=Mock(),
        )
        api.load_order_mgr.read_active_mods.return_value = {
            "active_mods": ["Author.ModA", "lib.core_steam", "stray.mod"],
        }

        with patch("backend.mcp_deps.ModDAO.get_profile_mods", return_value=self._visible_mods()), \
             patch("backend.mcp_deps.RuleManager") as rule_manager_cls:
            index, active = load_edge_index(api)

        rule_manager_cls.assert_not_called()  # sorter.rule_mgr 可用时不重复构造

        # 生效规则按 _GRAPH_RELATIONS 顺序入图，force 保留
        self.assertEqual(
            [(edge.target, edge.relation, edge.force) for edge in index["author.moda"]],
            [("lib.core", "dependencies", True), ("author.modb", "load_after", False)],
        )

        all_edges = [edge for bucket in index.values() for edge in bucket]
        all_targets = {edge.target for edge in all_edges}
        self.assertNotIn("shadowed.mod", all_targets)          # effective=False 不入图
        self.assertNotIn("enemy.mod", all_targets)             # incompatible 不入图
        self.assertTrue(all(edge.relation != "incompatible" for edge in all_edges))

        # 悬空目标（未安装的 lib.core）与无规则可见模组都是已知节点
        self.assertIn("lib.core", index)
        self.assertIn("author.modb", index)

        # 启用集合：包 token 归一（_steam 后缀剥离）
        self.assertEqual(active, {"author.moda", "lib.core", "stray.mod"})

    def test_fallback_constructs_rule_and_load_order_manager_once(self):
        """sorter / load_order_mgr 缺失时各构造一次（而不是每个模组一次）。"""
        context = SimpleNamespace(profile_id="current")
        api = SimpleNamespace(active_context=context, sorter=None, load_order_mgr=None)

        with patch("backend.mcp_deps.ModDAO.get_profile_mods", return_value=self._visible_mods()), \
             patch("backend.mcp_deps.RuleManager", return_value=self._fake_rule_mgr(self._rules_by_mod())) as rule_manager_cls, \
             patch("backend.mcp_deps.LoadOrderManager") as load_order_cls:
            load_order_cls.return_value.read_active_mods.return_value = {
                "active_mods": ["author.moda"],
            }
            index, active = load_edge_index(api)

        rule_manager_cls.assert_called_once_with(context)
        load_order_cls.assert_called_once_with(context)
        self.assertIn("author.moda", index)
        self.assertEqual(active, {"author.moda"})


if __name__ == "__main__":
    unittest.main()
