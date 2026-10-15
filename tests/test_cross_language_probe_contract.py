# tests/test_cross_language_probe_contract.py
"""Python/Rust 双实现探活接口约定的交叉比对测试。

`backend/mcp_endpoint.py` 与 `dev/rimcrow-mcp/src/endpoint.rs` 是同一判定链的
两侧各自实现（CLOCK_SKEW / MAX_BOOT / SCHEMA_VERSION / ENDPOINT_ENV）。任一侧常量
漂移都会导致本测试红：正则提取 Rust 侧常量，与 Python 常量直接断言相等，
让两侧同步在 import 期就能失败。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from backend.mcp_endpoint import CLOCK_SKEW, ENDPOINT_ENV, MAX_BOOT

RUST_ENDPOINT = Path(__file__).resolve().parents[3] / "rimcrow-mcp" / "src" / "endpoint.rs"


def _rust_source() -> str:
    if not RUST_ENDPOINT.exists():
        self_skip = unittest.SkipTest(f"未找到 Rust 源: {RUST_ENDPOINT}")
        raise self_skip
    return RUST_ENDPOINT.read_text(encoding="utf-8")


class TestCrossLanguageProbeContract(unittest.TestCase):
    """Python 常量 == Rust `pub const`（两侧任一改动必须同步，否则此测试红）。"""

    def setUp(self):
        self.rust = _rust_source()

    def _rust_const(self, name: str) -> str:
        match = re.search(rf"pub const {name}[^=]*=\s*([^;]+);", self.rust)
        self.assertIsNotNone(match, f"Rust 侧缺少 pub const {name}")
        return match.group(1).strip()

    def test_clock_skew_matches(self):
        self.assertEqual(float(self._rust_const("CLOCK_SKEW")), CLOCK_SKEW)

    def test_max_boot_matches(self):
        self.assertEqual(float(self._rust_const("MAX_BOOT")), MAX_BOOT)

    def test_schema_version_matches(self):
        rust_schema = int(self._rust_const("SCHEMA_VERSION"))
        from backend.mcp_endpoint import SCHEMA_VERSION as PY_SCHEMA

        self.assertEqual(rust_schema, PY_SCHEMA)

    def test_endpoint_env_matches(self):
        self.assertEqual(self._rust_const("ENDPOINT_ENV").strip('"'), ENDPOINT_ENV)


if __name__ == "__main__":
    unittest.main()
