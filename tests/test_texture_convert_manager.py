# tests/test_texture_convert_manager.py
"""贴图转换的 manager 层测试。

覆盖 manager 层的四个方法：

- ``preview_convert_targets``：dry_run 预演——聚合复用 ``_build_texture_plan`` 的
  ``summary``/``rows``（键名不另起一套）、按 ``needs_action`` 补 pending 字节量、
  **零写盘**、逐模组投影到 ``_PREVIEW_MOD_FIELDS``；
- ``get_encoder_status``：编码器可用性 + 机器自适应方案；重点是 **texconv 不可用时
  ``gpu_workers`` 必须为 0**（否则 backend=todds 却报 3，响应自相矛盾）；
- ``list_active_tasks``：加锁快照 + 按 ``created_at`` 排序；
- ``normalize_mod_paths``：物理路径归一化（空白剔除 / 非目录剔除 / 重复去重）。

编码器叶子与 MCP handler 层的测试在 ``test_texture_encoders.py`` /
``test_mcp_texture_convert.py``。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from backend.managers.mgr_texture_opt import (
    _PREVIEW_MOD_FIELDS,
    TextureOptimizationManager,
    TextureTask,
)
from backend.managers.texture_encoders import (
    NvttEncoder,
    TexconvEncoder,
    TextureOptError,
    ToddsEncoder,
)
from backend.settings import TextureOptConfig

FAKE_TEXCONV = Path(r"C:\RimCrowTools\todds\texconv.exe")
FAKE_TODDS = Path(r"C:\RimCrowTools\todds\todds.exe")


class _TextureManagerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = TextureOptimizationManager()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.mod_root = self.root / "PreviewMod"
        (self.mod_root / "Textures").mkdir(parents=True)

    def _png(self, relative_path: str, size: tuple[int, int] = (256, 256)) -> Path:
        path = self.mod_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", size, (255, 0, 0, 128)).save(path)
        return path


class TestNormalizeModPaths(_TextureManagerTestBase):
    def test_blank_non_dir_and_duplicate_are_dropped(self):
        targets = self.manager.normalize_mod_paths(
            [
                "",
                "   ",
                None,
                str(self.mod_root),
                str(self.mod_root),  # 重复 → 按 mod_instance_key 去重
                str(self.root / "no_such_mod"),
            ]
        )
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["mod_path"], os.path.abspath(str(self.mod_root)))
        self.assertEqual(targets[0]["mod_name"], "PreviewMod")

    def test_empty_input_yields_empty_targets(self):
        self.assertEqual(self.manager.normalize_mod_paths([]), [])
        self.assertEqual(self.manager.normalize_mod_paths(["", None]), [])


class TestPreviewConvertTargets(_TextureManagerTestBase):
    """dry_run：canonical summary + pending 字节量 + 零写盘。"""

    def setUp(self) -> None:
        super().setUp()
        self.pending_a = self._png("Textures/a.png")
        self.pending_b = self._png("Textures/b.png")
        self.already = self._png("Textures/c.png")
        self.already.with_suffix(".dds").write_bytes(b"DDS existing")
        self.options = {
            "scale_factor": 1.0,
            "process_mode": "all_skip_existing",
            "generate_mipmaps": True,
            "skip_small_textures": False,
        }

    def test_summary_reuses_scan_vocabulary_instead_of_a_second_one(self):
        with mock.patch(
            "backend.managers.mgr_texture_opt.resolve_auto_backend", return_value="texconv"
        ):
            result = self.manager.preview_convert_targets([str(self.mod_root)], dict(self.options))
        summary = result["summary"]
        # canonical 键（与 analyze / 进度快照同源）——聚合走 _merge_stat，不手写累加
        self.assertEqual(summary["mod_count"], 1)
        self.assertEqual(summary["source_total_count"], 3)
        self.assertEqual(summary["current_output_count"], 1)
        self.assertEqual(summary["generate_required_count"], 2)  # 待转换条数
        self.assertEqual(
            summary["pending_source_bytes"],
            self.pending_a.stat().st_size + self.pending_b.stat().st_size,
        )
        self.assertTrue(summary["pending_source_bytes_human"].endswith("MiB"))
        # 旧的同义键必须消失（同一概念只保留一套词汇）
        for legacy_key in ("mods_total", "pending_count", "existing_output_count", "small_skipped_count", "unsupported_count"):
            self.assertNotIn(legacy_key, summary)

    def test_mods_are_projected_to_declared_fields_plus_pending_bytes(self):
        with mock.patch(
            "backend.managers.mgr_texture_opt.resolve_auto_backend", return_value="texconv"
        ):
            result = self.manager.preview_convert_targets([str(self.mod_root)], dict(self.options))
        mods = result["mods"]
        self.assertEqual(len(mods), 1)
        self.assertEqual(set(mods[0]), set(_PREVIEW_MOD_FIELDS) | {"pending_source_bytes"})
        self.assertEqual(mods[0]["generate_required_count"], 2)
        self.assertEqual(
            mods[0]["pending_source_bytes"],
            self.pending_a.stat().st_size + self.pending_b.stat().st_size,
        )
        # 大载荷字段（逐模组 scale_breakdown / projection_basis）不进预演响应
        self.assertNotIn("scale_breakdown", mods[0])
        self.assertNotIn("projection_basis", mods[0])

    def test_preview_writes_nothing(self):
        before = {path.resolve() for path in (self.mod_root / "Textures").glob("*.dds")}
        with mock.patch("backend.managers.mgr_texture_opt.resolve_auto_backend", return_value="todds"):
            self.manager.preview_convert_targets([str(self.mod_root)], dict(self.options))
        after = {path.resolve() for path in (self.mod_root / "Textures").glob("*.dds")}
        self.assertEqual(before, after)
        self.assertEqual(len(after), 1)  # 只有预置的 c.dds，没有新生成的

    def test_backend_block_and_options_signature_are_reported(self):
        with mock.patch(
            "backend.managers.mgr_texture_opt.resolve_auto_backend", return_value="texconv"
        ):
            result = self.manager.preview_convert_targets([str(self.mod_root)], dict(self.options))
        self.assertEqual(result["backend"]["backend"], "texconv")
        self.assertIsInstance(result["backend"]["gpu_workers"], int)
        self.assertIsInstance(result["backend"]["cpu_workers"], int)
        self.assertGreaterEqual(result["backend"]["cpu_workers"], 1)
        self.assertIn("get_texture_encoder_status", result["backend"]["note"])
        self.assertTrue(str(result["options_signature"]))

    def test_unknown_mod_path_is_causal(self):
        with self.assertRaises(TextureOptError):
            self.manager.preview_convert_targets([str(self.root / "nope")], dict(self.options))

    def test_scaling_options_no_longer_force_todds_backend(self):
        # 验证：缩放由包装层预缩放承接 → backend 不依赖 scale_factor，auto 照常解析
        options = dict(self.options, scale_factor=0.5, process_mode="scaled_only_overwrite")
        with mock.patch(
            "backend.managers.mgr_texture_opt.resolve_auto_backend", return_value="texconv"
        ) as auto:
            result = self.manager.preview_convert_targets([str(self.mod_root)], options)
        self.assertEqual(result["backend"]["backend"], "texconv")
        auto.assert_called_once()


class TestGetEncoderStatus(_TextureManagerTestBase):
    def test_texconv_missing_reports_todds_and_zero_gpu_workers(self):
        # 自洽性：texconv 不可用 → backend=todds，此时 gpu_workers 必须是 0，
        # 不能同时报「todds」与「gpu_workers=3」（todds 不消费该参数）。
        with mock.patch.object(ToddsEncoder, "resolve_executable", return_value=FAKE_TODDS), \
             mock.patch.object(
                 TexconvEncoder, "resolve_executable", side_effect=TextureOptError("未找到 texconv.exe")
             ):
            status = self.manager.get_encoder_status({})
        self.assertTrue(status["encoders"]["todds"]["available"])
        self.assertFalse(status["encoders"]["texconv"]["available"])
        self.assertEqual(set(status["encoders"]), {"todds", "texconv", "nvtt"})  # 三档都在报
        self.assertIn("未找到 texconv.exe", status["encoders"]["texconv"]["note"])
        self.assertIsNone(status["machine"]["gpu_directcompute"])  # 未探测（无 exe 可探）
        self.assertEqual(status["plan"]["backend"], "todds")
        self.assertEqual(status["plan"]["gpu_workers"], 0)
        self.assertGreaterEqual(status["plan"]["cpu_workers"], 1)
        self.assertEqual(status["plan"]["texconv_version"], "")
        self.assertIn("三档均支持缩放", status["plan"]["note"])

    def test_texconv_available_reports_probe_and_auto_backend(self):
        info = {"gpu_directcompute": True, "version": "2026.5.8.1"}
        with mock.patch.object(ToddsEncoder, "resolve_executable", return_value=FAKE_TODDS), \
             mock.patch.object(TexconvEncoder, "resolve_executable", return_value=FAKE_TEXCONV), \
             mock.patch("backend.managers.mgr_texture_opt.texconv_info", return_value=info):
            status = self.manager.get_encoder_status({})
        self.assertEqual(status["encoders"]["texconv"]["resolved_path"], str(FAKE_TEXCONV))
        self.assertTrue(status["machine"]["gpu_directcompute"])
        self.assertEqual(status["plan"]["backend"], "todds")  # auto 质量恒定，todds 在位即落 todds
        self.assertEqual(status["plan"]["gpu_workers"], 3)  # 默认乐观值
        self.assertEqual(status["plan"]["texconv_version"], "2026.5.8.1")

    def test_failed_gpu_probe_zeroes_gpu_workers_but_keeps_texconv_backend(self):
        info = {"gpu_directcompute": False, "version": "2026.5.8.1"}
        with mock.patch.object(ToddsEncoder, "resolve_executable", return_value=FAKE_TODDS), \
             mock.patch.object(TexconvEncoder, "resolve_executable", return_value=FAKE_TEXCONV), \
             mock.patch("backend.managers.mgr_texture_opt.texconv_info", return_value=info):
            status = self.manager.get_encoder_status({})
        self.assertEqual(status["plan"]["backend"], "todds")  # auto 落 todds
        self.assertEqual(status["plan"]["gpu_workers"], 0)  # 探测失败 → alpha 全走 CPU BC7

    def test_explicit_worker_override_beats_probe(self):
        info = {"gpu_directcompute": False, "version": "2026.5.8.1"}
        with mock.patch.object(ToddsEncoder, "resolve_executable", return_value=FAKE_TODDS), \
             mock.patch.object(TexconvEncoder, "resolve_executable", return_value=FAKE_TEXCONV), \
             mock.patch("backend.managers.mgr_texture_opt.texconv_info", return_value=info):
            status = self.manager.get_encoder_status({"gpu_workers": 5, "cpu_workers": 4})
        self.assertEqual(status["plan"]["gpu_workers"], 5)
        self.assertEqual(status["plan"]["cpu_workers"], 4)


class TestEncoderBackendPersistence(unittest.TestCase):
    """档位必须能跨重启保存——`TextureOptConfig` 没有该字段时，配置加载会静默丢弃它
    （`_extract_compatible_config_values` 只保留 dataclass 已知字段）。"""

    def test_encoder_backend_is_a_known_config_field(self):
        from backend.settings import AppConfig, settings

        self.assertIn("encoder_backend", TextureOptConfig.__dataclass_fields__)
        self.assertEqual(TextureOptConfig().encoder_backend, "todds")  # 默认值 = 标准，存量用户行为不变

        # 模拟「从磁盘读回」：已知字段保留、未知字段丢弃
        payload = settings._extract_compatible_config_values(
            {"texture_opt": {"encoder_backend": "nvtt", "field_from_the_future": 1}},
            AppConfig(),
        )
        self.assertEqual(payload["texture_opt"], {"encoder_backend": "nvtt"})


class TestNvttInstallHint(_TextureManagerTestBase):
    """验证：NVTT 只检测 + 官网指引，不发起任何下载。"""

    def test_missing_nvtt_returns_official_url_without_downloading(self):
        with mock.patch.object(NvttEncoder, "resolve_executable", side_effect=TextureOptError("missing")), \
             mock.patch.object(self.manager, "_prepare_todds_download") as todds_download, \
             mock.patch.object(self.manager, "_prepare_texconv_download") as texconv_download:
            result = self.manager.prepare_encoder_download(None, "nvtt", {})
        self.assertEqual(result["download_kind"], "external")
        self.assertFalse(result["already_ready"])
        self.assertIn("developer.nvidia.com", result["url"])
        todds_download.assert_not_called()
        texconv_download.assert_not_called()

    def test_installed_nvtt_is_already_ready(self):
        with mock.patch.object(NvttEncoder, "resolve_executable", return_value=Path("X:/nvtt_export.exe")):
            result = self.manager.prepare_encoder_download(None, "nvtt", {})
        self.assertTrue(result["already_ready"])

    def test_unknown_backend_is_causal(self):
        with self.assertRaises(TextureOptError) as ctx:
            self.manager.prepare_encoder_download(None, "vulkan", {})
        self.assertIn("vulkan", str(ctx.exception))


class TestListActiveTasks(_TextureManagerTestBase):
    def _task(self, task_id: str, created_at: int, status: str = "running") -> TextureTask:
        return TextureTask(
            id=task_id,
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={},
            status=status,
            created_at=created_at,
            updated_at=created_at,
        )

    def test_returns_snapshot_sorted_by_created_at(self):
        newer = self._task("t-new", created_at=200)
        older = self._task("t-old", created_at=100)
        # 故意逆序插入，证明排序来自 created_at 而不是 dict 插入序
        with self.manager._lock:
            self.manager._tasks[newer.id] = newer
            self.manager._tasks[older.id] = older

        result = self.manager.list_active_tasks()
        self.assertEqual([task["task_id"] for task in result], ["t-old", "t-new"])
        self.assertEqual([task["status"] for task in result], ["running", "running"])

    def test_empty_when_no_tasks(self):
        self.assertEqual(self.manager.list_active_tasks(), [])

    def test_snapshot_is_a_copy_not_a_live_view(self):
        with self.manager._lock:
            self.manager._tasks["t1"] = self._task("t1", created_at=1)
        snapshot = self.manager.list_active_tasks()
        snapshot.clear()
        self.assertEqual(len(self.manager.list_active_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
