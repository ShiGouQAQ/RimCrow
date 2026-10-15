# tests/test_mcp_payload_e2e.py
"""端到端：语义层产物穿过 FastMCP + ``ResponseLimitingMiddleware`` 后仍是合法 JSON。

两层关系：

- **保险层** = ``create_mcp_app`` 注册的 ``ResponseLimitingMiddleware(max_size=max_output())``。
  它度量的是**整帧 ToolResult**（文本经 JSON 转义 + 126 字节包装，见
  ``backend.mcp_guard.frame_cost``），超限就把文本从中间切开再补
  ``[Response truncated due to size limit]`` → agent 拿到结构破损的 JSON
  （表现为 MCP 返回类型变 string、``Extra data`` 解析失败）。
- **语义层** = ``guard_payload``，默认 cap = ``semantic_max_output()``（保险层 × 0.75），
  产物恒为合法 JSON，且用 ``frame_cost`` 复核整帧成本 ≤ 保险层。

单测只能证明「语义层产物 ≤ cap」，**证明不了**它穿过保险层后还完整——所以这里的
每个用例都走进程内 ``fastmcp.Client`` 真正调用一次：客户端拿到的文本必须能
``json.loads`` 且带 ``__truncated__``。``test_insurance_layer_is_armed`` 先确认保险层
确实在这条链路上生效，后续用例的断言才有意义。
"""

from __future__ import annotations

import asyncio
import json
import unittest

from backend.mcp_guard import (
    TRUNCATED_KEY,
    frame_cost,
    guard_payload,
    max_output,
    semantic_max_output,
)
from backend.mcp_server import ManifestOnlyApi, create_mcp_app

INSURANCE_SUFFIX = "[Response truncated due to size limit]"


def _oversized_items(count: int = 6000, pad: int = 60) -> list[dict]:
    """远超语义 cap 的模组列表（6000 × ~100B ≈ 600KB）。"""
    return [
        {
            "package_id": f"author.modname{i:05d}",
            "name": f"Some Mod Name {i} " + "x" * pad,
            "is_active": bool(i % 3),
        }
        for i in range(count)
    ]


def _register(mcp, name: str, fn):
    """按 ``create_mcp_app`` 同款参数注册探针工具（output_schema=None 单份文本下发）。"""
    return mcp.tool(name=name, description="[只读] e2e payload probe", output_schema=None)(fn)


def _call_text(mcp, name: str) -> str:
    """进程内 FastMCP 客户端调用，返回客户端实际收到的文本。"""
    from fastmcp import Client

    async def _run() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(name, {})
        return result.content[0].text

    return asyncio.run(_run())


class TestPayloadSurvivesInsuranceLayer(unittest.TestCase):
    """超大返回工具经完整 MCP 链路后，客户端仍拿到可解析的结构化结果。"""

    def test_insurance_layer_is_armed_in_process(self):
        """验证：绕过语义层的超大**合法 JSON** 必被保险层切坏——保险层已生效，后续用例才非无效。"""
        mcp = create_mcp_app(ManifestOnlyApi())
        raw = json.dumps({"items": ["y" * 100] * 3000}, ensure_ascii=False)
        self.assertGreater(frame_cost(raw), max_output())  # 确实超保险层
        _register(mcp, "probe_bypass_guard", lambda: raw)
        got = _call_text(mcp, "probe_bypass_guard")
        self.assertIn(INSURANCE_SUFFIX, got)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(got)

    def test_guard_output_survives_insurance_layer(self):
        """验证：客户端收到的文本 json.loads 得动且含 __truncated__。"""
        mcp = create_mcp_app(ManifestOnlyApi())
        payload = {"status": "success", "message": "载入完成", "data": {"all_mods": _oversized_items()}}
        text = guard_payload(payload)
        parsed_semantic = json.loads(text)
        self.assertTrue(parsed_semantic[TRUNCATED_KEY])

        _register(mcp, "probe_guarded_payload", lambda: guard_payload(payload))
        got = _call_text(mcp, "probe_guarded_payload")
        self.assertNotIn(INSURANCE_SUFFIX, got)
        self.assertEqual(got, text)  # 保险层一个字节都没动它
        parsed = json.loads(got)  # 关键判据：客户端文本可解析
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertEqual(parsed["truncated_fields"], ["data.all_mods"])
        self.assertGreater(len(parsed["data"]["all_mods"]), 0)
        self.assertEqual(parsed["status"], "success")
        self.assertLessEqual(len(got.encode("utf-8")), semantic_max_output())
        self.assertLessEqual(frame_cost(got), max_output())

    def test_top_level_page_envelope_survives_insurance_layer(self):
        """顶层形态（list_mods 返回结构）同样端到端可解析。"""
        mcp = create_mcp_app(ManifestOnlyApi())
        payload = {"total": 6000, "items": _oversized_items(), "next_offset": None, "hint": "翻页"}
        text = guard_payload(payload)
        _register(mcp, "probe_page_envelope", lambda: guard_payload(payload))
        got = _call_text(mcp, "probe_page_envelope")
        self.assertNotIn(INSURANCE_SUFFIX, got)
        parsed = json.loads(got)
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertIsInstance(parsed["total_items"], int)  # 单字段形态
        self.assertNotIn("truncated_fields", parsed)
        self.assertLessEqual(frame_cost(got), max_output())

    def test_high_escape_density_payload_still_parses(self):
        """引号/反斜杠密度异常的载荷（转义后体积比 1.85×）靠 frame_cost 重新截断。"""
        mcp = create_mcp_app(ManifestOnlyApi())
        payload = {"items": [{"text": '"' * 60 + "\\" * 20 + "\n\t" * 10, "n": i} for i in range(4000)]}
        text = guard_payload(payload)  # 语义 cap 下若不重截，整帧必超保险层
        self.assertLessEqual(frame_cost(text), max_output())
        _register(mcp, "probe_pathological_density", lambda: guard_payload(payload))
        got = _call_text(mcp, "probe_pathological_density")
        self.assertNotIn(INSURANCE_SUFFIX, got)
        parsed = json.loads(got)
        self.assertTrue(parsed[TRUNCATED_KEY])
        self.assertGreater(len(parsed["items"]), 0)


class TestFrameCostContract(unittest.TestCase):
    """frame_cost 的字节等式必须与保险层实际序列化器逐字节一致（否则 126 字节包装常数会与实际脱节）。"""

    def test_frame_cost_matches_pydantic_serializer(self):
        import pydantic_core
        from fastmcp.tools.base import ToolResult
        from mcp_types import TextContent

        samples = (
            "plain ascii payload",
            "中文日志行" * 100,
            'quote"d and back\\slash' * 50,
            "tab\there\nnl\rret\bbs\ff" * 40,
            "ctrl\x00\x01\x1f\x0b\x0c" * 30,
            'mixed 中文 " \\ \n\t\x01' * 60,
        )
        for text in samples:
            real = len(
                pydantic_core.to_json(
                    ToolResult(content=[TextContent(type="text", text=text)]), fallback=str
                )
            )
            self.assertEqual(frame_cost(text), real, repr(text[:24]))

    def test_semantic_cap_is_below_insurance_cap(self):
        from backend.mcp_guard import SEMANTIC_RATIO

        self.assertLess(semantic_max_output(), max_output())
        self.assertEqual(semantic_max_output(), int(max_output() * SEMANTIC_RATIO))


if __name__ == "__main__":
    unittest.main()
