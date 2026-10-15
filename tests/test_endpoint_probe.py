# tests/test_endpoint_probe.py
"""backend/mcp_endpoint 单测：endpoint 探活判定链（probe_endpoint）。

逐条锁定 probe_endpoint docstring 自述的判定链（missing / 损坏缺字段 / schema_version /
url loopback / pid 存活 / 进程名白名单 / started_at 与 create_time 吻合的两个方向），
外加 read_endpoint、write_endpoint、remove_endpoint 的读写行为。

自洽性（不依赖 GUI / 真实 RimCrow 进程）：
- 「活进程」一律用 ``os.getpid()``（本测试进程自身）+ 本进程 create_time，
  进程名经 ``_expected_process_names()`` 天然落在白名单（sys.executable 同名）；
- 「pid 已死 / 进程名不符 / name 不可读 / pid 校验 OSError」用 ``unittest.mock``
  替换 ``psutil.Process``（probe 在函数内 import psutil，patch 模块属性即生效），不真开进程；
- 时间不真等：直接构造 ``create_time − 6s`` / ``create_time + 301s`` 打点越过
  CLOCK_SKEW(5s) / MAX_BOOT(300s) 阈值，不依赖常量是否导出、不 mock 常量。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import psutil

from backend.mcp_endpoint import (
    default_endpoint_path,
    probe_endpoint,
    read_endpoint,
    remove_endpoint,
    write_endpoint,
)

LOOPBACK_URL = "http://127.0.0.1:54321"


class TestEndpointProbe(unittest.TestCase):
    """probe_endpoint 判定链 + 读写行为：每个用例同时校验 status 与 reason 的关键事实。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.endpoint_path = self.tmp_path / "mcp-endpoint.json"
        # 时间基准：本进程 create_time（与探活取值同源，二者自洽）
        self.create_dt = datetime.fromtimestamp(
            psutil.Process(os.getpid()).create_time()
        ).astimezone()

    # ---------- helpers ----------

    def _write(self, *, path=None, url=LOOPBACK_URL, pid=None, started_at=None):
        """用被测模块自己的写入器造合法 endpoint 文件（默认 pid=本进程、时间=create_time）。"""
        return write_endpoint(
            url=url,
            pid=os.getpid() if pid is None else pid,
            token="test-token",
            mode="desktop",
            path=self.endpoint_path if path is None else path,
            started_at=self.create_dt if started_at is None else started_at,
        )

    def _rewrite_payload(self, *, drop=(), **changes):
        """在 write_endpoint 落盘结果上小改字段（测 schema 变体时复用写入器产物）。"""
        payload = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
        for key in drop:
            payload.pop(key, None)
        payload.update(changes)
        self.endpoint_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 判定链 1：文件缺失 ----------

    def test_missing_file_is_missing_and_reason_carries_path(self):
        """判定链 1：endpoint 文件不存在 → status=missing，reason 含「不存在」与完整路径。"""
        status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "missing")
        self.assertIsNone(info)
        self.assertIn("不存在", reason)
        self.assertIn(str(self.endpoint_path), reason)

    # ---------- 判定链 2：JSON 损坏 / 缺必需字段 ----------

    def test_corrupt_json_is_stale_and_reason_says_broken(self):
        """判定链 2a：文件存在但 JSON 损坏 → status=stale，reason 说明「损坏或字段缺失」。"""
        self.endpoint_path.write_text("{ 这不是合法 JSON", encoding="utf-8")

        status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNone(info)
        self.assertIn("损坏或字段缺失", reason)
        self.assertIn(str(self.endpoint_path), reason)

    def test_missing_required_field_is_stale_and_reason_says_broken(self):
        """判定链 2b：缺必需字段（url）→ status=stale，reason 同为「损坏或字段缺失」。"""
        self.endpoint_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "token": "t",
                    "started_at": self.create_dt.isoformat(),
                    "mode": "desktop",
                    "schema_version": 1,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNone(info)
        self.assertIn("损坏或字段缺失", reason)

    # ---------- 判定链 3：schema_version ----------

    def test_unsupported_schema_version_is_stale_and_reason_names_version(self):
        """判定链 3：schema_version=2（≠1）→ stale，reason 点出实际版本与期望版本。"""
        self._write()
        self._rewrite_payload(schema_version=2)

        status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNotNone(info)
        self.assertEqual(info.schema_version, 2)
        self.assertIn("schema_version=2", reason)
        self.assertIn("不受支持", reason)
        self.assertIn("期望 1", reason)

    def test_absent_schema_version_defaults_to_zero_and_is_stale(self):
        """附加分支：缺 schema_version 字段时读为 0 → 按「版本不支持」判 stale，而非「损坏」。"""
        self._write()
        self._rewrite_payload(drop=("schema_version",))

        status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNotNone(info)
        self.assertIn("schema_version=0", reason)
        self.assertNotIn("损坏或字段缺失", reason)

    # ---------- 判定链 4：url loopback ----------

    def test_non_loopback_url_is_stale_and_reason_flags_loopback(self):
        """判定链 4：url 非 http://127.0.0.1: 前缀（0.0.0.0 / https）→ stale，reason「非 loopback」。"""
        for bad_url in ("http://0.0.0.0:54321", "https://127.0.0.1:54321"):
            with self.subTest(url=bad_url):
                self._write(url=bad_url)

                status, info, reason = probe_endpoint(self.endpoint_path)

                self.assertEqual(status, "stale")
                self.assertIsNotNone(info)
                self.assertIn("非 loopback", reason)
                self.assertIn(bad_url, reason)

    # ---------- 判定链 5：pid 不存在 ----------

    def test_dead_pid_is_stale_and_reason_carries_pid(self):
        """判定链 5：pid 不存在 → stale，reason 含「pid … 已不存在」与文件路径。"""
        dead_pid = 987654
        self._write(pid=dead_pid)
        dead_process = mock.Mock(side_effect=psutil.NoSuchProcess(pid=dead_pid))

        with mock.patch("psutil.Process", dead_process):
            status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNotNone(info)
        dead_process.assert_called_once_with(dead_pid)
        self.assertIn(f"pid {dead_pid} 已不存在", reason)
        self.assertIn(str(self.endpoint_path), reason)

    # ---------- 判定链 6：进程名不在白名单 ----------

    def test_foreign_process_name_is_stale_and_reason_lists_whitelist(self):
        """判定链 6：pid 活着但进程名不在白名单 → stale，reason 给出占用者名与白名单。"""
        self._write()
        fake = mock.Mock()
        fake.name.return_value = "explorer.exe"
        fake.create_time.return_value = self.create_dt.timestamp()

        with mock.patch("psutil.Process", return_value=fake):
            status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNotNone(info)
        self.assertIn("已被其它进程占用", reason)
        self.assertIn("explorer.exe", reason)
        self.assertIn("rimcrow.exe", reason.casefold())
        self.assertIn(str(os.getpid()), reason)

    def test_whitelist_accepts_rimcrow_exe_name_case_insensitively(self):
        """附加：白名单含 rimcrow.exe（大小写不敏感）→ 过进程名校验，时间吻合即 ready。"""
        self._write()
        fake = mock.Mock()
        fake.name.return_value = "RimCrow.exe"
        fake.create_time.return_value = self.create_dt.timestamp()

        with mock.patch("psutil.Process", return_value=fake):
            status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "ready", reason)
        self.assertEqual(reason, "ok")

    def test_unreadable_process_name_skips_name_check_and_still_ready(self):
        """附加：name() 抛 AccessDenied → 按空名跳过进程名校验（pid+时间两道防线仍走）→ ready。"""
        self._write()
        fake = mock.Mock()
        fake.name.side_effect = psutil.AccessDenied(pid=os.getpid())
        fake.create_time.return_value = self.create_dt.timestamp()

        with mock.patch("psutil.Process", return_value=fake):
            status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "ready", reason)
        self.assertEqual(reason, "ok")

    # ---------- 判定链 7 / 8：started_at 与 create_time 吻合 ----------

    def test_started_before_create_time_beyond_skew_is_stale_pid_reuse(self):
        """判定链 7：started_at 早于 create_time 超 5s（构造 −6s）→ stale，reason 点明「pid 复用」。"""
        self._write(started_at=self.create_dt - timedelta(seconds=6))

        status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNotNone(info)
        self.assertIn("pid 复用", reason)
        self.assertIn(str(os.getpid()), reason)

    def test_started_beyond_max_boot_after_create_time_is_stale_mismatch(self):
        """判定链 8：started_at 晚于 create_time 超 300s（构造 +301s）→ stale，reason「不吻合」。"""
        self._write(started_at=self.create_dt + timedelta(seconds=301))

        status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNotNone(info)
        self.assertIn("不吻合", reason)
        self.assertIn(str(os.getpid()), reason)

    # ---------- 判定链 9：全部正常 ----------

    def test_self_consistent_endpoint_is_ready(self):
        """判定链 9：pid=本进程、loopback url、schema=1、started_at 落在
        [create−5s, create+300s] 内（含边界内偏移 0/−4s/+299s）→ ready。"""
        for offset in (0, -4, 299):
            with self.subTest(offset=offset):
                self._write(started_at=self.create_dt + timedelta(seconds=offset))

                status, info, reason = probe_endpoint(self.endpoint_path)

                self.assertEqual(status, "ready", reason)
                self.assertEqual(reason, "ok")
                self.assertIsNotNone(info)
                self.assertEqual(info.pid, os.getpid())
                self.assertEqual(info.url, LOOPBACK_URL)
                self.assertEqual(info.schema_version, 1)

    # ---------- 判定链 10：started_at 不可解析 ----------

    def test_unparsable_started_at_skips_time_check_and_is_ready(self):
        """判定链 10（实读代码定预期）：started_at 不可解析（垃圾串 / 空串）时
        _parse_epoch 返回 None → 跳过时间比对（与 create_time 拿不到时同为
        「退回不校验」策略）→ 仍 ready，reason=ok，防线只剩 pid 存活 + 进程名。"""
        for garbage in ("not-a-timestamp", ""):
            with self.subTest(started_at=garbage):
                self._write(started_at=garbage)

                status, info, reason = probe_endpoint(self.endpoint_path)

                self.assertEqual(status, "ready")
                self.assertEqual(reason, "ok")

    # ---------- 判定链 11：RIMCROW_MCP_ENDPOINT env 覆盖 ----------

    def test_env_var_overrides_default_endpoint_path(self):
        """判定链 11a：RIMCROW_MCP_ENDPOINT 设置时，default_endpoint_path() 完全服从该路径。"""
        with mock.patch.dict(os.environ, {"RIMCROW_MCP_ENDPOINT": str(self.endpoint_path)}):
            self.assertEqual(default_endpoint_path(), self.endpoint_path)

    def test_blank_env_override_is_ignored(self):
        """判定链 11b：env 为空白串视为未设置 → 回落 %LOCALAPPDATA%\\RimCrow\\mcp-endpoint.json。"""
        with mock.patch.dict(os.environ, {"RIMCROW_MCP_ENDPOINT": "   "}):
            path = default_endpoint_path()

        self.assertEqual(path.name, "mcp-endpoint.json")
        self.assertEqual(path.parent.name, "RimCrow")

    def test_default_path_falls_back_to_localappdata_without_env(self):
        """判定链 11c：无 env → %LOCALAPPDATA%\\RimCrow\\mcp-endpoint.json（缺失时退 Home）。"""
        with mock.patch.dict(os.environ):
            os.environ.pop("RIMCROW_MCP_ENDPOINT", None)
            expected_base = os.environ.get("LOCALAPPDATA") or str(Path.home())

            self.assertEqual(
                default_endpoint_path(),
                Path(expected_base) / "RimCrow" / "mcp-endpoint.json",
            )

    def test_probe_without_path_follows_env_override(self):
        """端到端锁定 11：probe_endpoint() 不传 path → 探的就是 env 指向的文件。"""
        with mock.patch.dict(os.environ, {"RIMCROW_MCP_ENDPOINT": str(self.endpoint_path)}):
            self._write()
            status, info, reason = probe_endpoint()

        self.assertEqual(status, "ready", reason)
        self.assertIsNotNone(info)
        self.assertEqual(info.pid, os.getpid())

    # ---------- write_endpoint / read_endpoint / remove_endpoint 读写行为 ----------

    def test_write_endpoint_roundtrip_leaves_no_temp_files(self):
        """write_endpoint：tmp + os.replace 落盘后目录只剩目标文件（无 .tmp 残留），
        read_endpoint 逐字段还原（含 path 携带）。"""
        written = self._write()

        leftovers = sorted(p.name for p in self.tmp_path.iterdir() if p != self.endpoint_path)
        self.assertEqual(leftovers, [])

        back = read_endpoint(self.endpoint_path)
        self.assertIsNotNone(back)
        self.assertEqual(back.url, written.url)
        self.assertEqual(back.pid, os.getpid())
        self.assertEqual(back.token, "test-token")
        self.assertEqual(back.mode, "desktop")
        self.assertEqual(back.started_at, written.started_at)
        self.assertEqual(back.schema_version, 1)
        self.assertEqual(back.path, self.endpoint_path)

    def test_read_endpoint_none_for_absent_broken_incomplete_or_non_dict(self):
        """read_endpoint：文件缺失 / JSON 损坏 / 缺必需字段 / 顶层非对象 → 一律 None。"""
        self.assertIsNone(read_endpoint(self.endpoint_path))

        for text in ("{ not json", json.dumps({"pid": 1, "token": "t"}), "[]"):
            with self.subTest(text=text):
                self.endpoint_path.write_text(text, encoding="utf-8")
                self.assertIsNone(read_endpoint(self.endpoint_path))

    def test_remove_endpoint_tolerates_missing_and_deletes_existing(self):
        """remove_endpoint：文件不存在不报错；存在则删除（退出清理语义）。"""
        remove_endpoint(self.endpoint_path)

        self._write()
        remove_endpoint(self.endpoint_path)

        self.assertFalse(self.endpoint_path.exists())

    # ---------- 附加分支：pid 校验抛 OSError ----------

    def test_pid_check_oserror_is_stale_with_error_detail(self):
        """附加分支：psutil.Process(pid) 抛 OSError → stale，reason 带 pid 与错误详情。"""
        self._write()
        broken = mock.Mock(side_effect=OSError("open pid failed"))

        with mock.patch("psutil.Process", broken):
            status, info, reason = probe_endpoint(self.endpoint_path)

        self.assertEqual(status, "stale")
        self.assertIsNotNone(info)
        self.assertIn("无法校验 pid", reason)
        self.assertIn(str(os.getpid()), reason)
        self.assertIn("open pid failed", reason)


if __name__ == "__main__":
    unittest.main()
