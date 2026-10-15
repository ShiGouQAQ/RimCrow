# tests/test_single_instance.py
"""backend/lifecycle 单测：数据面单实例互斥。

互斥域由 endpoint 路径决定，因此所有用例一律通过 ``RIMCROW_MCP_ENDPOINT``
（tmp_path 独立文件）或 ``RIMCROW_NO_SINGLE_INSTANCE`` 切换——
保证本文件可以与其它测试、与本机正在跑的 RimCrow GUI 并行互不干扰；
不存在任何用例去抢占「默认 endpoint」那把锁（那会和真实 GUI 抢）。

互斥行为（占用/释放/跨进程）是 Windows 内核对象语义，非 Windows 平台自动跳过；
互斥名推导是纯函数，全平台可跑。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.lifecycle import (
    ESCAPE_ENV,
    MUTEX_PREFIX,
    STATUS_ACQUIRED,
    STATUS_BUSY,
    STATUS_SKIPPED,
    SingleInstanceResult,
    acquire_single_instance,
    endpoint_mutex_name,
    release_single_instance,
    report_second_instance,
)
from backend.mcp_endpoint import write_endpoint

IS_WINDOWS = sys.platform == "win32"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _use_endpoint(monkeypatch, path: Path) -> None:
    monkeypatch.setenv("RIMCROW_MCP_ENDPOINT", str(path))


def _fresh_lock(monkeypatch, tmp_path: Path, name: str = "ep.json") -> SingleInstanceResult:
    """在独立互斥域里拿一把锁（grace=0 立即判定）。"""
    _use_endpoint(monkeypatch, tmp_path / name)
    return acquire_single_instance(grace_seconds=0)


# ---------- 互斥名推导（纯函数，全平台） ----------


def test_mutex_name_is_stable_and_well_formed(monkeypatch, tmp_path):
    _use_endpoint(monkeypatch, tmp_path / "ep.json")
    name = endpoint_mutex_name()
    # 同一 endpoint 路径 → 每次调用同名
    assert name == endpoint_mutex_name()
    # 形制：Local\RimCrowEndpoint-<16 位小写 hex>
    assert name.startswith(MUTEX_PREFIX)
    suffix = name[len(MUTEX_PREFIX):]
    assert len(suffix) == 16
    assert all(char in "0123456789abcdef" for char in suffix)


def test_mutex_name_normalizes_path_case():
    # Windows 命名内核对象不区分大小写 → 哈希前统一小写
    assert endpoint_mutex_name(r"C:\Temp\RimCrow\mcp-endpoint.json") == \
        endpoint_mutex_name(r"c:\temp\rimcrow\MCP-ENDPOINT.JSON")


def test_mutex_name_differs_per_endpoint(monkeypatch, tmp_path):
    _use_endpoint(monkeypatch, tmp_path / "a.json")
    name_a = endpoint_mutex_name()
    _use_endpoint(monkeypatch, tmp_path / "b.json")
    name_b = endpoint_mutex_name()
    assert name_a != name_b
    # 默认路径（无 env）也是一把独立的锁
    monkeypatch.delenv("RIMCROW_MCP_ENDPOINT", raising=False)
    name_default = endpoint_mutex_name()
    assert name_default not in {name_a, name_b}


# ---------- 获取 / 占用 / 释放（Windows 内核对象语义） ----------


@pytest.mark.skipif(not IS_WINDOWS, reason="named mutex 仅 Windows")
def test_second_acquire_reports_busy_until_release(monkeypatch, tmp_path):
    first = _fresh_lock(monkeypatch, tmp_path)
    assert first.status == STATUS_ACQUIRED
    assert first.handle is not None

    # 同进程内二次获取同一 endpoint → 已占用
    second = acquire_single_instance(grace_seconds=0)
    assert second.status == STATUS_BUSY
    assert second.name == first.name
    assert second.handle is None

    # 释放后可再次获取：证明占用判定来自内核对象，而不是「文件存在」之类的软判断
    release_single_instance(first)
    third = acquire_single_instance(grace_seconds=0)
    assert third.status == STATUS_ACQUIRED
    release_single_instance(third)


@pytest.mark.skipif(not IS_WINDOWS, reason="named mutex 仅 Windows")
def test_distinct_endpoints_do_not_contend(monkeypatch, tmp_path):
    lock_a = _fresh_lock(monkeypatch, tmp_path, "a.json")
    lock_b = _fresh_lock(monkeypatch, tmp_path, "b.json")
    try:
        assert lock_a.status == STATUS_ACQUIRED
        assert lock_b.status == STATUS_ACQUIRED
        assert lock_a.name != lock_b.name
    finally:
        release_single_instance(lock_a)
        release_single_instance(lock_b)


@pytest.mark.skipif(not IS_WINDOWS, reason="named mutex 仅 Windows")
def test_cross_process_holder_makes_acquire_busy(monkeypatch, tmp_path):
    """真实跨进程：子进程持锁期间，父进程同 endpoint 获取 → busy。"""
    _use_endpoint(monkeypatch, tmp_path / "ep.json")
    child_code = (
        "from backend.lifecycle import acquire_single_instance, STATUS_ACQUIRED;"
        "r = acquire_single_instance(grace_seconds=0);"
        "print(r.status, flush=True);"
        "import sys; sys.stdin.readline()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline().strip()
        assert line == STATUS_ACQUIRED, f"child failed to acquire: {line!r}"
        parent = acquire_single_instance(grace_seconds=0)
        assert parent.status == STATUS_BUSY
        assert parent.name == endpoint_mutex_name()
    finally:
        if child.stdin:
            child.stdin.close()
        child.wait(timeout=15)


# ---------- 跳过开关与平台守卫 ----------


def test_escape_hatch_skips_mutex(monkeypatch, tmp_path):
    _use_endpoint(monkeypatch, tmp_path / "ep.json")
    monkeypatch.setenv(ESCAPE_ENV, "1")
    result = acquire_single_instance(grace_seconds=0)
    assert result.status == STATUS_SKIPPED
    assert result.handle is None


@pytest.mark.skipif(not IS_WINDOWS, reason="named mutex 仅 Windows")
def test_escape_hatch_bypasses_held_lock(monkeypatch, tmp_path):
    held = _fresh_lock(monkeypatch, tmp_path)
    try:
        assert held.status == STATUS_ACQUIRED
        monkeypatch.setenv(ESCAPE_ENV, "1")
        assert acquire_single_instance(grace_seconds=0).status == STATUS_SKIPPED
    finally:
        release_single_instance(held)


def test_acquire_skips_on_non_windows_platform(monkeypatch):
    # 平台守卫：非 win32 直接跳过，不碰 ctypes.WinDLL（Linux/CI 上不许崩）
    monkeypatch.setattr(sys, "platform", "linux")
    result = acquire_single_instance(grace_seconds=0)
    assert result.status == STATUS_SKIPPED
    assert result.handle is None


def test_escape_hatch_accepts_truthy_variants(monkeypatch, tmp_path):
    _use_endpoint(monkeypatch, tmp_path / "ep.json")
    for value in ("1", "true", "YES", " on "):
        monkeypatch.setenv(ESCAPE_ENV, value)
        assert acquire_single_instance(grace_seconds=0).status == STATUS_SKIPPED


# ---------- 第二实例提示输出 ----------


def test_report_second_instance_builds_one_line_with_existing_info(monkeypatch, tmp_path):
    endpoint_file = tmp_path / "ep.json"
    _use_endpoint(monkeypatch, endpoint_file)
    # 已有实例写出的 endpoint（模拟对端信息可被第二实例读到）
    write_endpoint(
        url="http://127.0.0.1:54321",
        pid=4242,
        token="tok",
        mode="desktop",
        path=endpoint_file,
    )
    busy = SingleInstanceResult(STATUS_BUSY, name=endpoint_mutex_name())

    message = report_second_instance(busy)

    # 一行、含互斥名 / endpoint 路径 / 已有实例 pid、url、mode
    assert "\n" not in message
    assert endpoint_mutex_name() in message
    assert str(endpoint_file) in message
    assert "existing_pid=4242" in message
    assert "http://127.0.0.1:54321" in message
    assert "exit 0" in message


def test_report_second_instance_survives_missing_endpoint_file(monkeypatch, tmp_path):
    # endpoint 文件不存在/损坏时提示照样产出（只省略 existing_* 字段）
    _use_endpoint(monkeypatch, tmp_path / "missing.json")
    message = report_second_instance(SingleInstanceResult(STATUS_BUSY, name=endpoint_mutex_name()))
    assert endpoint_mutex_name() in message
    assert "existing_pid=" not in message


# ---------- main.py 钩子选点 ----------


def test_main_acquires_mutex_before_gui_and_after_worker_branch():
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

    def code_index(needle: str) -> int:
        """只在非注释行里找（main.py 注释会引用这些标识符，避免误命中）。"""
        offset = 0
        for line in source.splitlines(keepends=True):
            if not line.lstrip().startswith("#") and needle in line:
                return offset + line.index(needle)
            offset += len(line)
        raise AssertionError(f"main.py 中未找到: {needle}")

    worker_branch = code_index('sys.argv[1] == "--steam-worker"')
    acquire_call = code_index("acquire_single_instance()")
    busy_exit = code_index("instance.busy")
    api_import = code_index("from backend.api import API")
    # worker 分支在前（一次性子进程不参与互斥）→ 获取互斥 → 判忙 exit 0 → 才导入后端
    assert worker_branch < acquire_call < busy_exit < api_import
    # 判忙路径必须先于 validate_environment（它可能弹原生错误框）
    assert code_index("validate_environment(") > busy_exit
