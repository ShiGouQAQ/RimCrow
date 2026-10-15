import math
import unittest
from datetime import datetime

from backend.mcp_logview import (
    block_matches,
    block_time,
    filter_blocks,
    normalize_levels,
    parse_time,
    scan_pages,
)


def make_block(
    line: int,
    *,
    level: str = "INFO",
    timestamp: str | None = "2026-09-27 03:00:00.000",
    message: str | None = None,
    details: str = "",
    raw_lines: list[int] | None = None,
) -> dict:
    """构造一个贴近上游 reader 输出的块（raw_lines 默认 1 基单行号）。"""
    return {
        "id": f"id{line}",
        "level": level,
        "timestamp": timestamp if timestamp is not None else "",
        "message": message if message is not None else f"message {line}",
        "details": details,
        "count": 1,
        "raw_lines": [line] if raw_lines is None else raw_lines,
    }


def make_fetcher(newest_first_blocks: list[dict]):
    """仿 api.read_log_page 内层 payload 的分页 stub。

    输入按**全局最新优先**排列；stub 模拟真实 reader：page=1 为最新页，
    页内顺序为最旧→最新（即对页切片 reverse），并记录每次调用的页号。
    """
    calls: list[int] = []

    def fetch(page: int, page_size: int) -> dict:
        calls.append(page)
        total_pages = (
            math.ceil(len(newest_first_blocks) / page_size)
            if newest_first_blocks
            else 1
        )
        start = (page - 1) * page_size
        chunk = newest_first_blocks[start : start + page_size]
        if not chunk:
            return {"blocks": [], "has_more": False, "total_pages": total_pages, "current_page": page}
        return {
            "status": "success",
            "blocks": list(reversed(chunk)),  # 真实 reader：页内最旧→最新
            "has_more": page < total_pages,
            "total_pages": total_pages,
            "current_page": page,
        }

    fetch.calls = calls
    return fetch


class TestParseTime(unittest.TestCase):
    """parse_time：相对量 / ISO 绝对时间 / 非法输入。"""

    def test_relative_units_subtract_from_now(self):
        """s/m/h/d/w 五种相对单位都按 now 往回换算 epoch 秒。"""
        now = datetime(2026, 9, 27, 12, 0, 0)
        base = now.timestamp()
        cases = {"90s": 90, "30m": 1800, "1h": 3600, "2d": 2 * 86400, "1w": 7 * 86400}
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertAlmostEqual(parse_time(text, now=now), base - seconds, places=3)

    def test_iso_absolute_parses_t_and_space_separator(self):
        """ISO8601 的 T 分隔与空格分隔等价，且等于同一本地时刻的 epoch。"""
        expected = datetime(2026, 9, 27, 3, 0, 0).timestamp()
        self.assertAlmostEqual(parse_time("2026-09-27T03:00:00"), expected, places=3)
        self.assertAlmostEqual(parse_time("2026-09-27 03:00:00"), expected, places=3)

    def test_invalid_values_raise_value_error(self):
        """空串 / 纯文字 / 带杂讯的单位一律 ValueError（中文短消息）。"""
        for text in ("", "   ", "abc", "10x", "2026-13-99T99:99:99"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_time(text)


class TestNormalizeLevels(unittest.TestCase):
    """normalize_levels：None/空、大小写、别名。"""

    def test_none_and_empty_mean_no_filter(self):
        """None 与空列表都返回 None（= 不过滤）。"""
        self.assertIsNone(normalize_levels(None))
        self.assertIsNone(normalize_levels([]))
        self.assertIsNone(normalize_levels(["  "]))

    def test_case_insensitive_uppercases(self):
        """大小写混写统一归到上游大写等级名。"""
        self.assertEqual(normalize_levels(["error", "Info", " WARNING "]), {"ERROR", "INFO", "WARNING"})

    def test_aliases_map_warn_and_err(self):
        """别名 warn→WARNING、err→ERROR；与正式名去重合并。"""
        self.assertEqual(normalize_levels(["warn", "err", "error"]), {"WARNING", "ERROR"})


class TestBlockTime(unittest.TestCase):
    """block_time：块时间戳解析。"""

    def test_empty_or_missing_timestamp_returns_none(self):
        """空串 / 缺字段 / 解析不了 → None（Player.log 文本块即空串）。"""
        self.assertIsNone(block_time({"timestamp": ""}))
        self.assertIsNone(block_time({}))
        self.assertIsNone(block_time({"timestamp": "not a time"}))

    def test_valid_formatter_timestamp_parses(self):
        """JSONFormatter 的 'YYYY-MM-DD HH:MM:SS.mmm' 直接可解析为 epoch 秒。"""
        got = block_time({"timestamp": "2026-09-27 03:00:00.123"})
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, datetime(2026, 9, 27, 3, 0, 0, 123000).timestamp(), places=3)


class TestBlockMatches(unittest.TestCase):
    """block_matches：逐维度过滤 + AND 组合语义。"""

    @staticmethod
    def _kwargs(**overrides) -> dict:
        kwargs = {
            "levels": None,
            "since_ts": None,
            "until_ts": None,
            "line_from": None,
            "line_to": None,
            "keyword": None,
        }
        kwargs.update(overrides)
        return kwargs

    def test_level_filter_is_case_insensitive_on_block_side(self):
        """块等级大小写混写也能命中大写集合；不在集合内则排除。"""
        error = make_block(1, level="error")
        info = make_block(2, level="INFO")
        self.assertTrue(block_matches(error, **self._kwargs(levels={"ERROR"})))
        self.assertFalse(block_matches(info, **self._kwargs(levels={"ERROR"})))

    def test_time_range_inclusive_and_timestamp_less_excluded(self):
        """闭区间 since<=t<=until；时间过滤激活时无时间戳块被排除。"""
        lo = datetime(2026, 9, 27, 3, 0, 0).timestamp()
        hi = datetime(2026, 9, 27, 4, 0, 0).timestamp()
        inside = make_block(1, timestamp="2026-09-27 03:30:00.000")
        edge_lo = make_block(2, timestamp="2026-09-27 03:00:00.000")
        outside = make_block(3, timestamp="2026-09-27 05:00:00.000")
        timeless = make_block(4, timestamp="")
        self.assertTrue(block_matches(inside, **self._kwargs(since_ts=lo, until_ts=hi)))
        self.assertTrue(block_matches(edge_lo, **self._kwargs(since_ts=lo, until_ts=hi)))
        self.assertFalse(block_matches(outside, **self._kwargs(since_ts=lo, until_ts=hi)))
        self.assertFalse(block_matches(timeless, **self._kwargs(since_ts=lo, until_ts=hi)))
        # 没有时间过滤时空时间戳块照常放行
        self.assertTrue(block_matches(timeless, **self._kwargs()))

    def test_line_range_overlap_semantics(self):
        """raw_lines [100..105] 与查询窗口做重叠判断（闭边界）。"""
        block = make_block(100, raw_lines=[100, 101, 105])
        self.assertTrue(block_matches(block, **self._kwargs(line_from=105)))
        self.assertFalse(block_matches(block, **self._kwargs(line_from=106)))
        self.assertTrue(block_matches(block, **self._kwargs(line_to=100)))
        self.assertFalse(block_matches(block, **self._kwargs(line_to=99)))
        self.assertTrue(block_matches(block, **self._kwargs(line_from=102, line_to=103)))
        self.assertFalse(block_matches(block, **self._kwargs(line_from=106, line_to=200)))

    def test_line_filter_excludes_blocks_without_raw_lines(self):
        """行过滤激活时无 raw_lines 的块被排除；未激活时放行。"""
        block = {"level": "ERROR", "message": "x", "details": ""}
        self.assertFalse(block_matches(block, **self._kwargs(line_from=10)))
        self.assertTrue(block_matches(block, **self._kwargs()))

    def test_keyword_matches_case_insensitively_across_message_and_details(self):
        """keyword 对 message 与 details 都做大小写不敏感子串匹配。"""
        block = make_block(1, message="Hello World", details="NullReferenceException at Foo")
        self.assertTrue(block_matches(block, **self._kwargs(keyword="world")))
        self.assertTrue(block_matches(block, **self._kwargs(keyword="NULLREFERENCE")))
        self.assertFalse(block_matches(block, **self._kwargs(keyword="nope")))

    def test_all_filters_combine_with_and(self):
        """五个维度全提供时必须同时满足（AND），任一失配即排除。"""
        lo = datetime(2026, 9, 27, 3, 0, 0).timestamp()
        hi = datetime(2026, 9, 27, 4, 0, 0).timestamp()
        base = dict(levels={"ERROR"}, since_ts=lo, until_ts=hi, line_from=100, line_to=200, keyword="boom")
        keeper = make_block(
            150, level="ERROR", timestamp="2026-09-27 03:30:00.000",
            message="mod X boom", raw_lines=[150],
        )
        self.assertTrue(block_matches(keeper, **self._kwargs(**base)))
        # 每次只破坏一个维度
        wrong_level = dict(keeper, level="INFO")
        wrong_time = dict(keeper, timestamp="2026-09-27 09:00:00.000")
        wrong_line = dict(keeper, raw_lines=[300])
        wrong_keyword = dict(keeper, message="all fine")
        self.assertFalse(block_matches(wrong_level, **self._kwargs(**base)))
        self.assertFalse(block_matches(wrong_time, **self._kwargs(**base)))
        self.assertFalse(block_matches(wrong_line, **self._kwargs(**base)))
        self.assertFalse(block_matches(wrong_keyword, **self._kwargs(**base)))


class TestFilterBlocks(unittest.TestCase):
    """filter_blocks：保持输入顺序、时间字符串只解析一次且非法上抛。"""

    def test_keeps_input_order(self):
        """命中块按输入相对顺序返回（本函数不做任何排序）。"""
        blocks = [
            make_block(3, level="ERROR"),
            make_block(2, level="INFO"),
            make_block(1, level="ERROR"),
        ]
        got = filter_blocks(blocks, levels=["error"])
        self.assertEqual([b["id"] for b in got], ["id3", "id1"])

    def test_invalid_since_raises_value_error(self):
        """非法时间字符串直接上抛 ValueError，不被吞掉。"""
        with self.assertRaises(ValueError):
            filter_blocks([make_block(1)], since="garbage")


class TestScanPages(unittest.TestCase):
    """scan_pages：最新优先累积 / offset+limit / tail 提前停 / 各终止标志。"""

    def test_accumulates_pages_in_newest_first_order(self):
        """跨页拼接后仍是全局最新优先：每页内部反转再依次追加。"""
        stream = [make_block(i) for i in range(25)]  # id0 最新 … id24 最旧
        fetch = make_fetcher(stream)
        result = scan_pages(fetch, page_size=10, limit=100)
        self.assertEqual([b["id"] for b in result["items"]], [f"id{i}" for i in range(25)])
        self.assertEqual(result["total"], 25)
        self.assertTrue(result["exhausted"])
        self.assertFalse(result["has_more_pages"])
        self.assertEqual(result["scanned_pages"], 3)
        self.assertEqual(fetch.calls, [1, 2, 3])

    def test_offset_and_limit_window(self):
        """offset+limit 作用在全局匹配流上，窗口边界精确。"""
        stream = [make_block(i) for i in range(25)]
        fetch = make_fetcher(stream)
        result = scan_pages(fetch, page_size=10, offset=5, limit=7)
        self.assertEqual([b["id"] for b in result["items"]], [f"id{i}" for i in range(5, 12)])
        # want=12 → 第 2 页（整页 10 块收完）后凑满，停在 2 页；
        # 整页才判停保证 has_more=False 时 total 精确，故 total=已收完整页的匹配数
        self.assertEqual(result["scanned_pages"], 2)
        self.assertEqual(result["total"], 20)
        self.assertFalse(result["exhausted"])  # page2 时 has_more 仍为 True

    def test_tail_stops_early_with_fewer_pages(self):
        """tail=3 只读第 1 页（10 块/页），比全量扫描少读页。"""
        stream = [make_block(i) for i in range(50)]  # 5 页
        fetch = make_fetcher(stream)
        full = scan_pages(fetch, page_size=10, limit=100)
        self.assertEqual(full["scanned_pages"], 5)

        fetch_tail = make_fetcher(stream)
        result = scan_pages(fetch_tail, page_size=10, tail=3)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual([b["id"] for b in result["items"]], ["id0", "id1", "id2"])
        self.assertLess(result["scanned_pages"], full["scanned_pages"])
        self.assertEqual(fetch_tail.calls, [1])

    def test_tail_forces_offset_zero(self):
        """tail 设置时 offset 被强制为 0，结果仍从最新开始。"""
        stream = [make_block(i) for i in range(50)]
        fetch = make_fetcher(stream)
        result = scan_pages(fetch, page_size=10, tail=3, offset=40)
        self.assertEqual([b["id"] for b in result["items"]], ["id0", "id1", "id2"])

    def test_max_pages_cap_reports_not_exhausted(self):
        """触到 max_pages 上限即停：exhausted=False、total 是已找到的下界。"""
        stream = [make_block(i) for i in range(100)]  # 10 页
        fetch = make_fetcher(stream)
        result = scan_pages(fetch, page_size=10, limit=1000, max_pages=2)
        self.assertEqual(result["scanned_pages"], 2)
        self.assertEqual(fetch.calls, [1, 2])
        self.assertFalse(result["exhausted"])
        self.assertTrue(result["has_more_pages"])
        self.assertEqual(result["total"], 20)  # 只见过前两页

    def test_empty_reader_ends_scan_immediately(self):
        """首块即空页：扫描立刻结束，exhausted=True、total=0。"""
        fetch = make_fetcher([])
        result = scan_pages(fetch, limit=50)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertTrue(result["exhausted"])
        self.assertFalse(result["has_more_pages"])
        self.assertEqual(result["scanned_pages"], 1)

    def test_exact_exhaustion_marks_total_exact(self):
        """want 恰好在最后一页凑满且 has_more=False：exhausted=True、total 精确。"""
        stream = [make_block(i) for i in range(25)]  # 3 页，末页 has_more=False
        fetch = make_fetcher(stream)
        result = scan_pages(fetch, page_size=10, offset=20, limit=5)
        self.assertEqual(result["scanned_pages"], 3)
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["total"], 25)
        self.assertEqual([b["id"] for b in result["items"]], ["id20", "id21", "id22", "id23", "id24"])

    def test_filters_apply_across_pages(self):
        """levels + keyword 过滤跨页生效，total 只统计命中块。"""
        stream = []
        for i in range(30):
            level = "ERROR" if i % 2 == 0 else "INFO"
            msg = "boom happened" if i % 3 == 0 else f"regular {i}"
            stream.append(make_block(i, level=level, message=msg))
        fetch = make_fetcher(stream)
        result = scan_pages(fetch, page_size=10, levels=["error"], keyword="boom", limit=100)
        # 命中：i 为偶数且 i%3==0 → i ∈ {0,6,12,18,24}（30 以内）
        self.assertEqual([b["id"] for b in result["items"]], ["id0", "id6", "id12", "id18", "id24"])
        self.assertEqual(result["total"], 5)
        self.assertTrue(result["exhausted"])

    def test_time_filter_excludes_text_blocks_in_scan(self):
        """Player.log 文本块（timestamp=''）在 since 过滤下不进结果。"""
        stream = [
            make_block(2, timestamp="2026-09-27 03:30:00.000"),
            make_block(1, timestamp=""),  # 纯文本块，应被排除
        ]
        fetch = make_fetcher(stream)
        since = datetime(2026, 9, 27, 3, 0, 0).timestamp()
        result = scan_pages(fetch, since="2026-09-27T03:00:00", limit=10)
        self.assertEqual([b["id"] for b in result["items"]], ["id2"])
        self.assertEqual(result["total"], 1)
        self.assertAlmostEqual(block_time(stream[0]), since + 1800, places=3)


if __name__ == "__main__":
    unittest.main()
