"""embed 端到端验证脚本（只读/短时，不碰 Steam 目录）。

阶段：
  1. 桌面模式启动（真实 GUI 窗口）→ 日志断言 mcp_endpoint_ready < window_created
     → attach 连入 → 全量工具调用（窗口开启期间连续 MCP 调用）→ 关窗 →
     endpoint 文件被清理
  2. --browser --no-open 无窗/无标签模式 → endpoint 就绪 → attach 全通 →
     进程列表无 msedgewebview2 增量 → 结束进程 → pid 陈旧识别
  3. token 鉴权：无 token/错 token → 401

attach 形态 = **直连 embed HTTP 端点**（fastmcp Client + StreamableHttpTransport）：
stdio 侧的握手/降级/转发/spawn 检查见 ``scripts/smoke_bridge.py``
（走 rimcrow-mcp.exe），本脚本只负责数据面（mcp-embed）自身的 E2E。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILURES: list[str] = []
PID_FILE = Path(os.environ.get("TEMP", ".")) / "embed_verify_ep.json"
CHILD_ENV = {**os.environ, "RIMCROW_MCP_ENDPOINT": str(PID_FILE)}  # 与 embed 写出位置对齐


def ok(msg: str) -> None:
    print(f"  [ok] {msg}", flush=True)


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  [FAIL] {msg}", flush=True)


def webview_count() -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process msedgewebview2 -ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True,
    )
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


def wait_for(predicate, timeout: float, interval: float = 0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def read_endpoint() -> dict | None:
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


ATTACH_TOOLS = ["list_mods", "get_load_order", "get_groups", "list_profiles",
                "list_log_files", "get_settings_overview", "get_mod_dependencies",
                "read_logs", "search_mods", "get_active_mod_list"]


def _tool_arguments(name: str) -> dict:
    if name == "get_mod_dependencies":
        return {"packageId": "ludeon.rimworld", "depth": 1}
    if name == "search_mods":
        return {"keyword": "rim", "limit": 5}
    if name == "read_logs":
        return {"tail": 5}
    return {}


def attach_roundtrip(label: str, ep: dict) -> None:
    """直连 embed HTTP 端点跑 10 工具（窗口开启期间连续 MCP 调用）。

    成熟客户端（fastmcp Client）接管 initialize/会话/传输，本函数只断言
    工具结果可解析与成功率——与 stdio 版断言的度量基准一致。
    """
    import anyio
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def _run() -> tuple[int, int, list[str]]:
        transport = StreamableHttpTransport(
            url=ep["url"], headers={"Authorization": f"Bearer {ep['token']}"}
        )
        problems: list[str] = []
        parseable = succeeded = 0
        async with Client(transport, name="embed-verify") as client:
            tools = await client.list_tools()
            if not tools:
                problems.append("tools/list 为空")
            for tool in ATTACH_TOOLS:
                try:
                    result = await client.call_tool(
                        tool, _tool_arguments(tool), raise_on_error=False, timeout=60
                    )
                except Exception as exc:  # noqa: BLE001 - 逐工具隔离，一个炸不拖垮全轮
                    problems.append(f"{tool} 调用异常: {exc}")
                    continue
                text = "".join(
                    block.text for block in result.content if getattr(block, "type", "") == "text"
                )
                try:
                    json.loads(text)
                    parseable += 1
                except ValueError:
                    problems.append(f"{tool} 返回非 JSON: {text[:120]}")
                    continue
                if not result.is_error:
                    succeeded += 1
        return parseable, succeeded, problems

    try:
        parseable, succeeded, problems = anyio.run(_run)
    except Exception as exc:  # noqa: BLE001 - 连不上 = attach 失败（端点/鉴权/传输）
        fail(f"{label}: attach 失败: {exc}")
        return
    for problem in problems:
        fail(f"{label}: {problem}")
    ok(f"{label}: attach initialize ok（fastmcp Client 直连 embed）")
    ok(f"{label}: {parseable}/10 可解析 JSON，{succeeded}/10 成功（窗口开启期间连续调用）")
    if succeeded < 5:
        fail(f"{label}: 成功调用 {succeeded} < 5（数据面未真正打通）")


def http_auth_check(ep: dict) -> None:
    """无 token / 错 token → 401。"""
    req = urllib.request.Request(ep["url"], data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    try:
        urllib.request.urlopen(req, timeout=5)
        fail("无 token 请求竟成功（应 401）")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            ok("无 token → 401（零工具执行）")
        else:
            fail(f"无 token 返回 {exc.code}（期望 401）")
    except Exception as exc:  # noqa: BLE001
        fail(f"无 token 检查异常: {exc}")
    req.add_header("Authorization", "Bearer wrong-token")
    try:
        urllib.request.urlopen(req, timeout=5)
        fail("错 token 请求竟成功（应 401）")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            ok("错 token → 401")
        else:
            fail(f"错 token 返回 {exc.code}（期望 401）")
    except Exception as exc:  # noqa: BLE001
        fail(f"错 token 检查异常: {exc}")


def stage_order_from_log() -> None:
    """桌面模式日志断言 mcp_endpoint_ready < window_created。"""
    # log_startup_perf 走 logger.debug → 文件 handler（级别 DEBUG）；
    # 工作树本地 data/config.json 的 debug_mode 必须为 true 才会落盘。
    log_path = REPO / "data" / "logs" / "app.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        fail(f"读取 app.log 失败: {exc}")
        return
    ready = text.find("stage=mcp_endpoint_ready")
    created = text.find("stage=window_created")
    if ready == -1:
        fail("日志缺 mcp_endpoint_ready 阶段戳（debug_mode=true？）")
        return
    if created == -1:
        fail("日志缺 window_created 阶段戳")
        return
    if ready < created:
        ok("mcp_endpoint_ready < window_created")
    else:
        fail(f"阶段顺序错误: ready@{ready} >= created@{created}")


def desktop_phase() -> None:
    print("== 阶段 1：桌面模式（真实窗口 + 端点清理）==", flush=True)
    if PID_FILE.exists():
        PID_FILE.unlink()
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=str(REPO), env=CHILD_ENV)
    ep: dict | None = None
    try:
        ep = wait_for(lambda: read_endpoint(), timeout=60)
        if not ep:
            fail("桌面模式 60s 内未写出 endpoint 文件")
            return
        ok(f"endpoint 就绪: {ep['url']} mode={ep['mode']} pid={ep['pid']}")
        stage_order_from_log()
        http_auth_check(ep)
        attach_roundtrip("desktop", ep)
    finally:
        # endpoint 里的 pid 才是真实 GUI 进程（Popen pid 是 uv venv 的 launcher）。
        _close_gracefully(proc, int(ep["pid"]) if ep else proc.pid)


def _close_window_by_pid(pid: int, title: str = "RimCrow") -> str:
    """向指定 pid 持有的指定标题窗口投递 WM_CLOSE（ctypes EnumWindows，纯 Python）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    hit = {"closed": False}

    def _on_window(hwnd, _lparam):  # noqa: ANN001 - ctypes 回调
        try:
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid:
                buffer = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buffer, 256)
                if buffer.value == title:
                    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                    hit["closed"] = True
        except Exception:  # noqa: BLE001 - 枚举途中单窗失败不中断
            pass
        return True  # 继续枚举

    user32.EnumWindows(enum_proc(_on_window), 0)
    return "closed" if hit["closed"] else "none"


def _close_gracefully(proc: subprocess.Popen, gui_pid: int) -> None:
    """关窗走 WM_CLOSE（触发 window.events.closed → api.cleanup → endpoint 清理）。

    目标 pid 取 **endpoint 里的 pid**（权威 GUI 进程）：uv venv 的 ``python.exe``
    是 launcher，Popen 拿到的 pid ≠ 实际跑 main.py 的子进程。
    按「pid 精确匹配 + 标题 RimCrow」过滤，绝不误伤用户自己的 RimCrow 窗口。
    窗口关不掉时降级为强杀——届时按替代度量基准走 pid 陈旧识别。
    """
    if proc.poll() is not None:
        time.sleep(1.0)
        _assert_endpoint_removed()
        return
    verdict = _close_window_by_pid(gui_pid)
    if verdict == "closed":
        ok(f"已向 GUI 进程 {gui_pid} 的 RimCrow 窗口投递 WM_CLOSE")
    else:
        fail(f"未找到 pid={gui_pid} 的 RimCrow 窗口（EnumWindows={verdict!r}）")
    try:
        proc.wait(timeout=30)
        ok("关窗（WM_CLOSE）后进程优雅退出")
        time.sleep(1.0)
        _assert_endpoint_removed()
        return
    except subprocess.TimeoutExpired:
        pass
    # 降级：强杀 GUI 进程 → 按替代度量基准验证（pid 陈旧被识别）
    fail("WM_CLOSE 未在 30s 内退出，改用强杀（按 pid 失效状态验证）")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Stop-Process -Id {gui_pid} -Force -ErrorAction SilentlyContinue"],
                   capture_output=True, text=True)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    time.sleep(1.0)
    _assert_endpoint_stale()


def _assert_endpoint_removed() -> None:
    if PID_FILE.exists():
        fail("退出后 endpoint 文件未清理")
    else:
        ok("退出后 endpoint 文件已清理（cleanup 钩子生效）")


def _assert_endpoint_stale() -> None:
    if not PID_FILE.exists():
        ok("endpoint 文件已被清理")
        return
    from backend.mcp_endpoint import probe_endpoint

    status, _info, reason = probe_endpoint(PID_FILE)
    if status == "stale":
        ok(f"强杀后 pid 陈旧被识别（替代判定方式）: {reason}")
    else:
        fail(f"强杀后 probe={status}（期望 stale）: {reason}")


def browser_phase() -> None:
    print("== 阶段 2：--browser --no-open（无标签页 + headless MCP）==", flush=True)
    if PID_FILE.exists():
        PID_FILE.unlink()
    before = webview_count()
    proc = subprocess.Popen([sys.executable, "main.py", "--browser", "--no-open"],
                            cwd=str(REPO), env=CHILD_ENV)
    gui_pid: int | None = None
    try:
        ep = wait_for(lambda: read_endpoint(), timeout=60)
        if not ep:
            fail("browser 模式 60s 内未写出 endpoint 文件")
            return
        if ep["mode"] != "browser":
            fail(f"endpoint mode={ep['mode']}（期望 browser）")
        else:
            ok(f"endpoint ready mode=browser url={ep['url']} pid={ep['pid']}")
        gui_pid = int(ep["pid"])
        after = webview_count()
        if after <= before:
            ok(f"browser: 无 msedgewebview2 增量（{before} → {after}）")
        else:
            fail(f"browser: msedgewebview2 增量 {before} → {after}")
        attach_roundtrip("browser-headless", ep)
    finally:
        # kill（而非优雅退出）→ 端点文件残留 → 验证 pid 陈旧识别
        for target in filter(None, (gui_pid, proc.pid)):
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"Stop-Process -Id {target} -Force -ErrorAction SilentlyContinue"],
                           capture_output=True, text=True)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    time.sleep(0.5)
    ep = read_endpoint()
    if ep is None:
        ok("kill 后 endpoint 文件也被清理（atexit 清理生效）")
        return
    from backend.mcp_endpoint import probe_endpoint

    status, _info, reason = probe_endpoint(PID_FILE)
    if status == "stale":
        ok(f"kill 后 pid 陈旧被识别: {reason}")
    else:
        fail(f"kill 后 probe={status}（期望 stale）: {reason}")


def main() -> int:
    desktop_phase()
    browser_phase()
    print(f"\n{'VERIFY FAILED' if FAILURES else 'VERIFY PASSED'} ({len(FAILURES)} failures)", flush=True)
    for item in FAILURES:
        print(f"  - {item}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
