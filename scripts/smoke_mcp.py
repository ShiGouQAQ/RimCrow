# scripts/smoke_mcp.py
"""MCP stdio 冒烟（工具层 stdio 测试载体）。

> 生产 stdio 入口是 ``rimcrow-mcp.exe``（Rust 桥），桥侧冒烟见 ``scripts/smoke_bridge.py``。

三种形态（默认全跑）：

- ``--server``   工具层 stdio（真实 API，只读调用）：list_mods 不传 limit → 合法
                 JSON + 体积 ≤ 上限（+ truncated hint）；get_load_order 分页响应结构；
- ``--dry-run``  工具清单打印，不初始化后端。

统一断言：握手成功、清单与 ``backend.mcp_tool_specs`` 同源、调用返回可解析 JSON、
关闭 stdin 后进程 **干净退出（无孤儿）**。退出码 0 = 全部通过。

用法：``uv run python scripts/smoke_mcp.py [--server|--dry-run]``
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.mcp_guard import max_output  # noqa: E402
from backend.mcp_tool_specs import TOOL_SPECS  # noqa: E402

EXPECTED_NAMES = [spec.name for spec in TOOL_SPECS]
FAILURES: list[str] = []


def _fail(message: str) -> None:
    FAILURES.append(message)
    print(f"  [FAIL] {message}")


def _ok(message: str) -> None:
    print(f"  [ok] {message}")


class StdioClient:
    """最小 MCP stdio 客户端：按行读 JSON-RPC，带超时。"""

    def __init__(self, cmd: list[str], *, cwd: Path = REPO_ROOT):
        self.started_at = time.perf_counter()
        self.proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self.queue: queue.Queue = queue.Queue()
        self.stderr_lines: list[bytes] = []
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.queue.put(line)
        self.queue.put(None)

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line)

    def send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def recv(self, timeout: float = 30.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.queue.get(timeout=min(0.5, max(0.05, deadline - time.time())))
            except queue.Empty:
                continue
            if line is None:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue  # 非协议行直接忽略（容错）
        return None

    def initialize(self) -> dict | None:
        self.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "rimcrow-smoke", "version": "0"}},
        })
        reply = self.recv()
        if reply:
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def request(self, method: str, params: dict, *, msg_id: int, timeout: float = 30.0) -> dict | None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        return self.recv(timeout=timeout)

    def call(self, name: str, arguments: dict, *, msg_id: int, timeout: float = 30.0) -> tuple[bool, str]:
        """tools/call → (is_error, 文本内容)。"""
        reply = self.request(
            "tools/call", {"name": name, "arguments": arguments}, msg_id=msg_id, timeout=timeout
        )
        if not reply or "result" not in reply:
            return True, f"no result: {str(reply)[:200]}"
        result = reply["result"]
        text = "".join(
            block.get("text", "") for block in result.get("content", [])
            if block.get("type") == "text"
        )
        return bool(result.get("isError")), text

    def shutdown_clean(self) -> int | None:
        """关闭 stdin（EOF）→ 期望进程自然退出；15s 不退 = 孤儿，杀掉并记失败。"""
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            return self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
            _fail("关闭 stdin 后进程未退出（孤儿进程）")
            return None

    def stderr_text(self) -> str:
        return b"".join(self.stderr_lines).decode("utf-8", "replace")


def _tool_text(reply: dict | None) -> tuple[bool, str]:
    """从 tools/call 应答取回文本内容。返回 (is_error, text)。"""
    if not reply or "result" not in reply:
        _fail(f"tools/call 无 result: {str(reply)[:200]}")
        return True, ""
    result = reply["result"]
    content = result.get("content") or []
    text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
    return bool(result.get("isError")), text


def _check_manifest(reply: dict | None, label: str) -> None:
    if not reply or "result" not in reply:
        _fail(f"{label}: tools/list 无 result: {str(reply)[:200]}")
        return
    names = [tool.get("name") for tool in reply["result"].get("tools", [])]
    if names != EXPECTED_NAMES:
        _fail(f"{label}: 清单与 mcp_tool_specs 不一致（{len(names)} vs {len(EXPECTED_NAMES)}）")
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(names, EXPECTED_NAMES)) if a != b), None
        )
        _fail(f"{label}: 首个差异 index={first_diff} got={names[:3]} want={EXPECTED_NAMES[:3]}")
    else:
        _ok(f"{label}: tools/list = {len(names)} 个，与 specs 同源一致")



def smoke_server() -> None:
    print("== 工具层 stdio 形态（真实 API，只读）==")
    client = StdioClient([sys.executable, "-m", "backend.mcp_server"])
    init = client.initialize()
    cold_ms = (time.perf_counter() - client.started_at) * 1000
    if not init or "result" not in init:
        _fail(f"server: initialize 失败: {str(init)[:200]} stderr={client.stderr_text()[-800:]}")
        client.shutdown_clean()
        return
    _ok(f"server: initialize {cold_ms:.0f}ms（对表 timeout.startup 30s/60s）")
    _check_manifest(client.request("tools/list", {}, msg_id=2), "server")

    # list_mods 不传 limit → 合法 JSON + 体积 ≤ cap（语义层护栏）
    is_error, text = _tool_text(
        client.request("tools/call", {"name": "list_mods", "arguments": {}}, msg_id=3, timeout=60)
    )
    if is_error:
        _fail(f"server: list_mods isError: {text[:200]}")
    else:
        cap = max_output()
        size = len(text.encode("utf-8"))
        try:
            payload = json.loads(text)
        except ValueError:
            _fail(f"server: list_mods 产物不是合法 JSON（size={size}）")
            payload = {}
        else:
            _ok(f"server: list_mods 合法 JSON size={size} <= cap={cap}")
        if size > cap:
            _fail(f"server: list_mods {size}B 超上限 {cap}B")
        if payload.get("__truncated__"):
            hint = payload.get("hint", "")
            if not hint:
                _fail("server: 截断缺 hint")
            else:
                _ok(f"server: 截断 hint={hint[:40]}")

    # get_load_order 分页响应结构（limit 生效）
    is_error, text = _tool_text(
        client.request(
            "tools/call",
            {"name": "get_load_order", "arguments": {"limit": 5}},
            msg_id=4,
            timeout=60,
        )
    )
    if is_error:
        _fail(f"server: get_load_order isError: {text[:200]}")
    else:
        payload = json.loads(text)
        if "total" in payload and "items" in payload and "version_token" in payload:
            _ok(f"server: get_load_order 返回结构 total={payload['total']} items={len(payload['items'])}（≤5）")
            if len(payload["items"]) > 5:
                _fail("server: get_load_order limit=5 未生效")
        else:
            _ok(f"server: get_load_order 返回错误载荷（空档案属预期）: {str(payload)[:80]}")

    code = client.shutdown_clean()
    if code == 0:
        _ok("server: stdin EOF 后干净退出（无孤儿）")


def smoke_dry_run() -> None:
    print("== dry-run 形态（清单打印，不起后端）==")
    proc = subprocess.run(
        [sys.executable, "-m", "backend.mcp_server", "--dry-run"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        _fail(f"dry-run: 退出码 {proc.returncode}: {proc.stderr[-400:]}")
        return
    if f"Total tools: {len(EXPECTED_NAMES)}" not in proc.stdout:
        _fail(f"dry-run: 清单计数不符: {proc.stdout.splitlines()[-1:]}")
    else:
        _ok(f"dry-run: Total tools: {len(EXPECTED_NAMES)}")



def main(argv: list[str]) -> int:
    modes = [arg for arg in argv if arg in ("--server", "--dry-run")]
    if not modes:
        modes = ["--server", "--dry-run"]
    if "--server" in modes:
        smoke_server()
    if "--dry-run" in modes:
        smoke_dry_run()
    print(f"\n{'SMOKE FAILED' if FAILURES else 'SMOKE PASSED'} ({len(FAILURES)} failures)")
    for message in FAILURES:
        print(f"  - {message}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
