# tests/test_tool_cache_generator.py
"""生成器测试：产物格式与工具清单同源。

四组断言：

1. **格式**：``--out`` 产物 ``json.loads`` 通过、UTF-8 无 BOM、indent=2 且
   非 ASCII 原文（ensure_ascii=False）；``schema_version == 1``、
   ``rimcrow_version == backend._version.__version__``、
   ``instructions == SERVER_INSTRUCTIONS``、``captured_at`` 为带时区 ISO8601。
2. **同源（核心）**：产物 tools 名字列表与顺序 == ``[spec.name for spec in TOOL_SPECS]``；
   逐工具 ``description == spec.desc``；inputSchema / description / annotations 与真实
   ``create_mcp_app(ManifestOnlyApi())`` + ``list_tools()`` 的 ``to_mcp_tool()``
   协议线上形态**深度相等**——生成器若手工复制 schema 导致偏离，此断言即红。
3. **缺省输出路径**：存在（不存在则先跑一次生成器写出），且与 ``--out`` 产物内容一致
   （``captured_at`` 是生成时刻时间戳，比对时剔除；两侧各自都断言合法带时区）。
4. **零导入期副作用**：子进程仅 import 生成器模块后 ``backend.api`` /
   ``backend.mcp_server`` / ``backend.mcp_tool_specs`` 均不在 ``sys.modules``；
   子进程跑生成器退出码 0 且 stdout **恰为一行摘要**（路径 + 工具数 + rimcrow_version）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from backend._version import __version__
from backend.mcp_server import ManifestOnlyApi, create_mcp_app
from backend.mcp_tool_specs import SERVER_INSTRUCTIONS, TOOL_SPECS

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen_tool_cache.py"

# 一次性生成、全模块复用（每次子进程 ~2s，避免每个用例重跑）。
_TMP = tempfile.TemporaryDirectory(prefix="gen-tool-cache-")


def _run_generator(*args: str) -> tuple[int, str, str]:
    """子进程跑生成器，返回 (退出码, stdout, stderr)；字节手动解码防控制台编码差异。"""
    proc = subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        timeout=300,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


@lru_cache(maxsize=1)
def _generate_to_tmp() -> tuple[Path, int, str, str]:
    out = Path(_TMP.name) / "tools-cache.json"
    code, stdout, stderr = _run_generator("--out", str(out))
    return out, code, stdout, stderr


def _payload() -> dict:
    out, code, _stdout, stderr = _generate_to_tmp()
    if code != 0:
        raise AssertionError(f"generator failed rc={code}\n{stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


def _load_generator_module():
    """按文件路径 import 生成器（scripts/ 非包），供取 DEFAULT_OUT 等模块常量。"""
    spec = importlib.util.spec_from_file_location("gen_tool_cache_under_test", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _load_generator_module()
DEFAULT_OUT: Path = GEN.DEFAULT_OUT


class TestFormatContract(unittest.TestCase):
    """用例组 1：产物格式 v1。"""

    def test_envelope_fields(self):
        payload = _payload()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["rimcrow_version"], __version__)
        self.assertEqual(payload["instructions"], SERVER_INSTRUCTIONS)
        self.assertEqual(payload["server_name"], "rimcrow-mcp")
        self.assertEqual(len(payload["tools"]), len(TOOL_SPECS))

    def test_captured_at_is_iso8601_with_timezone(self):
        stamp = datetime.fromisoformat(_payload()["captured_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertIsNotNone(stamp.utcoffset())

    def test_encoding_and_formatting(self):
        out, _code, _stdout, _stderr = _generate_to_tmp()
        raw = out.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "must be UTF-8 without BOM")
        text = raw.decode("utf-8")  # 非 ASCII 原文（ensure_ascii=False）
        self.assertIn("RimCrow（RimWorld 模组管理器）", text)
        self.assertNotIn("\\u4e2d", text)
        first, second, *_rest = text.splitlines()
        self.assertEqual(first, "{")
        self.assertTrue(second.startswith('  "schema_version"'), second)  # indent=2

    def test_tool_entries_have_exact_contract_fields(self):
        for entry in _payload()["tools"]:
            self.assertEqual(
                set(entry), {"name", "description", "inputSchema", "annotations"}, entry.get("name")
            )


class TestSameSource(unittest.TestCase):
    """用例组 2（核心）：产物与 TOOL_SPECS / FastMCP 实际注册结果同源。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        cls.live_tools = list(asyncio.run(create_mcp_app(ManifestOnlyApi()).list_tools()))

    def test_names_order_match_tool_specs(self):
        self.assertEqual(
            [entry["name"] for entry in self.payload["tools"]],
            [spec.name for spec in TOOL_SPECS],
        )

    def test_descriptions_match_spec_desc(self):
        for entry, spec in zip(self.payload["tools"], TOOL_SPECS):
            self.assertEqual(entry["description"], spec.desc, spec.name)

    def test_schema_annotations_match_live_registration(self):
        """inputSchema / description / annotations 与真实注册产物深度相等。"""
        self.assertEqual(len(self.payload["tools"]), len(self.live_tools))
        for entry, live in zip(self.payload["tools"], self.live_tools):
            wire = json.loads(
                live.to_mcp_tool().model_dump_json(by_alias=True, exclude_none=True)
            )
            self.assertEqual(entry["name"], wire["name"])
            self.assertEqual(entry["description"], wire.get("description"), live.name)
            self.assertEqual(entry["inputSchema"], wire["inputSchema"], live.name)
            self.assertEqual(entry["annotations"], wire.get("annotations"), live.name)

    def test_annotations_follow_risk_hints(self):
        by_name = {entry["name"]: entry for entry in self.payload["tools"]}
        from backend.mcp_tool_specs import RISK_HIGH, RISK_READ

        for spec in TOOL_SPECS:
            annotations = by_name[spec.name]["annotations"]
            if spec.risk == RISK_READ:
                self.assertIs(annotations["readOnlyHint"], True, spec.name)
                self.assertIs(annotations["destructiveHint"], False, spec.name)
            elif spec.risk == RISK_HIGH:
                self.assertIs(annotations["destructiveHint"], True, spec.name)


class TestDefaultOutput(unittest.TestCase):
    """用例组 3：缺省输出路径存在，且与 --out 产物内容一致。"""

    def test_default_path_exists_and_matches_out_artifact(self):
        if not DEFAULT_OUT.exists():
            code, _stdout, stderr = _run_generator()
            self.assertEqual(code, 0, stderr)
        default_payload = json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))
        ours = _payload()

        # captured_at 是生成时刻，逐次必变——剔除后其余内容必须逐字一致
        default_stripped = {k: v for k, v in default_payload.items() if k != "captured_at"}
        ours_stripped = {k: v for k, v in ours.items() if k != "captured_at"}
        self.assertEqual(default_stripped, ours_stripped)
        for payload in (default_payload, ours):
            stamp = datetime.fromisoformat(payload["captured_at"])
            self.assertIsNotNone(stamp.utcoffset())


class TestNoImportSideEffects(unittest.TestCase):
    """用例组 4：导入期零副作用 + stdout 单行摘要。"""

    def test_module_import_pulls_no_backend(self):
        code = (
            "import importlib.util, sys;"
            f"spec = importlib.util.spec_from_file_location('gtc', {str(GENERATOR)!r});"
            "module = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(module);"
            "print('IMPORTED', 'backend.api' in sys.modules,"
            " 'backend.mcp_server' in sys.modules, 'backend.mcp_tool_specs' in sys.modules)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        lines = [line for line in proc.stdout.decode("utf-8", errors="replace").splitlines() if line]
        self.assertEqual(lines, ["IMPORTED False False False"], lines)

    def test_stdout_is_single_summary_line(self):
        out, code, stdout, stderr = _generate_to_tmp()
        self.assertEqual(code, 0, stderr)
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"stdout must be one summary line, got: {stdout!r}")
        summary = lines[0]
        self.assertIn(os.path.normcase(str(out)), os.path.normcase(summary))
        self.assertIn(f"tools={len(TOOL_SPECS)}", summary)
        self.assertIn(f"rimcrow_version={__version__}", summary)


if __name__ == "__main__":
    unittest.main()
