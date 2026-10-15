"""backend/mcp_loadops.py 的单测：增量 ops 的原子性、锚语义与冲突定位。"""

import unittest

from backend.mcp_loadops import MAX_OPS, Conflict, apply_ops


class TestApplyOpsHappyPath(unittest.TestCase):
    """成功路径：锚定插入/移动、幂等激活停用、最终顺序与摘要。"""

    def setUp(self):
        # 注意大小写混排：身份比较 casefold，输出必须保留原始大小写
        self.base = ["ludeon.rimworld", "brrainz.harmony", "Some.Mod"]

    def test_insert_append_at_end_when_anchor_absent(self):
        """无 anchor → 追加到末尾，确认串为 at end。"""
        result = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, ["ludeon.rimworld", "brrainz.harmony", "Some.Mod", "zeta.mod"])
        self.assertEqual(result.applied, ["insert zeta.mod at end"])
        self.assertEqual(result.summary["inserted"], 1)

    def test_insert_anchor_none_also_appends(self):
        """anchor 显式为 null 与缺省等价（工具层透传 JSON 时常见）。"""
        result = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod", "anchor": None}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered[-1], "zeta.mod")

    def test_insert_after_anchor(self):
        result = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod", "anchor": {"after": "brrainz.harmony"}}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, ["ludeon.rimworld", "brrainz.harmony", "zeta.mod", "Some.Mod"])
        self.assertEqual(result.applied, ["insert zeta.mod after brrainz.harmony"])

    def test_insert_before_anchor(self):
        result = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod", "anchor": {"before": "Some.Mod"}}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, ["ludeon.rimworld", "brrainz.harmony", "zeta.mod", "Some.Mod"])
        self.assertEqual(result.applied, ["insert zeta.mod before Some.Mod"])

    def test_insert_index_anchor_including_bounds(self):
        """index 合法范围 [0, len(work)]（含末尾追加位）。"""
        first = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod", "anchor": {"index": 0}}])
        self.assertTrue(first.ok)
        self.assertEqual(first.ordered, ["zeta.mod", "ludeon.rimworld", "brrainz.harmony", "Some.Mod"])
        self.assertEqual(first.applied, ["insert zeta.mod -> index 0"])

        last = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod", "anchor": {"index": 3}}])
        self.assertTrue(last.ok)
        self.assertEqual(last.ordered[-1], "zeta.mod")
        self.assertEqual(last.applied, ["insert zeta.mod -> index 3"])

    def test_ref_resolves_against_current_working_list(self):
        """锚引用的是「当前工作副本」——前一条 op 刚插入的 id 也能被后一条引用。"""
        ops = [
            {"op": "insert", "id": "new.ref"},
            {"op": "move", "id": "brrainz.harmony", "to": {"after": "new.ref"}},
        ]
        result = apply_ops(self.base, ops)
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.ordered, ["ludeon.rimworld", "Some.Mod", "new.ref", "brrainz.harmony"])

    def test_remove_by_ids_keeps_original_casing(self):
        result = apply_ops(self.base, [{"op": "remove", "ids": ["some.mod", "brrainz.harmony"]}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, ["ludeon.rimworld"])
        # 确认串取工作副本里的原始大小写
        self.assertEqual(result.applied, ["remove Some.Mod, brrainz.harmony"])
        self.assertEqual(result.summary["removed"], 2)

    def test_remove_duplicate_id_only_removes_once(self):
        """大小写不同但同身份的重复 id 去重后只删一次。"""
        result = apply_ops(self.base, [{"op": "remove", "ids": ["SOME.MOD", "some.mod"]}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, ["ludeon.rimworld", "brrainz.harmony"])
        self.assertEqual(result.summary["removed"], 1)

    def test_move_by_index(self):
        result = apply_ops(self.base, [{"op": "move", "id": "brrainz.harmony", "to": {"index": 0}}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, ["brrainz.harmony", "ludeon.rimworld", "Some.Mod"])
        self.assertEqual(result.applied, ["move brrainz.harmony -> index 0"])
        self.assertEqual(result.summary["moved"], 1)

    def test_move_by_last_index(self):
        """index 上界 = len(work) - 1（最终位置按移除后的结果列表算）。"""
        result = apply_ops(self.base, [{"op": "move", "id": "ludeon.rimworld", "to": {"index": 2}}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, ["brrainz.harmony", "Some.Mod", "ludeon.rimworld"])

    def test_move_by_after_and_before_anchors(self):
        after = apply_ops(self.base, [{"op": "move", "id": "ludeon.rimworld", "to": {"after": "Some.Mod"}}])
        self.assertTrue(after.ok)
        self.assertEqual(after.ordered, ["brrainz.harmony", "Some.Mod", "ludeon.rimworld"])
        self.assertEqual(after.applied, ["move ludeon.rimworld after Some.Mod"])

        before = apply_ops(self.base, [{"op": "move", "id": "Some.Mod", "to": {"before": "ludeon.rimworld"}}])
        self.assertTrue(before.ok)
        self.assertEqual(before.ordered, ["Some.Mod", "ludeon.rimworld", "brrainz.harmony"])
        self.assertEqual(before.applied, ["move Some.Mod before ludeon.rimworld"])

    def test_move_resolves_anchor_case_insensitively_and_keeps_casing(self):
        """id/锚大小写不敏感，但输出保留工作副本的原始大小写。"""
        result = apply_ops(self.base, [{"op": "move", "id": "SOME.MOD", "to": {"after": "LUDEON.RIMWORLD"}}])
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.ordered, ["ludeon.rimworld", "Some.Mod", "brrainz.harmony"])
        self.assertEqual(result.applied, ["move Some.Mod after ludeon.rimworld"])

    def test_activate_appends_missing_ids_in_order(self):
        result = apply_ops(self.base, [{"op": "activate", "ids": ["z.1", "z.2", "ghost.mod"]}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered[-3:], ["z.1", "z.2", "ghost.mod"])
        self.assertEqual(result.applied, ["activate z.1, z.2, ghost.mod"])
        self.assertEqual(result.summary["activated"], 3)
        self.assertEqual(result.summary["skipped"], 0)

    def test_activate_idempotent_counts_skipped_not_conflict(self):
        """已在列表里的 id 是幂等 no-op：计 skipped，不是冲突，也不产出确认行。"""
        result = apply_ops(self.base, [{"op": "activate", "ids": ["ludeon.rimworld", "LUDEON.RIMWORLD"]}])
        self.assertTrue(result.ok)
        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.ordered, self.base)
        self.assertEqual(result.applied, [])
        self.assertEqual(result.summary["activated"], 0)
        self.assertEqual(result.summary["skipped"], 2)

    def test_deactivate_removes_present_ids(self):
        result = apply_ops(self.base, [{"op": "deactivate", "ids": ["some.mod"]}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, ["ludeon.rimworld", "brrainz.harmony"])
        self.assertEqual(result.applied, ["deactivate Some.Mod"])
        self.assertEqual(result.summary["deactivated"], 1)

    def test_deactivate_absent_id_is_idempotent_skipped(self):
        result = apply_ops(self.base, [{"op": "deactivate", "ids": ["ghost.mod"]}])
        self.assertTrue(result.ok)
        self.assertEqual(result.ordered, self.base)
        self.assertEqual(result.applied, [])
        self.assertEqual(result.summary["deactivated"], 0)
        self.assertEqual(result.summary["skipped"], 1)

    def test_mixed_happy_path_exact_final_order(self):
        """混合一组 ops：最终顺序、确认串、摘要计数逐项精确核对。"""
        ops = [
            {"op": "insert", "id": "zeta.mod", "anchor": {"after": "brrainz.harmony"}},
            {"op": "remove", "ids": ["Some.Mod"]},
            {"op": "move", "id": "brrainz.harmony", "to": {"index": 0}},
            {"op": "activate", "ids": ["new.one", "ludeon.rimworld"]},
            {"op": "deactivate", "ids": ["ghost.mod"]},
        ]
        result = apply_ops(self.base, ops)
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.ordered, ["brrainz.harmony", "ludeon.rimworld", "zeta.mod", "new.one"])
        self.assertEqual(
            result.applied,
            [
                "insert zeta.mod after brrainz.harmony",
                "remove Some.Mod",
                "move brrainz.harmony -> index 0",
                "activate new.one",
            ],
        )
        self.assertEqual(
            result.summary,
            {
                "inserted": 1,
                "removed": 1,
                "moved": 1,
                "activated": 1,
                "deactivated": 0,
                "skipped": 2,  # ludeon.rimworld 已激活 + ghost.mod 本就停用
            },
        )
        self.assertEqual(result.conflicts, [])

    def test_summary_always_has_all_six_keys(self):
        """固定形态：ok 时六个键恒在，无变化的计 0。"""
        result = apply_ops(self.base, [{"op": "remove", "ids": ["Some.Mod"]}])
        self.assertTrue(result.ok)
        self.assertEqual(
            set(result.summary),
            {"inserted", "removed", "moved", "activated", "deactivated", "skipped"},
        )
        self.assertEqual(result.summary["inserted"], 0)
        self.assertEqual(result.summary["skipped"], 0)

    def test_input_list_not_mutated_on_success(self):
        snapshot = list(self.base)
        apply_ops(self.base, [{"op": "insert", "id": "zeta.mod"}, {"op": "remove", "ids": ["Some.Mod"]}])
        self.assertEqual(self.base, snapshot)


class TestApplyOpsConflicts(unittest.TestCase):
    """冲突与原子性：单条失败、逐条报告、整体拒绝。"""

    def setUp(self):
        self.base = ["ludeon.rimworld", "brrainz.harmony", "Some.Mod"]

    def test_insert_duplicate_conflict(self):
        result = apply_ops(self.base, [{"op": "insert", "id": "some.mod"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.ordered, self.base)
        self.assertEqual(result.applied, [])
        self.assertEqual(result.summary, {})
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].index, 0)
        self.assertEqual(result.conflicts[0].op, "insert")
        self.assertIn("some.mod", result.conflicts[0].reason)

    def test_insert_duplicate_is_case_insensitive(self):
        """大小写不同的同身份 id 也算重复（身份比较 casefold）。"""
        result = apply_ops(["Alpha.Mod"], [{"op": "insert", "id": "alpha.MOD"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.conflicts[0].op, "insert")

    def test_insert_missing_ref_conflict(self):
        result = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod", "anchor": {"after": "ghost"}}])
        self.assertFalse(result.ok)
        self.assertIn("ghost", result.conflicts[0].reason)

    def test_insert_bad_anchor_shape_conflicts(self):
        """锚必须恰好含一个 after/before/index 键：缺键、多键、非 dict 都算冲突。"""
        bad_anchors = [
            "brrainz.harmony",  # 非 dict
            {},  # 缺键
            {"after": "brrainz.harmony", "before": "ludeon.rimworld"},  # 多键
            {"after": "brrainz.harmony", "index": 1},  # extra key
            {"foo": 1},  # 键名不对
        ]
        for anchor in bad_anchors:
            with self.subTest(anchor=anchor):
                result = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod", "anchor": anchor}])
                self.assertFalse(result.ok)
                self.assertEqual(result.conflicts[0].op, "insert")

    def test_insert_index_out_of_range_conflict(self):
        """insert 的 index 合法范围 [0, len(work)]（含 len）。"""
        for bad in (-1, 4):
            with self.subTest(index=bad):
                result = apply_ops(self.base, [{"op": "insert", "id": "zeta.mod", "anchor": {"index": bad}}])
                self.assertFalse(result.ok)
                self.assertIn("超出范围", result.conflicts[0].reason)

    def test_remove_missing_id_fails_whole_op(self):
        """任一 id 缺失 → 整条 remove 失败（不得部分删除），且逐 id 报冲突。"""
        result = apply_ops(self.base, [{"op": "remove", "ids": ["ludeon.rimworld", "ghost1", "ghost2"]}])
        self.assertFalse(result.ok)
        self.assertEqual(result.ordered, self.base)  # 好 id 也没被删
        self.assertEqual(result.applied, [])
        self.assertEqual(len(result.conflicts), 2)
        self.assertEqual([c.reason for c in result.conflicts], ["packageId 不存在：ghost1", "packageId 不存在：ghost2"])
        self.assertTrue(all(c.index == 0 and c.op == "remove" for c in result.conflicts))

    def test_empty_ids_conflicts_for_list_ops(self):
        for op_name in ("remove", "activate", "deactivate"):
            with self.subTest(op=op_name):
                result = apply_ops(self.base, [{"op": op_name, "ids": []}])
                self.assertFalse(result.ok)
                self.assertEqual(result.conflicts[0].op, op_name)

    def test_cross_op_atomicity_mixed_good_and_bad(self):
        """好 op + 坏 op 混合 → 整体拒绝：顺序不变、确认清空、冲突带对的 index。"""
        ops = [
            {"op": "insert", "id": "ok.inserted"},  # 0 好
            {"op": "remove", "ids": ["brrainz.harmony", "ghost"]},  # 1 坏
            {"op": "move", "id": "ludeon.rimworld", "to": {"index": 0}},  # 2 好
        ]
        result = apply_ops(self.base, ops)
        self.assertFalse(result.ok)
        self.assertEqual(result.ordered, self.base)
        self.assertNotIn("ok.inserted", result.ordered)
        self.assertEqual(result.applied, [])
        self.assertEqual(result.summary, {})
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].index, 1)
        self.assertEqual(result.conflicts[0].op, "remove")
        self.assertIn("ghost", result.conflicts[0].reason)

    def test_validation_continues_after_failure(self):
        """首条失败后仍继续校验后续 op，独立冲突一次报全（逐条返回）。"""
        ops = [
            {"op": "insert", "id": "brrainz.harmony"},  # 0 重复
            {"op": "activate", "ids": ["fine.mod"]},  # 1 好（不产冲突）
            {"op": "move", "id": "ghost", "to": {"index": 0}},  # 2 不存在
        ]
        result = apply_ops(self.base, ops)
        self.assertFalse(result.ok)
        self.assertEqual([c.index for c in result.conflicts], [0, 2])

    def test_move_self_anchor_conflict(self):
        """锚与被移动 id 同身份（大小写不敏感）→ 自锚冲突。"""
        result = apply_ops(self.base, [{"op": "move", "id": "brrainz.harmony", "to": {"after": "BRRAINZ.HARMONY"}}])
        self.assertFalse(result.ok)
        self.assertEqual(result.conflicts[0].op, "move")
        self.assertIn("自身", result.conflicts[0].reason)
        self.assertEqual(result.ordered, self.base)

    def test_move_index_out_of_range_conflict(self):
        """move 的 index 上界 = len(work) - 1（最终位置按移除后算）。"""
        for bad in (-1, 3, 99):
            with self.subTest(index=bad):
                result = apply_ops(self.base, [{"op": "move", "id": "ludeon.rimworld", "to": {"index": bad}}])
                self.assertFalse(result.ok)
                self.assertEqual(result.ordered, self.base)

    def test_move_missing_id_and_missing_anchor_conflicts(self):
        absent_id = apply_ops(self.base, [{"op": "move", "id": "ghost", "to": {"index": 0}}])
        self.assertFalse(absent_id.ok)
        self.assertIn("ghost", absent_id.conflicts[0].reason)

        absent_ref = apply_ops(self.base, [{"op": "move", "id": "ludeon.rimworld", "to": {"before": "ghost"}}])
        self.assertFalse(absent_ref.ok)
        self.assertIn("ghost", absent_ref.conflicts[0].reason)

        missing_to = apply_ops(self.base, [{"op": "move", "id": "ludeon.rimworld"}])
        self.assertFalse(missing_to.ok)
        self.assertEqual(missing_to.conflicts[0].op, "move")

    def test_unknown_op_conflict(self):
        """未知 op 值与缺失 op 字段都归为 op="unknown" 的冲突（不抛异常）。"""
        unknown = apply_ops(self.base, [{"op": "toggle", "id": "x"}])
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.conflicts[0].op, "unknown")
        self.assertIn("toggle", unknown.conflicts[0].reason)

        missing = apply_ops(self.base, [{"id": "x"}])
        self.assertFalse(missing.ok)
        self.assertEqual(missing.conflicts[0].op, "unknown")
        self.assertEqual(missing.conflicts[0].index, 0)

    def test_failed_result_does_not_mutate_input(self):
        snapshot = list(self.base)
        apply_ops(self.base, [{"op": "insert", "id": "ludeon.rimworld"}, {"op": "remove", "ids": ["ghost"]}])
        self.assertEqual(self.base, snapshot)

    def test_conflict_to_dict_shape(self):
        conflict = Conflict(index=3, op="insert", reason="packageId 已存在：x.mod")
        self.assertEqual(conflict.to_dict(), {"index": 3, "op": "insert", "reason": "packageId 已存在：x.mod"})


class TestApplyOpsStructure(unittest.TestCase):
    """ops 参数级别的结构性误用 → ValueError（不是 Conflict）。"""

    def setUp(self):
        self.base = ["ludeon.rimworld", "brrainz.harmony"]

    def test_empty_ops_raises(self):
        with self.assertRaises(ValueError):
            apply_ops(self.base, [])

    def test_non_list_ops_raises(self):
        for bad in (None, "insert", {"op": "insert"}, 42):
            with self.subTest(ops=bad):
                with self.assertRaises(ValueError):
                    apply_ops(self.base, bad)  # type: ignore[arg-type]

    def test_non_dict_entry_raises_and_names_index(self):
        with self.assertRaises(ValueError) as ctx:
            apply_ops(self.base, [{"op": "insert", "id": "x"}, "bogus", 42])
        self.assertIn("ops[1]", str(ctx.exception))

    def test_more_than_max_ops_raises(self):
        ops = [{"op": "activate", "ids": ["a.mod"]} for _ in range(MAX_OPS + 1)]
        with self.assertRaises(ValueError) as ctx:
            apply_ops(self.base, ops)
        self.assertIn(str(MAX_OPS), str(ctx.exception))

    def test_exactly_max_ops_is_accepted(self):
        """恰好 MAX_OPS 条合法 ops → 不抛；重复 activate 全部幂等。"""
        ops = [{"op": "activate", "ids": ["a.mod"]} for _ in range(MAX_OPS)]
        result = apply_ops(self.base, ops)
        self.assertTrue(result.ok)
        self.assertEqual(result.summary["activated"], 1)
        self.assertEqual(result.summary["skipped"], MAX_OPS - 1)

    def test_semantic_failures_never_raise(self):
        """单条 op 的语义失败一律降级为 Conflict，绝不冒泡成异常。"""
        semantic_garbage = [
            [{"op": "insert"}],
            [{"op": "insert", "id": "x.mod", "anchor": "nope"}],
            [{"op": "remove", "ids": "not-a-list"}],
            [{"op": "remove", "ids": [1, 2]}],
            [{"op": "move", "id": "x.mod", "to": {"index": "0"}}],
            [{"op": "activate", "ids": "not-a-list"}],
            [{"op": "deactivate", "ids": [None]}],
            [{"op": None}],
            [{"op": ["insert"]}],
        ]
        for ops in semantic_garbage:
            with self.subTest(ops=ops):
                result = apply_ops(self.base, ops)
                self.assertFalse(result.ok)
                self.assertGreaterEqual(len(result.conflicts), 1)
                self.assertEqual(result.ordered, self.base)


if __name__ == "__main__":
    unittest.main()
