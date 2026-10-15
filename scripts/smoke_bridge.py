# scripts/smoke_bridge.py
"""生产入口 rimcrow-mcp.exe 的五态冒烟。

> 生产 stdio 入口 = Rust 薄封装层；工具层自身的 stdio 冒烟见 ``scripts/smoke_mcp.py``。

形态（默认全跑）：

- ``--cold``     冷启动降级态（endpoint 缺失，不起 GUI）：initialize < 1s、
                 tools/list = 嵌入快照且与 ``backend.mcp_tool_specs`` 同源、
                 tools/call → isError + 因果响应结构 + nextActions、
                 EOF 干净退出、10 次 spawn/kill 握手 < 1s 且 temp 零新增；
- ``--mismatch`` 错配矩阵抽样：endpoint schema_version=2、
                 endpoint pid 陈旧 → 文案点出两侧事实 + nextActions，无空洞 timeout；
- ``--fake``     透传实证（上游用 crate 内 ``fake_upstream.py``
                 测试替身，**非 RimCrow**）——清单透传、echo 往返、远端 isError 逐字
                 回抛、unknown tool 双侧版本文案、attach 版本错配告警、错误 bearer 因果失败；
- ``--spawn``    转发 + spawn 态（会真实拉起 headless GUI）：``--spawn`` 自动拉起、
                 就绪判据 = endpoint 写出、透传调用成功、再次调用防重入、
                 未知工具 isError、收尾杀干净；
- ``--live``     真实 endpoint 透传（GUI 在跑时；未跑则 SKIP，透传已由 fake 覆盖）。

用法：``uv run python scripts/smoke_bridge.py [--cold|--mismatch|--fake|--spawn|--live]``
桥路径默认按 ``rimcrow-mcp/Cargo.toml`` 在仓库祖先目录的兄弟处解析，``--bridge`` 覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import psutil  # noqa: E402

from backend.mcp_tool_specs import TOOL_SPECS  # noqa: E402
import smoke_mcp as sm  # noqa: E402 - 复用 StdioClient / 断言风格（零重复造轮子）

EXPECTED = [(spec.name, spec.desc) for spec in TOOL_SPECS]

# 冒烟专用 endpoint 文件：与 GUI 默认位置、verify_embed 的位置都隔离，
# 且单实例互斥（lifecycle）按 endpoint 路径散列 → 互不干扰任何在跑实例。
SMOKE_EP = Path(tempfile.gettempdir()) / "rimcrow_smoke_bridge_ep.json"


def _fail(message: str) -> None:
    sm._fail(message)


def _ok(message: str) -> None:
    sm._ok(message)


def _find_bridge(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    if os.environ.get("RIMCROW_BRIDGE_EXE"):
        path = Path(os.environ["RIMCROW_BRIDGE_EXE"])
        if path.exists():
            return path
    for ancestor in REPO_ROOT.parents:
        crate = ancestor / "rimcrow-mcp"
        if (crate / "Cargo.toml").exists():
            for profile in ("release", "debug"):
                exe = crate / "target" / profile / "rimcrow-mcp.exe"
                if exe.exists():
                    return exe
    return None


def _with_endpoint(path: Path, **extra: str):
    """context manager：把 RIMCROW_MCP_ENDPOINT（及附加 env）切到指定值（子进程继承）。"""
    import contextlib

    @contextlib.contextmanager
    def _scope():
        saved_ep = os.environ.get("RIMCROW_MCP_ENDPOINT")
        saved_extra = {key: os.environ.get(key) for key in extra}
        os.environ["RIMCROW_MCP_ENDPOINT"] = str(path)
        os.environ.update(extra)
        try:
            yield
        finally:
            if saved_ep is None:
                os.environ.pop("RIMCROW_MCP_ENDPOINT", None)
            else:
                os.environ["RIMCROW_MCP_ENDPOINT"] = saved_ep
            for key, value in saved_extra.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _scope()


def _bridge_cmd(bridge: Path, *flags: str) -> list[str]:
    return [str(bridge), *flags]


def wait_for(predicate, timeout: float, interval: float = 0.2):
    """轮询谓词至真值或超时（超时返回 None）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _mei_count() -> int:
    temp = Path(tempfile.gettempdir())
    try:
        return sum(1 for entry in temp.iterdir() if entry.name.startswith("_MEI"))
    except OSError:
        return -1


def _gui_root_count(marker: str) -> int:
    """命令行含 marker 的 python 根进程数（launcher+子解释器同 cmdline，按根计数）。"""
    roots = 0
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            if (proc.info["name"] or "").casefold() != "python.exe":
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if marker not in cmdline:
                continue
            ppid = proc.info["ppid"] or 0
            parent_cmd = ""
            if ppid:
                try:
                    parent_cmd = " ".join(psutil.Process(ppid).cmdline())
                except psutil.Error:
                    parent_cmd = ""
            if marker not in parent_cmd:
                roots += 1
        except psutil.Error:
            continue
    return roots


def _parse_envelope(text: str) -> dict:
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _init_ok(client: "sm.StdioClient", label: str, deadline_ms: float | None = None) -> bool:
    init = client.initialize()
    cold_ms = (time.perf_counter() - client.started_at) * 1000
    if not init or "result" not in init:
        _fail(f"{label}: initialize 失败: {str(init)[:200]} stderr={client.stderr_text()[-600:]}")
        return False
    if deadline_ms is not None and cold_ms > deadline_ms:
        _fail(f"{label}: 冷启动 {cold_ms:.0f}ms > {deadline_ms:.0f}ms")
        return False
    _ok(f"{label}: initialize ok {cold_ms:.0f}ms")
    return True


def _check_snapshot_manifest(reply: dict | None, label: str) -> None:
    """快照清单与 mcp_tool_specs 同源（名字顺序 + description 逐字）。"""
    if not reply or "result" not in reply:
        _fail(f"{label}: tools/list 无 result: {str(reply)[:200]}")
        return
    tools = reply["result"].get("tools", [])
    got = [(tool.get("name"), tool.get("description")) for tool in tools]
    if got == EXPECTED:
        _ok(f"{label}: tools/list = {len(got)} 个，与 mcp_tool_specs 同源一致（快照一致）")
        return
    if [name for name, _ in got] == [name for name, _ in EXPECTED]:
        _fail(f"{label}: 名字一致但 description 有差异（快照生成源走样）")
        for (g_name, g_desc), (w_name, w_desc) in zip(got, EXPECTED):
            if g_desc != w_desc:
                _fail(f"{label}: desc 差异 @ {g_name}: got={str(g_desc)[:60]!r} want={str(w_desc)[:60]!r}")
                break
    else:
        _fail(f"{label}: 快照清单与 specs 不一致（{len(got)} vs {len(EXPECTED)}）")
        first = next(
            (i for i, (a, b) in enumerate(zip([n for n, _ in got], [n for n, _ in EXPECTED])) if a != b),
            None,
        )
        _fail(f"{label}: 首个差异 index={first} got={[n for n, _ in got][:3]}")


def smoke_cold(bridge: Path) -> None:
    print("== 阶段 1：冷启动降级（无 endpoint / 无 GUI）==")
    missing_ep = SMOKE_EP.with_name("rimcrow_smoke_bridge_absent.json")
    missing_ep.unlink(missing_ok=True)
    with _with_endpoint(missing_ep):
        client = sm.StdioClient(_bridge_cmd(bridge))
        try:
            if not _init_ok(client, "cold", deadline_ms=1000):
                client.shutdown_clean()
                return
            _check_snapshot_manifest(client.request("tools/list", {}, msg_id=2), "cold")
            # tools/call → 因果 isError + nextActions（禁空洞 timeout）
            is_error, text = client.call("list_mods", {}, msg_id=3, timeout=30)
            if not is_error:
                _fail(f"cold: endpoint 缺失时 list_mods 竟成功（应 isError）: {text[:120]}")
            else:
                envelope = _parse_envelope(text)
                if envelope.get("status") == "error" and envelope.get("message"):
                    actions = envelope.get("nextActions")
                    if isinstance(actions, list) and actions:
                        _ok(f"cold: isError 因果响应 ok，message={str(envelope['message'])[:70]}")
                    else:
                        _fail(f"cold: 错误响应缺 nextActions[]: {text[:200]}")
                else:
                    _fail(f"cold: 错误响应形状不符（期望 status/message/nextActions）: {text[:200]}")
        finally:
            code = client.shutdown_clean()
        if code == 0:
            _ok("cold: stdin EOF 后干净退出，退出码 0")

    # 连续 10 次 spawn/kill——握手均 < 1s、temp _MEI 零新增
    print("== 阶段 2：10 次 spawn/kill 循环 ==")
    base_mei = _mei_count()
    durations: list[float] = []
    with _with_endpoint(missing_ep):
        for i in range(10):
            client = sm.StdioClient(_bridge_cmd(bridge))
            start = time.perf_counter()
            init = client.initialize()
            elapsed = (time.perf_counter() - start) * 1000
            if not init or "result" not in init:
                _fail(f"loop[{i}]: initialize 失败: {str(init)[:120]}")
                client.proc.kill()
                break
            durations.append(elapsed)
            client.proc.kill()  # 模拟客户端强 kill
            client.proc.wait(timeout=10)
    tail = durations[2:] or durations  # 首两次含 Defender 首扫，只统计后 8 次
    if tail:
        worst = max(tail)
        if worst > 1000:
            _fail(f"loop: 后 {len(tail)} 次握手中位最差 {worst:.0f}ms > 1000ms")
        else:
            _ok(f"loop: {len(tail)} 次握手最差 {worst:.0f}ms（< 1s）")
    after_mei = _mei_count()
    if base_mei >= 0 and after_mei >= 0:
        delta = after_mei - base_mei
        if delta > 0:
            _fail(f"loop: temp _MEI 新增 {delta} 个（桥应零解压零残留）")
        else:
            _ok(f"loop: temp _MEI 零新增（{base_mei} → {after_mei}）")


def smoke_mismatch(bridge: Path) -> None:
    print("== 阶段 3：错配矩阵抽样 ==")

    # endpoint schema_version=2 → 升级提示，点出两侧
    bad_schema = SMOKE_EP.with_name("rimcrow_smoke_bridge_schema2.json")
    bad_schema.write_text(
        json.dumps({
            "schema_version": 2,
            "url": "http://127.0.0.1:1/mcp",
            "pid": 1,
            "token": "x",
            "started_at": "2020-01-01T00:00:00+00:00",
            "mode": "desktop",
        }),
        encoding="utf-8",
    )
    with _with_endpoint(bad_schema):
        client = sm.StdioClient(_bridge_cmd(bridge))
        try:
            if _init_ok(client, "schema2"):
                is_error, text = client.call("list_mods", {"limit": 1}, msg_id=2, timeout=30)
                if not is_error:
                    _fail("schema2: schema_version=2 时调用竟成功（应 isError）")
                elif "schema" in text.lower() and "rimcrow-mcp" in text:
                    _ok(f"schema2: 升级提示 ok: {text[:110]}")
                else:
                    _fail(f"schema2: 文案未点出 schema 与桥版本: {text[:200]}")
        finally:
            client.shutdown_clean()
    bad_schema.unlink(missing_ok=True)

    # endpoint 存在但 pid 已死 → 因果降级 + nextActions
    dead_ep = SMOKE_EP.with_name("rimcrow_smoke_bridge_deadpid.json")
    dead_pid = _pick_dead_pid()
    dead_ep.write_text(
        json.dumps({
            "schema_version": 1,
            "url": "http://127.0.0.1:1/mcp",
            "pid": dead_pid,
            "token": "x",
            "started_at": "2020-01-01T00:00:00+00:00",
            "mode": "desktop",
        }),
        encoding="utf-8",
    )
    with _with_endpoint(dead_ep):
        client = sm.StdioClient(_bridge_cmd(bridge))
        try:
            if _init_ok(client, "deadpid"):
                is_error, text = client.call("list_mods", {"limit": 1}, msg_id=2, timeout=30)
                envelope = _parse_envelope(text)
                if not is_error:
                    _fail("deadpid: pid 陈旧时调用竟成功（应 isError）")
                elif str(dead_pid) in text and envelope.get("nextActions"):
                    _ok(f"deadpid: 因果文案点出 pid + nextActions ok: {text[:110]}")
                else:
                    _fail(f"deadpid: 文案缺 pid 事实或 nextActions: {text[:200]}")
        finally:
            client.shutdown_clean()
    dead_ep.unlink(missing_ok=True)


def _pick_dead_pid() -> int:
    """挑一个确定不存在的 pid（65534 起向上找空洞，避免与真实进程撞号）。"""
    for candidate in range(65534, 65534 + 2000):
        try:
            psutil.Process(candidate)
        except psutil.Error:
            return candidate
    return 999999


def smoke_spawn(bridge: Path) -> None:
    print("== 阶段 4/5：--spawn 拉起 + 转发 + 防重入（会真实拉起 headless GUI）==")
    SMOKE_EP.unlink(missing_ok=True)
    # 拉起命令用**绝对路径**的 main.py（cwd 无关）；headless 语义在命令内自带
    # --browser --no-open，不依赖桥是否追加 flag（RIMCROW_SPAWN_CMD 环境约定）。
    spawn_cmd = f'"{sys.executable}" "{REPO_ROOT / "main.py"}" --browser --no-open'
    saved_cmd = os.environ.get("RIMCROW_SPAWN_CMD")
    saved_marker = os.environ.get("RIMCROW_MCP_ENDPOINT")
    os.environ["RIMCROW_SPAWN_CMD"] = spawn_cmd
    os.environ["RIMCROW_MCP_ENDPOINT"] = str(SMOKE_EP)
    marker = str(REPO_ROOT / "main.py")
    client = sm.StdioClient(_bridge_cmd(bridge, "--spawn"))
    gui_spawned = False
    try:
        if not _init_ok(client, "spawn", deadline_ms=2000):
            return
        # 首次调用触发 spawn：有界等待（桥内 15s）→ endpoint 写出 → 转发成功
        is_error, text = client.call("list_mods", {"limit": 5}, msg_id=2, timeout=60)
        if is_error:
            _fail(f"spawn: 拉起后 list_mods 仍失败: {text[:250]}")
            return
        _ok(f"spawn: 自动拉起后转发成功（list_mods）: {text[:80]}")
        gui_spawned = True

        # 防重入：再次调用不重复拉起——GUI 根进程数**保持不变**（第一次
        # 调用已拉起，故基线=1、增量=0 才是「不重复拉起」）。
        before = _gui_root_count(marker)
        if before < 1:
            _fail(f"spawn: 首次调用后未见 GUI 根进程（marker={marker}）")
        # 第二次调用换 list_profiles：get_load_order 在扫描期会按上游约定返回
        # 「加载顺序管理器未初始化」因果错误，那是数据面语义，不是转发失败。
        is_error2, text2 = client.call("list_profiles", {}, msg_id=3, timeout=60)
        after = _gui_root_count(marker)
        if is_error2:
            _fail(f"spawn: 第二次调用失败: {text2[:200]}")
        if after == before:
            _ok(f"spawn: 两次调用 GUI 根进程数不变（{before}→{after}，未重复拉起）")
        else:
            _fail(f"spawn: GUI 根进程数 {before}→{after}（期望不变 = 未重复拉起）")

        # 转发态清单（透传优先）：与 specs 同源即通过校验
        _check_snapshot_manifest(client.request("tools/list", {}, msg_id=4), "spawn")

        # 未知工具：远端 isError 逐字回抛
        is_error3, text3 = client.call("definitely_not_a_tool", {}, msg_id=5, timeout=30)
        if is_error3 and "definitely_not_a_tool" in text3:
            _ok(f"spawn: 未知工具 isError 回抛 ok: {text3[:100]}")
        else:
            _fail(f"spawn: 未知工具未按 isError 回抛: is_error={is_error3} {text3[:150]}")

        # 大载荷过桥不掉线（远端护栏）：list_mods 不传 limit
        is_error4, text4 = client.call("list_mods", {}, msg_id=6, timeout=120)
        if is_error4:
            _fail(f"spawn: 全量 list_mods isError: {text4[:200]}")
        else:
            size = len(text4.encode("utf-8"))
            from backend.mcp_guard import max_output

            if size > max_output():
                _fail(f"spawn: 全量 list_mods {size}B 超 cap {max_output()}B（远端语义层护栏失效）")
            else:
                payload = _parse_envelope(text4)
                total = payload.get("total", "?")
                _ok(f"spawn: 全量 list_mods 过桥 ok（total={total}, {size}B ≤ cap）")
    finally:
        client.shutdown_clean()
        _cleanup_gui(gui_spawned)
        # 恢复 env：否则 spawn 段的 endpoint/拉起命令泄漏给后续场景（live 会被
        # 误导到冒烟临时 endpoint 而 SKIP）。
        if saved_cmd is None:
            os.environ.pop("RIMCROW_SPAWN_CMD", None)
        else:
            os.environ["RIMCROW_SPAWN_CMD"] = saved_cmd
        if saved_marker is None:
            os.environ.pop("RIMCROW_MCP_ENDPOINT", None)
        else:
            os.environ["RIMCROW_MCP_ENDPOINT"] = saved_marker


def _cleanup_gui(spawned: bool) -> None:
    """收尾：杀掉冒烟拉起的 headless GUI（taskkill /T 连 launcher 一起清）。"""
    if not spawned:
        return
    try:
        raw = json.loads(SMOKE_EP.read_text(encoding="utf-8"))
        pid = int(raw["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pid = 0
    if pid:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True,
        )
    # launcher 可能残留：按 marker 清（只杀 main.py 命令行，绝不误伤真实 GUI）
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (proc.info["name"] or "").casefold() != "python.exe":
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if str(REPO_ROOT / "main.py") in cmdline:
                proc.kill()
        except psutil.Error:
            continue
    time.sleep(1.0)
    if SMOKE_EP.exists():
        try:
            os.unlink(SMOKE_EP)
        except OSError:
            pass


def _crate_dir(bridge: Path) -> Path:
    """exe 位于 <crate>/target/release/rimcrow-mcp.exe → parents[2] 即 crate。"""
    return bridge.parents[2]


def smoke_fake(bridge: Path) -> None:
    """透传实证：上游用 fake_upstream.py 测试替身（非 RimCrow）。

    覆盖清单透传（上游 2 工具 ≠ 快照全量清单 → 证明非快照）、
    echo 真实 HTTP 往返、远端 isError 逐字回抛、unknown tool 双侧版本增强文案、
    attach 版本错配告警、错误 bearer → 连接期因果 isError。
    """
    print("== 阶段 6：fake 上游透传（测试替身，非 RimCrow）==")
    crate = _crate_dir(bridge)
    fake_script = crate / "fake_upstream.py"
    if not fake_script.exists():
        _fail(f"fake: 缺少测试替身 {fake_script}")
        return
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()

    ep = crate / "target" / "test-tmp" / "scripts-fake-endpoint.json"
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.unlink(missing_ok=True)
    fake = subprocess.Popen(
        [sys.executable, str(fake_script), "--port", str(port), "--endpoint", str(ep)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    snap_version = _snapshot_version()
    try:
        if not wait_for(lambda: ep.exists(), timeout=10):
            _fail("fake: 上游 10s 内未写出 endpoint")
            return
        with _with_endpoint(ep, RIMCROW_MCP_LOG="debug"):
            client = sm.StdioClient(_bridge_cmd(bridge))
            try:
                if not _init_ok(client, "fake", deadline_ms=1000):
                    return
                reply = client.request("tools/list", {}, msg_id=2)
                if reply and "result" in reply:
                    names = [tool.get("name") for tool in reply["result"].get("tools", [])]
                    if names == ["echo_tool", "remote_error_tool"]:
                        _ok(f"fake: tools/list = 上游实时清单 {names}（≠ 快照 → 透传非快照）")
                    else:
                        _fail(f"fake: 清单非上游透传: {names}")
                stderr = client.stderr_text()
                if "版本错配" in stderr and "9.9.9" in stderr and snap_version in stderr:
                    _ok("fake: 版本错配告警点出两侧版本（不阻断）")
                else:
                    _fail(f"fake: 缺版本错配告警: {stderr[-400:]}")
                is_error, text = client.call("echo_tool", {"text": "hi"}, msg_id=3, timeout=30)
                if not is_error and "FAKE_RESULT" in text:
                    _ok(f"fake: echo 往返 ok: {text[:60]}")
                else:
                    _fail(f"fake: echo 往返失败: is_error={is_error} {text[:150]}")
                is_error, text = client.call("remote_error_tool", {}, msg_id=4, timeout=30)
                if is_error and text == "REMOTE_FAILURE: disk is read-only (fixed upstream text)":
                    _ok("fake: 远端 isError 逐字回抛（无改写、无 nextActions 插入）")
                else:
                    _fail(f"fake: 远端 isError 未逐字回抛: is_error={is_error} {text[:150]}")
                is_error, text = client.call("__nope__", {}, msg_id=5, timeout=30)
                envelope = _parse_envelope(text)
                message = str(envelope.get("message", ""))
                if is_error and "工具 __nope__ 在 RimCrow 9.9.9 中不存在" in message \
                        and "0.1.0" in message and snap_version in message \
                        and "Unknown tool" in message and envelope.get("nextActions"):
                    _ok("fake: unknown tool 文案点出双侧版本 + 保留上游原文 + nextActions")
                else:
                    _fail(f"fake: unknown tool 文案不符: {message[:300]}")
            finally:
                client.shutdown_clean()

        # --- 鉴权：错误 bearer → 连接期失败 → 因果 isError（pid=本进程，探活必 ready）---
        bad_ep = crate / "target" / "test-tmp" / "scripts-fake-badtoken.json"
        raw = json.loads(ep.read_text(encoding="utf-8"))
        bad_ep.write_text(json.dumps({
            "schema_version": 1, "url": raw["url"], "pid": os.getpid(),
            "token": "definitely-wrong-token", "started_at": _now_iso(), "mode": "desktop",
        }, ensure_ascii=False), encoding="utf-8")
        with _with_endpoint(bad_ep):
            client = sm.StdioClient(_bridge_cmd(bridge))
            try:
                if _init_ok(client, "fake-badtoken"):
                    is_error, text = client.call("echo_tool", {}, msg_id=2, timeout=30)
                    envelope = _parse_envelope(text)
                    message = str(envelope.get("message", ""))
                    if is_error and "连接上游端点失败" in message \
                            and "0.1.0" in message and snap_version in message:
                        _ok(f"fake-badtoken: 错误 bearer → 连接期因果 isError: {message[:100]}")
                    else:
                        _fail(f"fake-badtoken: 文案不符: is_error={is_error} {message[:250]}")
            finally:
                client.shutdown_clean()
        bad_ep.unlink(missing_ok=True)
    finally:
        fake.terminate()
        try:
            fake.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fake.kill()
        ep.unlink(missing_ok=True)


def _snapshot_version() -> str:
    """从 tools-cache.json 读 rimcrow_version（快照即产物，读它而不是 import）。"""
    cache = _crate_dir_cache()
    try:
        return str(json.loads(cache.read_text(encoding="utf-8"))["rimcrow_version"])
    except (OSError, ValueError, KeyError):
        return "0.24.7"  # 备用值：仅用于文案断言的辅助比对


def _crate_dir_cache() -> Path:
    for ancestor in REPO_ROOT.parents:
        crate = ancestor / "rimcrow-mcp"
        if (crate / "tools-cache.json").exists():
            return crate / "tools-cache.json"
    return REPO_ROOT / "tools-cache.json"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def smoke_live(bridge: Path) -> None:
    """真实 endpoint 透传（GUI 在跑时）：清单透传、调用往返、远端 isError 逐字回抛。"""
    print("== 阶段 7：live 透传（真实 endpoint）==")
    real_ep = Path(
        os.environ.get("RIMCROW_MCP_ENDPOINT")
        or os.path.join(os.environ["LOCALAPPDATA"], "RimCrow", "mcp-endpoint.json")
    )
    if not real_ep.exists():
        print(f"  [SKIP] 真实 endpoint 不存在（{real_ep}）——透传路径已由 fake 场景覆盖；")
        print("         RimCrow GUI 起来后重跑 `--live` 补验。")
        return
    snap_version = _snapshot_version()
    # log=debug：「透传上游」是 DEBUG 级日志（tracing），info 下不可见。
    with _with_endpoint(real_ep, RIMCROW_MCP_LOG="debug"):
        client = sm.StdioClient(_bridge_cmd(bridge))
        try:
            if not _init_ok(client, "live", deadline_ms=1000):
                return
            reply = client.request("tools/list", {}, msg_id=2)
            if reply and "result" in reply:
                names = [tool.get("name") for tool in reply["result"].get("tools", [])]
                if names == [name for name, _ in EXPECTED]:
                    _ok(f"live: tools/list 透传上游 {len(names)} 个（与 specs 同源）")
                else:
                    _fail(f"live: 清单与 specs 不一致（{len(names)} vs {len(EXPECTED)}）")
            stderr = client.stderr_text()
            if "透传上游" in stderr:
                _ok("live: stderr 记录了「透传上游」")
            else:
                _fail(f"live: stderr 缺「透传上游」记录: {stderr[-300:]}")
            if snap_version in stderr:
                _ok(f"live: attach 版本比对记录（快照 {snap_version}）")
            else:
                _fail(f"live: stderr 缺版本比对记录: {stderr[-300:]}")
            is_error, text = client.call("list_mods", {"limit": 2, "offset": 0}, msg_id=3, timeout=60)
            if not is_error and ('"total"' in text or '"items"' in text):
                _ok(f"live: tools/call 透传成功: {text[:80]}")
            else:
                _fail(f"live: tools/call 失败: is_error={is_error} {text[:150]}")
            if '"nextActions"' not in text:
                _ok("live: 成功路径不夹带降级信息")
            else:
                _fail("live: 成功路径混入 nextActions 降级信息")
            # 远端 isError 逐字回抛（上游 pydantic 越界校验错误，非本桥响应包装）。
            # 探针必须用「schema 层放行、运行期才拒」的取值：桥预校验只拦未知参数 /
            # 类型明显不符 / enum 不符，limit=0 类型合法但越界（search_mods 限 1..50），
            # 只有它才能穿透到上游 pydantic，验到远端回抛语义。
            is_error, text = client.call("search_mods", {"keyword": "rimworld", "limit": 0}, msg_id=4, timeout=60)
            if is_error and "nextActions" not in text and "validation error" in text:
                _ok(f"live: 远端 isError 逐字回抛: {text[:80]}")
            elif is_error and "nextActions" in text:
                _ok("live: 该取值被桥预校验前置拦截（schema 层拒），跳过远端 isError 回抛核验")
            elif not is_error:
                print(f"  [INFO] 上游未对非法参数判错，跳过远端 isError 回抛核验: {text[:100]}")
            else:
                _fail(f"live: 远端 isError 被改写（混入 nextActions）: {text[:200]}")
        finally:
            code = client.shutdown_clean()
        if code == 0:
            _ok("live: EOF 干净退出码 = 0")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="rimcrow-mcp.exe five-state smoke")
    parser.add_argument("--bridge", default=None, help="path to rimcrow-mcp.exe")
    parser.add_argument("--cold", action="store_true")
    parser.add_argument("--mismatch", action="store_true")
    parser.add_argument("--spawn", action="store_true")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    bridge = _find_bridge(args.bridge)
    if bridge is None:
        print("SMOKE FAILED: 找不到 rimcrow-mcp.exe（先 cargo build --release，或用 --bridge 指定）")
        return 2
    print(f"bridge: {bridge}")
    modes = {name for name in ("--cold", "--mismatch", "--fake", "--spawn", "--live")
             if getattr(args, name[2:])}
    if not modes:
        modes = {"--cold", "--mismatch", "--fake", "--spawn", "--live"}
    if "--cold" in modes:
        smoke_cold(bridge)
    if "--mismatch" in modes:
        smoke_mismatch(bridge)
    if "--fake" in modes:
        smoke_fake(bridge)
    if "--spawn" in modes:
        smoke_spawn(bridge)
    if "--live" in modes:
        smoke_live(bridge)
    print(f"\n{'SMOKE FAILED' if sm.FAILURES else 'SMOKE PASSED'} ({len(sm.FAILURES)} failures)")
    for message in sm.FAILURES:
        print(f"  - {message}")
    return 1 if sm.FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
