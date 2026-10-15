import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

import backend.managers.mgr_texture_opt as texture_opt_module
import backend.managers.texture_encoders as texture_encoders
from backend.managers.mgr_texture_opt import TextureOptimizationManager, TextureTask, _ZstdFatalError, _ZstdFinalizer
from backend.managers.texture_encoders import (
    NvttEncoder,
    TexconvEncoder,
    TextureOptCancelled,
    TextureOptError,
    ToddsEncoder,
    _ToolProcessRunner,
)


def _fake_paths(entries):
    """entries 协议：fake 内从扫描条目取 source_path。"""
    return [str(entry.get("source_path") or "") for entry in (entries or [])]


class TestTextureOptimizationManager(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.mod_root = self.temp_root / "ExampleMod"
        (self.mod_root / "Textures").mkdir(parents=True)
        self.manager = TextureOptimizationManager()
        self.options = {
            "texture_tools_path": str(self.temp_root / "tools"),
            "process_mode": "scaled_only_overwrite",
            "output_format": "dds",
            "generate_mipmaps": True,
            "overwrite_existing": True,
            "clean_orphaned_dds": False,
            "skip_small_textures": True,
            "min_dimension": 128,
            "max_source_dimension": 2048,
            "scale_factor": 0.5,
            "max_size": 128,
            "clean_uninstalled_residue_only": False,
            "clean_without_source": False,
            "clean_output_format": "dds",
        }

    def _write_png(self, relative_path: str, size=(32, 32), alpha=False, fully_transparent=False):
        path = self.mod_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "RGBA" if alpha else "RGB"
        color = (255, 0, 0, 0 if fully_transparent else 128) if alpha else (255, 0, 0)
        Image.new(mode, size, color).save(path)
        return path

    def _write_fake_png(self, relative_path: str, size=(32, 32)):
        path = self.mod_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (255, 0, 0)).save(path, format="JPEG")
        return path

    def _write_header_only_png(self, relative_path: str, size=(32, 32), alpha=True, use_trns=False, color_type=None):
        path = self.mod_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)

        def chunk(name: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + name + data + b"\x00\x00\x00\x00"

        png_color_type = color_type if color_type is not None else (6 if alpha else 2)
        ihdr = struct.pack(">IIBBBBB", size[0], size[1], 8, png_color_type, 0, 0, 0)
        payload = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
        if use_trns:
            if png_color_type == 3:
                trns = b"\x00"
            elif png_color_type == 0:
                trns = struct.pack(">H", 0)
            else:
                trns = struct.pack(">HHH", 65535, 0, 0)
            payload += chunk(b"tRNS", trns)
        payload += chunk(b"IDAT", b"broken") + chunk(b"IEND", b"")
        path.write_bytes(payload)
        return path

    def _build_plan(self, options=None, mod_targets=None):
        return self.manager._build_texture_plan(
            mod_targets or [str(self.mod_root)],
            self.manager._build_options(options or self.options),
            None,
        )

    def _first_plan_result(self, options=None, mod_targets=None):
        return self._build_plan(options, mod_targets)["results"][0]

    def test_task_payload_exposes_task_id_alias(self):
        with patch("backend.managers.mgr_texture_opt.threading.Thread.start", return_value=None):
            task = self.manager.start_task([str(self.mod_root)], options=self.options)
        self.assertEqual(task["id"], task["task_id"])

    def test_inspect_png_treats_fully_transparent_rgba_as_alpha(self):
        source = self._write_png("Textures/transparent.png", size=(128, 128), alpha=True, fully_transparent=True)
        info = self.manager._inspect_source_image(source)
        self.assertTrue(info["has_alpha"])

    def test_clean_generated_deletes_dds_with_png_source(self):
        source = self._write_png("Textures/source.png")
        generated_dds = source.with_suffix(".dds")
        generated_dds.write_bytes(b"dds")
        standalone_dds = self.mod_root / "Textures" / "standalone.dds"
        standalone_dds.write_bytes(b"dds")

        task = TextureTask(
            id="clean-with-source",
            action="clean_generated",
            mod_paths=[str(self.mod_root)],
            options=self.options,
            status="running",
        )

        result = self.manager._clean_generated(task)

        self.assertEqual(result["orphan_deleted"], 1)
        self.assertFalse(generated_dds.exists())
        self.assertTrue(standalone_dds.exists())
        self.assertFalse(result["refresh_after_analyze"])

    def test_clean_generated_reports_failed_items(self):
        source = self._write_png("Textures/locked.png")
        generated_dds = source.with_suffix(".dds")
        generated_dds.write_bytes(b"dds")
        task = TextureTask(
            id="clean-failed-item",
            action="clean_generated",
            mod_paths=[str(self.mod_root)],
            mod_targets=[{
                "mod_path": str(self.mod_root),
                "mod_name": "ExampleMod",
                "package_id": "example.mod",
                "requires_png_source": True,
            }],
            options=self.options,
            status="running",
        )

        with patch.object(Path, "unlink", side_effect=PermissionError("file is locked")):
            result = self.manager._clean_generated(task)

        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["failed_items"][0]["mod_name"], "ExampleMod")
        self.assertEqual(result["failed_items"][0]["rel_path"], "Textures/locked.dds")
        self.assertEqual(result["failed_items"][0]["file_path"], str(generated_dds))
        self.assertIn("file is locked", result["failed_items"][0]["error"])

    def test_clean_generated_residue_target_deletes_dds_without_png_after_normalize(self):
        residue_root = self.temp_root / "2978572782"
        dds_path = residue_root / "Textures" / "Things" / "Building" / "SubMap" / "LadderDown_m.dds"
        dds_path.parent.mkdir(parents=True, exist_ok=True)
        dds_path.write_bytes(b"dds")
        normalized_targets = self.manager._normalize_mod_targets([{
            "mod_path": str(residue_root),
            "mod_name": "2978572782",
            "residue": True,
            "requires_png_source": False,
        }])
        task = TextureTask(
            id="clean-residue-without-png",
            action="clean_generated",
            mod_paths=[str(residue_root)],
            mod_targets=normalized_targets,
            options={**self.options, "clean_uninstalled_residue_only": True},
            status="running",
        )

        result = self.manager._clean_generated(task)

        self.assertEqual(result["orphan_deleted"], 1)
        self.assertFalse(dds_path.exists())

    def test_build_options_normalizes_clean_uninstalled_residue_only_to_bool(self):
        options_true = self.manager._build_options({**self.options, "clean_uninstalled_residue_only": "true"})
        options_false = self.manager._build_options({**self.options, "clean_uninstalled_residue_only": "false"})

        self.assertIs(options_true["clean_uninstalled_residue_only"], True)
        self.assertIs(options_false["clean_uninstalled_residue_only"], False)

    def test_build_options_derives_process_mode_flags(self):
        skip_existing = self.manager._build_options({**self.options, "process_mode": "all_skip_existing"})
        all_overwrite = self.manager._build_options({**self.options, "process_mode": "all_overwrite"})

        self.assertEqual(skip_existing["process_mode"], "all_skip_existing")
        self.assertEqual(all_overwrite["process_mode"], "all_overwrite")

    def test_build_encode_batches_splits_large_groups(self):
        total = texture_opt_module.TEXTURE_ENCODE_BATCH_SIZE + 1
        entries = [
            {
                "needs_action": True,
                "output_exists": False,
                "scale_percent": 50,
                "source_path": f"source-{index}.png",
            }
            for index in range(total)
        ]

        batches = self.manager._build_encode_batches(entries)

        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]["entries"]), texture_opt_module.TEXTURE_ENCODE_BATCH_SIZE)
        self.assertEqual(len(batches[1]["entries"]), 1)

    def test_iter_texture_output_paths_only_returns_dds_outputs(self):
        self._write_png("Textures/source.png")
        (self.mod_root / "Textures" / "a.dds").write_bytes(b"a")
        (self.mod_root / "Textures" / "a.dds.zstd").write_bytes(b"z")

        found = sorted(path.name for path in self.manager._iter_texture_output_paths(str(self.mod_root)))
        self.assertEqual(found, ["a.dds"])
        found_with_zstd = sorted(
            path.name
            for path in self.manager._iter_texture_output_paths(str(self.mod_root), include_zstd=True)
        )
        self.assertEqual(found_with_zstd, ["a.dds", "a.dds.zstd"])

    def test_iter_texture_roots_ignores_source_directory(self):
        source_texture = self.mod_root / "Source" / "Textures"
        source_texture.mkdir(parents=True, exist_ok=True)
        source_texture.joinpath("author.psd").write_bytes(b"psd")

        found = list(self.manager._iter_texture_root_dirs(str(self.mod_root)))

        self.assertNotIn(source_texture, found)

    def test_scan_zstd_mode_tracks_zstd_outputs_separately(self):
        source = self._write_png("Textures/source.png", size=(128, 128))
        source.with_suffix(".dds.zstd").write_bytes(b"zstd")
        source.with_suffix(".dds").write_bytes(b"dds")

        plan = self._build_plan({**self.options, "output_format": "zstd"})
        row = plan["rows"][0]

        self.assertEqual(row["output_total_count"], 2)
        self.assertEqual(row["dds_output_count"], 1)
        self.assertEqual(row["zstd_output_count"], 1)
        self.assertEqual(row["dds_output_bytes_share_pct"], 100.0)
        self.assertEqual(row["zstd_output_bytes_share_pct"], 100.0)
        self.assertEqual(row["current_output_count"], 1)
        self.assertEqual(row["external_orphan_output_count"], 0)

    def test_clean_generated_deletes_only_selected_output_format(self):
        source = self._write_png("Textures/source.png")
        dds_path = source.with_suffix(".dds")
        zstd_path = source.with_suffix(".dds.zstd")
        dds_path.write_bytes(b"dds")
        zstd_path.write_bytes(b"zstd")
        task = TextureTask(
            id="clean-selected-output-format",
            action="clean_generated",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "clean_output_format": "dds"},
            status="running",
        )

        result = self.manager._clean_generated(task)

        self.assertEqual(result["orphan_deleted"], 1)
        self.assertFalse(dds_path.exists())
        self.assertTrue(zstd_path.exists())

        dds_path.write_bytes(b"dds")
        task.options = {**self.options, "clean_output_format": "zstd"}
        result = self.manager._clean_generated(task)

        self.assertEqual(result["orphan_deleted"], 1)
        self.assertTrue(dds_path.exists())
        self.assertFalse(zstd_path.exists())

    def test_clean_without_source_deletes_only_outputs_without_png(self):
        source = self._write_png("Textures/source.png")
        source.with_suffix(".dds").write_bytes(b"dds")
        jpg_source = self.mod_root / "Textures" / "photo.jpg"
        psd_source = self.mod_root / "Textures" / "layered.psd"
        jpg_source.write_bytes(b"jpg")
        psd_source.write_bytes(b"psd")
        jpg_source.with_suffix(".dds").write_bytes(b"jpg-dds")
        psd_source.with_suffix(".dds.zstd").write_bytes(b"psd-zstd")
        orphan_dds = self.mod_root / "Textures" / "orphan.dds"
        orphan_zstd = self.mod_root / "Textures" / "orphan.dds.zstd"
        orphan_dds.write_bytes(b"orphan-dds")
        orphan_zstd.write_bytes(b"orphan-zstd")
        task = TextureTask(
            id="clean-without-source",
            action="clean_generated",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "clean_without_source": True, "clean_output_format": "dds"},
            status="running",
        )

        result = self.manager._clean_generated(task)

        self.assertEqual(result["orphan_deleted"], 1)
        self.assertTrue(source.with_suffix(".dds").exists())
        self.assertTrue(jpg_source.with_suffix(".dds").exists())
        self.assertTrue(psd_source.with_suffix(".dds.zstd").exists())
        self.assertFalse(orphan_dds.exists())
        self.assertTrue(orphan_zstd.exists())
        self.assertTrue(result["refresh_after_analyze"])

        task.options = {**self.options, "clean_without_source": True, "clean_output_format": "zstd"}
        result = self.manager._clean_generated(task)

        self.assertEqual(result["orphan_deleted"], 1)
        self.assertTrue(psd_source.with_suffix(".dds.zstd").exists())
        self.assertFalse(orphan_zstd.exists())
        self.assertTrue(result["refresh_after_analyze"])

    def test_scan_snapshot_tracks_external_orphan_dds_separately(self):
        self._write_png("Textures/source.png")
        orphan_dir = self.mod_root / "Textures" / "Loose"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        (orphan_dir / "orphan.dds").write_bytes(b"dds")

        plan = self._build_plan()
        summary = plan["summary"]
        row = plan["rows"][0]

        self.assertEqual(summary["external_orphan_output_count"], 1)
        self.assertEqual(row["external_orphan_output_count"], 1)
        self.assertEqual(summary["output_total_count"], 1)

    def test_scan_summary_tracks_current_outputs_and_action_required_separately(self):
        source = self._write_png("Textures/source.png", size=(128, 128))
        output = source.with_suffix(".dds")
        output.write_bytes(b"dds")
        source_stat = source.stat()
        os.utime(output, ns=(source_stat.st_mtime_ns + 1_000_000, source_stat.st_mtime_ns + 1_000_000))

        current_row = self._build_plan()["rows"][0]
        regenerate_row = self._build_plan({**self.options, "process_mode": "all_overwrite"})["rows"][0]

        self.assertEqual(current_row["current_output_count"], 1)
        self.assertEqual(current_row["action_required_count"], 0)
        self.assertEqual(regenerate_row["current_output_count"], 1)
        self.assertEqual(regenerate_row["action_required_count"], 1)

    def test_scan_marks_fake_png_payload_as_engine_unsupported(self):
        source = self._write_fake_png("Textures/fake.png", size=(128, 128))

        projected = self._first_plan_result()
        entry = next(item for item in projected["entries"] if Path(item["source_path"]) == source)

        self.assertTrue(entry["engine_unsupported"])
        self.assertEqual(entry["engine_unsupported_reason"], "文件扩展名为 PNG，但实际内容不是 PNG")
        self.assertFalse(entry["needs_action"])
        self.assertEqual(entry["action_status"], "unsupported")

    def test_scan_uses_png_header_fallback_for_pillow_unreadable_png(self):
        source = self._write_header_only_png("Textures/fallback.png", size=(20, 12), alpha=True)

        projected = self._first_plan_result()
        entry = next(item for item in projected["entries"] if Path(item["source_path"]) == source)

        self.assertTrue(entry["source_readable"])
        self.assertEqual(entry["width"], 20)
        self.assertEqual(entry["height"], 12)
        self.assertTrue(entry["has_alpha"])
        self.assertFalse(entry["engine_unsupported"])

    def test_scan_png_header_fallback_treats_trns_truecolor_as_alpha(self):
        source = self._write_header_only_png(
            "Textures/trns_truecolor.png",
            size=(18, 10),
            alpha=False,
            use_trns=True,
            color_type=2,
        )

        projected = self._first_plan_result()
        entry = next(item for item in projected["entries"] if Path(item["source_path"]) == source)

        self.assertTrue(entry["source_readable"])
        self.assertTrue(entry["has_alpha"])

    def test_inspect_source_image_prefers_png_header_fast_path_when_precise_alpha_disabled(self):
        source = self._write_png("Textures/fast_header.png", size=(24, 12), alpha=True)

        with patch("backend.managers.mgr_texture_opt.Image.open", side_effect=AssertionError("should not open via Pillow")):
            info = self.manager._inspect_source_image(source, precise_alpha=False)

        self.assertEqual(info["width"], 24)
        self.assertEqual(info["height"], 12)
        self.assertTrue(info["has_alpha"])

    def test_unreadable_png_header_fallback_still_associates_existing_dds(self):
        source = self._write_header_only_png("Textures/fallback.png", size=(20, 12), alpha=False)
        source.with_suffix(".dds").write_bytes(b"dds")

        row = self._build_plan()["rows"][0]

        self.assertEqual(row["source_total_count"], 1)
        self.assertEqual(row["output_total_count"], 1)
        self.assertEqual(row["external_orphan_output_count"], 0)

    def test_scan_stat_reports_keep_original_reasons_and_fake_png_sources(self):
        keep = self._write_png("Textures/keep.png", size=(128, 128))
        self._write_png("Textures/small.png", size=(8, 8))
        self._write_png("Textures/mask_m.png", size=(128, 128))
        self._write_fake_png("Textures/fake.png", size=(128, 128))
        keep_dds = keep.with_suffix(".dds")
        keep_dds.write_bytes(b"old")
        source_stat = keep.stat()
        os.utime(keep_dds, ns=(source_stat.st_mtime_ns + 1_000_000, source_stat.st_mtime_ns + 1_000_000))

        snapshot = self._first_plan_result({**self.options, "min_dimension": 16, "scale_factor": 1.0, "max_size": 128})
        stat = snapshot["stat"]

        self.assertEqual(stat["source_total_count"], 4)
        self.assertEqual(stat["current_output_count"], 1)
        self.assertEqual(stat["action_required_count"], 1)
        self.assertEqual(stat["skip_small_count"], 1)
        self.assertEqual(stat["skipped_mask_count"], 0)
        self.assertEqual(stat["keep_original_count"], 3)
        self.assertEqual(stat["keep_original_normal_count"], 1)
        self.assertEqual(stat["keep_original_mask_count"], 1)
        self.assertEqual(stat["keep_original_range_count"], 1)
        self.assertEqual(stat["unsupported_source_count"], 1)

    def test_optimize_uses_todds_fast_path_and_updates_output_stats(self):
        source = self._write_png("Textures/fast.png", size=(128, 128))
        task = TextureTask(
            id="todds-fast",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 1.0},
            status="running",
        )

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            self.assertEqual(_fake_paths(entries), [str(source)])
            self.assertIsNone(scale_percent)
            source.with_suffix(".dds").write_bytes(b"dds")

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(result["refresh_after_analyze"])
        self.assertEqual(result["final_summary"]["current_output_count"], 1)
        self.assertEqual(result["final_mods"][0]["current_output_count"], 1)
        self.assertTrue(source.with_suffix(".dds").exists())

    def test_optimize_zstd_mode_compresses_temp_dds_and_restores_old_dds(self):
        source = self._write_png("Textures/zstd.png", size=(128, 128))
        old_dds = source.with_suffix(".dds")
        old_dds.write_bytes(b"old-dds")
        task = TextureTask(
            id="zstd-generate",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 1.0, "output_format": "zstd"},
            status="running",
        )
        calls = []

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            calls.append((overwrite_existing, _fake_paths(entries)))
            source.with_suffix(".dds").write_bytes(b"new-dds")

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            result = self.manager._optimize(task)

        from zstandard.backend_c import ZstdDecompressor
        zstd_path = source.with_suffix(".dds.zstd")
        with zstd_path.open("rb") as compressed:
            with ZstdDecompressor().stream_reader(compressed) as reader:
                self.assertEqual(reader.read(), b"new-dds")
        self.assertEqual(old_dds.read_bytes(), b"old-dds")
        self.assertEqual(calls, [(True, [str(source)])])
        self.assertEqual(result["optimized"], 1)
        self.assertEqual(result["output_format"], "zstd")
        self.assertEqual(result["final_summary"]["zstd_output_count"], 1)
        self.assertEqual(result["final_summary"]["dds_output_count"], 1)
        self.assertEqual(result["final_summary"]["current_output_count"], 1)

    def test_optimize_zstd_mode_retries_retryable_compress_io_error(self):
        source = self._write_png("Textures/zstd-retry.png", size=(128, 128))
        task = TextureTask(
            id="zstd-retry",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 1.0, "output_format": "zstd"},
            status="running",
        )
        compress_calls = []

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            for source_path in _fake_paths(entries):
                Path(source_path).with_suffix(".dds").write_bytes(b"new-dds")

        def fake_compress(dds_path, zstd_path):
            compress_calls.append(dds_path)
            if len(compress_calls) == 1:
                raise PermissionError(13, "locked")
            zstd_path.write_bytes(dds_path.read_bytes())
            return zstd_path.stat().st_size

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch), \
             patch.object(TextureOptimizationManager, "_compress_dds_to_zstd", side_effect=fake_compress), \
             patch("backend.managers.mgr_texture_opt.time.sleep"):
            result = self.manager._optimize(task)

        self.assertEqual(len(compress_calls), 2)
        self.assertEqual(result["optimized"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(source.with_suffix(".dds.zstd").exists())

    def test_optimize_zstd_mode_can_delete_old_dds_after_success(self):
        source = self._write_png("Textures/zstd-clean.png", size=(128, 128))
        old_dds = source.with_suffix(".dds")
        old_dds.write_bytes(b"old-dds")
        task = TextureTask(
            id="zstd-clean-old",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={
                **self.options,
                "process_mode": "all_overwrite",
                "scale_factor": 1.0,
                "output_format": "zstd",
                "zstd_clean_old_dds": True,
            },
            status="running",
        )

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            source.with_suffix(".dds").write_bytes(b"new-dds")

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            self.manager._optimize(task)

        self.assertFalse(old_dds.exists())
        self.assertTrue(source.with_suffix(".dds.zstd").exists())

    def test_optimize_zstd_mode_records_missing_temp_dds_as_item_failure(self):
        good = self._write_png("Textures/zstd-good.png", size=(128, 128))
        bad = self._write_png("Textures/zstd-bad.png", size=(128, 128))
        task = TextureTask(
            id="zstd-missing-temp",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 1.0, "output_format": "zstd"},
            status="running",
        )

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            self.assertTrue(overwrite_existing)
            for source_path in _fake_paths(entries):
                if Path(source_path) == good:
                    Path(source_path).with_suffix(".dds").write_bytes(b"dds")

        def fake_compress(dds_path, zstd_path):
            if not dds_path.exists():
                raise texture_opt_module.TextureOptError(f"todds 未生成临时 DDS，无法压缩为 ZSTD: {dds_path}")
            zstd_path.write_bytes(b"zstd")
            return zstd_path.stat().st_size

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch), \
             patch.object(TextureOptimizationManager, "_compress_dds_to_zstd", side_effect=fake_compress):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["final_status"], "failed")
        self.assertTrue(good.with_suffix(".dds.zstd").exists())
        self.assertFalse(bad.with_suffix(".dds.zstd").exists())
        self.assertEqual(result["failed_items"][0]["rel_path"], "Textures/zstd-bad.png")

    def test_optimize_zstd_clean_old_dds_restores_failed_original_dds(self):
        good = self._write_png("Textures/zstd-clean-good.png", size=(128, 128))
        bad = self._write_png("Textures/zstd-clean-bad.png", size=(128, 128))
        good_dds = good.with_suffix(".dds")
        bad_dds = bad.with_suffix(".dds")
        good_dds.write_bytes(b"old-good")
        bad_dds.write_bytes(b"old-bad")
        task = TextureTask(
            id="zstd-clean-partial-failure",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={
                **self.options,
                "process_mode": "all_overwrite",
                "scale_factor": 1.0,
                "output_format": "zstd",
                "zstd_clean_old_dds": True,
            },
            status="running",
        )

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            for source_path in _fake_paths(entries):
                if Path(source_path) == good:
                    Path(source_path).with_suffix(".dds").write_bytes(b"new-good")

        def fake_compress(dds_path, zstd_path):
            if not dds_path.exists():
                raise texture_opt_module.TextureOptError(f"todds 未生成临时 DDS，无法压缩为 ZSTD: {dds_path}")
            zstd_path.write_bytes(b"zstd")
            return zstd_path.stat().st_size

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch), \
             patch.object(TextureOptimizationManager, "_compress_dds_to_zstd", side_effect=fake_compress):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(good_dds.exists())
        self.assertEqual(bad_dds.read_bytes(), b"old-bad")
        self.assertTrue(good.with_suffix(".dds.zstd").exists())
        self.assertFalse(bad.with_suffix(".dds.zstd").exists())

    def test_optimize_zstd_mode_records_backup_failure_as_item_failure(self):
        good = self._write_png("Textures/zstd-backup-good.png", size=(128, 128))
        bad = self._write_png("Textures/zstd-backup-bad.png", size=(128, 128))
        good.with_suffix(".dds").write_bytes(b"old-good")
        bad_dds = bad.with_suffix(".dds")
        bad_dds.write_bytes(b"old-bad")
        task = TextureTask(
            id="zstd-backup-failure",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 1.0, "output_format": "zstd"},
            status="running",
        )
        original_replace = Path.replace

        def guarded_replace(path_self, target):
            if path_self == bad_dds:
                raise OSError("locked")
            return original_replace(path_self, target)

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            for source_path in _fake_paths(entries):
                Path(source_path).with_suffix(".dds").write_bytes(b"new-dds")

        def fake_compress(dds_path, zstd_path):
            zstd_path.write_bytes(dds_path.read_bytes())
            return zstd_path.stat().st_size

        with patch.object(Path, "replace", guarded_replace), \
             patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch), \
             patch.object(TextureOptimizationManager, "_compress_dds_to_zstd", side_effect=fake_compress):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(good.with_suffix(".dds.zstd").exists())
        self.assertFalse(bad.with_suffix(".dds.zstd").exists())
        self.assertEqual(result["failed_items"][0]["rel_path"], "Textures/zstd-backup-bad.png")

    def test_optimize_only_scans_each_mod_once(self):
        source = self._write_png("Textures/once.png", size=(128, 128))
        task = TextureTask(
            id="scan-once",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 1.0},
            status="running",
        )
        original_build = self.manager._build_mod_base_index

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            self.assertEqual(_fake_paths(entries), [str(source)])
            source.with_suffix(".dds").write_bytes(b"dds")

        with patch.object(self.manager, "_build_mod_base_index", wraps=original_build) as build_mock, \
             patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            result = self.manager._optimize(task)

        self.assertEqual(build_mock.call_count, 1)
        self.assertEqual(result["optimized"], 1)
        self.assertEqual(result["failed"], 0)

    def test_optimize_prepare_uses_threaded_plan_builder(self):
        other_mod_root = self.temp_root / "OtherMod"
        (other_mod_root / "Textures").mkdir(parents=True)
        task = TextureTask(
            id="threaded-plan",
            action="optimize",
            mod_paths=[str(self.mod_root), str(other_mod_root)],
            options={**self.options, "process_mode": "all_overwrite"},
            status="running",
        )
        seen_threads: list[str] = []
        lock = threading.Lock()

        def fake_load_or_build_base_index(target, **_kwargs):
            mod_path = str(target.get("mod_path") or "")
            with lock:
                seen_threads.append(threading.current_thread().name)
            return {
                "mod_path": mod_path,
                "mod_name": Path(mod_path).name,
                "entries": [],
            }

        def fake_project_mod_index(base_index, _options, **_kwargs):
            mod_path = str(base_index.get("mod_path") or "")
            return {
                "mod_path": mod_path,
                "mod_name": Path(mod_path).name,
                "entries": [],
                "stat": self.manager._create_empty_stat(mod_path=mod_path, mod_name=Path(mod_path).name),
                "output_stats": {},
            }

        with patch.object(self.manager, "_resolve_scan_workers", return_value=2), \
             patch.object(self.manager, "_load_or_build_base_index", side_effect=fake_load_or_build_base_index), \
             patch.object(self.manager, "_project_mod_index", side_effect=fake_project_mod_index):
            results = self.manager._scan_targets_for_optimize(task, task.options)

        self.assertEqual(len(results), 2)
        self.assertEqual(len(seen_threads), 2)
        self.assertTrue(all(name.startswith("TexturePlan") for name in seen_threads))

    def test_build_texture_plan_keeps_other_mods_when_one_scan_fails(self):
        other_mod_root = self.temp_root / "OtherMod"
        (other_mod_root / "Textures").mkdir(parents=True)
        self._write_png("Textures/ok.png", size=(128, 128))
        task_options = {**self.options, "process_mode": "all_overwrite"}
        targets = self.manager._normalize_mod_targets([
            {"mod_path": str(self.mod_root), "mod_name": "ExampleMod"},
            {"mod_path": str(other_mod_root), "mod_name": "OtherMod"},
        ])

        def fake_load_or_build_base_index(target, *, cancel_event=None, validate_cache=True):
            if str(target.get("mod_path")) == str(other_mod_root):
                raise OSError("scan failed")
            return self.manager._build_mod_base_index(target, cancel_event=cancel_event)

        with patch.object(self.manager, "_load_or_build_base_index", side_effect=fake_load_or_build_base_index):
            plan = self.manager._build_texture_plan(targets, task_options, None)

        rows_by_name = {row["mod_name"]: row for row in plan["rows"]}
        results_by_name = {result["mod_name"]: result for result in plan["results"]}
        self.assertEqual(len(plan["results"]), 2)
        self.assertEqual(len(results_by_name["ExampleMod"]["entries"]), 1)
        self.assertEqual(results_by_name["OtherMod"]["entries"], [])
        self.assertEqual(rows_by_name["OtherMod"]["scan_status"], "failed")
        self.assertEqual(plan["summary"]["scan_failed_count"], 1)

    def test_optimize_reuses_recent_analysis_cache_without_revalidating_source(self):
        source = self._write_png("Textures/reuse.png", size=(128, 128))
        source.with_suffix(".dds").write_bytes(b"dds")
        options = {**self.options, "process_mode": "all_skip_existing"}
        self.manager._scan_mods([str(self.mod_root)], options, None)
        task = TextureTask(
            id="reuse-analysis-cache",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options=options,
            status="running",
        )

        with patch.object(self.manager, "_build_mod_base_index", side_effect=AssertionError("should use base scan cache")):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 0)
        self.assertEqual(result["failed"], 0)

    def test_optimize_rebuilds_base_scan_after_cache_ttl_expires(self):
        source = self._write_png("Textures/expired.png", size=(128, 128))
        source.with_suffix(".dds").write_bytes(b"dds")
        options = {**self.options, "process_mode": "all_skip_existing"}
        self.manager._scan_mods([str(self.mod_root)], options, None)
        cache_key = texture_opt_module.generate_path_hash(str(self.mod_root))
        self.manager._base_scan_cache[cache_key]["generated_at"] = (
            texture_opt_module.current_ms() - texture_opt_module.TEXTURE_BASE_SCAN_CACHE_TTL_MS - 1
        )
        task = TextureTask(
            id="expired-analysis-cache",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options=options,
            status="running",
        )

        with patch.object(self.manager, "_build_mod_base_index", wraps=self.manager._build_mod_base_index) as build_mock:
            result = self.manager._optimize(task)

        self.assertEqual(build_mock.call_count, 1)
        self.assertEqual(result["optimized"], 0)
        self.assertEqual(result["failed"], 0)

    def test_optimize_no_jobs_still_returns_final_snapshot_data(self):
        source = self._write_png("Textures/current.png", size=(32, 32))
        source.with_suffix(".dds").write_bytes(b"dds")
        task = TextureTask(
            id="no-jobs",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "clean_orphaned_dds": False},
            status="running",
        )

        result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 0)
        self.assertIn("final_summary", result)
        self.assertIn("final_mods", result)
        self.assertEqual(result["final_summary"]["mod_count"], 1)
        self.assertEqual(len(result["final_mods"]), 1)

    def test_optimize_skip_existing_current_dds_is_cleaned_when_source_exists(self):
        source = self._write_png("Textures/external.png", size=(128, 128))
        current_dds = source.with_suffix(".dds")
        current_dds.write_bytes(b"current-dds")
        source_stat = source.stat()
        os.utime(current_dds, ns=(source_stat.st_mtime_ns + 1_000_000, source_stat.st_mtime_ns + 1_000_000))
        task = TextureTask(
            id="external-current",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_skip_existing"},
            status="running",
        )

        with patch.object(ToddsEncoder, "encode_batch", side_effect=AssertionError("should not re-encode external current DDS")):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(current_dds.exists())

        clean_task = TextureTask(
            id="external-current-clean",
            action="clean_generated",
            mod_paths=[str(self.mod_root)],
            options=self.options,
            status="running",
        )
        clean_result = self.manager._clean_generated(clean_task)

        self.assertEqual(clean_result["orphan_deleted"], 1)
        self.assertFalse(current_dds.exists())

    def test_optimize_force_overwrites_existing_output(self):
        source = self._write_png("Textures/overwrite.png", size=(128, 128))
        output = source.with_suffix(".dds")
        output.write_bytes(b"old")
        source_stat = source.stat()
        os.utime(output, ns=(source_stat.st_mtime_ns + 1_000_000, source_stat.st_mtime_ns + 1_000_000))
        task = TextureTask(
            id="force-overwrite",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 1.0},
            status="running",
        )
        calls = []

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            calls.append((overwrite_existing, _fake_paths(entries)))
            self.assertIsNone(scale_percent)
            output.write_bytes(b"new")

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            result = self.manager._optimize(task)

        self.assertEqual(calls, [(True, [str(source)])])
        self.assertEqual(result["optimized"], 1)
        self.assertEqual(result["failed"], 0)

    def test_all_skip_existing_process_mode_keeps_existing_outputs(self):
        source = self._write_png("Textures/existing.png", size=(256, 256))
        output = source.with_suffix(".dds")
        output.write_bytes(b"old")
        source_stat = source.stat()
        os.utime(output, ns=(source_stat.st_mtime_ns + 1_000_000, source_stat.st_mtime_ns + 1_000_000))
        task = TextureTask(
            id="skip-existing",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_skip_existing"},
            status="running",
        )

        with patch.object(ToddsEncoder, "encode_batch", side_effect=AssertionError("should skip current dds")):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)

    def test_all_skip_existing_process_mode_skips_stale_existing_output(self):
        source = self._write_png("Textures/stale.png", size=(256, 256))
        output = source.with_suffix(".dds")
        output.write_bytes(b"old")
        source_stat = source.stat()
        os.utime(output, ns=(source_stat.st_mtime_ns - 1_000_000, source_stat.st_mtime_ns - 1_000_000))
        task = TextureTask(
            id="skip-stale",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_skip_existing"},
            status="running",
        )
        with patch.object(ToddsEncoder, "encode_batch", side_effect=AssertionError("should skip existing dds")):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)

    def test_scale_strategy_falls_back_to_original_size_for_incompatible_dimensions(self):
        source = self._write_png("Textures/fallback.png", size=(136, 136))

        projected = self._first_plan_result({**self.options, "scale_factor": 0.5, "max_size": 128})
        entry = next(item for item in projected["entries"] if Path(item["source_path"]) == source)

        self.assertEqual(entry["plan_kind"], "keep_original")
        self.assertIsNone(entry["scale_percent"])

    def test_scale_strategy_uses_larger_scale_when_needed(self):
        source = self._write_png("Textures/scaled.png", size=(512, 512))

        projected = self._first_plan_result({**self.options, "scale_factor": 0.2, "max_size": 128})
        entry = next(item for item in projected["entries"] if Path(item["source_path"]) == source)

        self.assertEqual(entry["plan_kind"], "fallback")
        self.assertEqual(entry["scale_percent"], 25)

    def test_no_compression_does_not_read_min_clarity(self):
        self._write_png("Textures/source.png", size=(512, 512))

        with patch.object(TextureOptimizationManager, "_get_scale_target_size", side_effect=AssertionError("不压缩时不应读取最小清晰度")):
            snapshot = self._build_plan({**self.options, "scale_factor": 1.0, "max_size": 1024})

        self.assertEqual(snapshot["summary"]["scaled_count"], 0)
        self.assertEqual(snapshot["summary"]["keep_original_count"], 1)

    def test_no_compression_signature_ignores_min_clarity(self):
        no_compress_128 = self.manager._build_signature({**self.options, "scale_factor": 1.0, "max_size": 128})
        no_compress_1024 = self.manager._build_signature({**self.options, "scale_factor": 1.0, "max_size": 1024})
        scaled_128 = self.manager._build_signature({**self.options, "scale_factor": 0.5, "max_size": 128})
        scaled_1024 = self.manager._build_signature({**self.options, "scale_factor": 0.5, "max_size": 1024})

        self.assertEqual(no_compress_128, no_compress_1024)
        self.assertNotEqual(scaled_128, scaled_1024)

    def test_optimize_scale_strategy_splits_scaled_and_original_size_batches(self):
        scaled_source = self._write_png("Textures/scaled.png", size=(256, 256))
        fallback_source = self._write_png("Textures/fallback.png", size=(136, 136))
        task = TextureTask(
            id="step-batches",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 0.5, "max_size": 128},
            status="running",
        )
        calls = []

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            calls.append(
                {
                    "overwrite_existing": overwrite_existing,
                    "source_paths": _fake_paths(entries),
                    "scale_percent": scale_percent,
                }
            )
            for source_path in _fake_paths(entries):
                Path(source_path).with_suffix(".dds").write_bytes(b"dds")

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertIn("按当前档位缩放 1 张", result["message"])
        self.assertIn("保持原尺寸 1 张", result["message"])
        self.assertEqual(
            calls,
            [
                {
                    "overwrite_existing": True,
                    "source_paths": [str(scaled_source)],
                    "scale_percent": 50,
                },
                {
                    "overwrite_existing": True,
                    "source_paths": [str(fallback_source)],
                    "scale_percent": None,
                },
            ],
        )

    def test_optimize_streams_todds_progress_with_callback(self):
        source = self._write_png("Textures/progress.png", size=(128, 128))
        task = TextureTask(
            id="stream-progress",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 1.0},
            status="running",
        )
        emitted: list[dict[str, Any]] = []

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            self.assertTrue(callable(output_callback))
            output_callback("Progress: 1 / 1")
            for source_path in _fake_paths(entries):
                Path(source_path).with_suffix(".dds").write_bytes(b"dds")

        def capture_progress(_task_id, _task_type, **payload):
            emitted.append(payload)

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch), \
             patch("backend.managers.mgr_texture_opt.EventBus.emit_progress", side_effect=capture_progress):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 1)
        self.assertTrue(source.with_suffix(".dds").exists())
        self.assertTrue(any(item.get("metrics", {}).get("current_batch_done") == 1 for item in emitted))

    def test_optimize_generates_mask_textures_at_original_size(self):
        mask = self._write_png("Textures/mask_m.png", size=(256, 256))
        task = TextureTask(
            id="mask-original",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, "process_mode": "all_overwrite", "scale_factor": 0.2, "max_size": 128},
            status="running",
        )
        calls = []

        def fake_encode_batch(
            _cancel_event,
            *,
            entries,
            overwrite_existing,
            scale_percent,
            output_callback=None,
            on_unit_done=None,
        ):
            calls.append({"source_paths": _fake_paths(entries), "scale_percent": scale_percent})
            for source_path in _fake_paths(entries):
                Path(source_path).with_suffix(".dds").write_bytes(b"dds")

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 1)
        self.assertEqual(calls, [{"source_paths": [str(mask)], "scale_percent": None}])
        self.assertTrue(mask.with_suffix(".dds").exists())

    def test_scan_summary_reports_scale_breakdown(self):
        self._write_png("Textures/scaled.png", size=(256, 256))
        self._write_png("Textures/fallback.png", size=(136, 136))

        snapshot = self._build_plan({**self.options, "scale_factor": 0.5, "max_size": 128})
        summary = snapshot["summary"]
        row = snapshot["rows"][0]

        self.assertEqual(summary["scaled_count"], 1)
        self.assertEqual(summary["keep_original_count"], 1)
        self.assertEqual(row["scaled_count"], 1)
        self.assertEqual(row["keep_original_count"], 1)
        self.assertEqual(
            row["scale_breakdown"],
            [
                {"kind": "scaled", "label": "50%", "count": 1},
                {"kind": "keep_original", "label": "不缩放", "count": 1},
            ],
        )

    def test_scale_breakdown_sorts_scale_labels_by_percent(self):
        breakdown = TextureOptimizationManager._finalize_scale_breakdown({
            ("fallback", "50%"): 200,
            ("fallback", "25%"): 10,
            ("fallback", "40%"): 50,
            ("fallback", "80%"): 1,
            ("fallback", "75%"): 5,
            ("scaled", "20%"): 300,
            ("keep_original", "不缩放"): 20,
        })

        self.assertEqual(
            breakdown,
            [
                {"kind": "scaled", "label": "20%", "count": 300},
                {"kind": "fallback", "label": "25%", "count": 10},
                {"kind": "fallback", "label": "40%", "count": 50},
                {"kind": "fallback", "label": "50%", "count": 200},
                {"kind": "fallback", "label": "75%", "count": 5},
                {"kind": "fallback", "label": "80%", "count": 1},
                {"kind": "keep_original", "label": "不缩放", "count": 20},
            ],
        )

    def test_scaled_only_process_mode_skips_keep_original_jobs(self):
        scaled = self._write_png("Textures/scaled.png", size=(256, 256))
        fallback = self._write_png("Textures/fallback.png", size=(136, 136))
        scan_result = self._first_plan_result({**self.options, "process_mode": "scaled_only_overwrite"})
        batches = self.manager._build_encode_batches(scan_result["entries"])

        self.assertEqual(len(batches), 1)
        self.assertEqual([entry["source_path"] for entry in batches[0]["entries"]], [str(scaled)])
        self.assertNotIn(str(fallback), [entry["source_path"] for entry in batches[0]["entries"]])
        self.assertEqual(self.manager._count_skipped_entries(scan_result["entries"], {**self.options, "process_mode": "scaled_only_overwrite"}), 1)

    def test_dimension_window_skips_too_small_and_too_large_sources(self):
        self.assertTrue(self.manager._is_outside_recommended_source_range(127, 256, self.options))
        self.assertTrue(self.manager._is_outside_recommended_source_range(4096, 512, self.options))
        self.assertFalse(self.manager._is_outside_recommended_source_range(512, 512, self.options))

    def test_resolve_scan_workers_uses_auto_cap_and_manual_override(self):
        auto_workers = self.manager._resolve_scan_workers(12, self.options)
        manual_workers = self.manager._resolve_scan_workers(12, {**self.options, "scan_workers": 3})

        self.assertGreaterEqual(auto_workers, 1)
        self.assertLessEqual(auto_workers, 8)
        self.assertEqual(manual_workers, 3)

    def test_read_process_log_returns_tail_segment(self):
        log_path = self.temp_root / "tool.log"
        log_path.write_bytes(b"a" * 4096 + b"TAIL-END")

        tail = _ToolProcessRunner.read_process_log(log_path, limit=16)

        self.assertEqual(tail, "aaaaaaaaTAIL-END")


class TestTextureOptimizationPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.results_dir = self.temp_root / "results"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.exclusions_path = self.temp_root / "texture_opt_exclusions.json"
        self.results_patcher = patch.object(texture_opt_module, "TEXTURE_RESULTS_DIR", self.results_dir)
        self.exclusions_patcher = patch.object(texture_opt_module, "TEXTURE_EXCLUSIONS_PATH", self.exclusions_path)
        self.results_patcher.start()
        self.exclusions_patcher.start()
        self.addCleanup(self.results_patcher.stop)
        self.addCleanup(self.exclusions_patcher.stop)

        self.mod_root = self.temp_root / "ExampleMod"
        (self.mod_root / "Textures").mkdir(parents=True, exist_ok=True)
        self.manager = TextureOptimizationManager()
        self.options = {
            "texture_tools_path": str(self.temp_root / "tools"),
            "process_mode": "all_overwrite",
            "output_format": "dds",
            "generate_mipmaps": True,
            "overwrite_existing": True,
            "skip_small_textures": False,
            "min_dimension": 1,
            "max_source_dimension": 4096,
            "scale_factor": 1.0,
            "max_size": 128,
        }

    def _write_png(self, relative_path: str, size=(128, 128)) -> Path:
        path = self.mod_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", size, (255, 0, 0, 128)).save(path)
        return path

    def _build_plan(self, options=None, mod_targets=None):
        return self.manager._build_texture_plan(
            mod_targets or [str(self.mod_root)],
            self.manager._build_options(options or self.options),
            None,
        )

    def test_mod_exclusion_removes_entries_from_generation_plan(self):
        self._write_png("Textures/excluded.png")
        self.manager.set_mod_exclusion("Example.Mod", True)

        projected = self._build_plan(
            mod_targets=[{
                "mod_path": str(self.mod_root),
                "mod_name": "ExampleMod",
                "package_id": "example.mod",
            }],
        )["results"][0]

        self.assertEqual(projected["stat"]["package_id"], "example.mod")
        self.assertEqual(projected["stat"]["excluded_count"], 1)
        self.assertEqual(projected["stat"]["generate_required_count"], 0)
        self.assertEqual(projected["entries"][0]["action_status"], "excluded")
        self.assertTrue(projected["entries"][0]["excluded"])
        self.assertEqual(self.manager._build_encode_batches(projected["entries"]), [])

    def test_file_exclusion_removes_single_entry_from_generation_plan(self):
        self._write_png("Textures/keep.png")
        self._write_png("Textures/skip.png")
        self.manager.set_file_exclusion(str(self.mod_root), "Textures/skip.png", True)

        projected = self._build_plan()["results"][0]
        entries = {entry["rel_path"]: entry for entry in projected["entries"]}

        self.assertEqual(projected["stat"]["excluded_count"], 1)
        self.assertEqual(projected["stat"]["generate_required_count"], 1)
        self.assertEqual(entries["Textures/skip.png"]["action_status"], "excluded")
        self.assertEqual(entries["Textures/keep.png"]["action_status"], "pending")

    def test_result_history_keeps_latest_three_files(self):
        for index in range(4):
            task = TextureTask(
                id=f"task-{index}",
                action="optimize",
                mod_paths=[str(self.mod_root)],
                options=dict(self.options),
                status="running",
                created_at=1000 + index,
                updated_at=1000 + index,
            )
            self.manager._write_task_result_file(
                task,
                self.options,
                self.manager._create_empty_stat(include_mod_count=True, mod_count=1),
                [],
                [],
            )
            time.sleep(0.01)

        history = self.manager.list_result_history(10)

        self.assertEqual([item["task_id"] for item in history], ["task-3", "task-2", "task-1"])
        self.assertEqual(len(list(self.results_dir.glob("*.json"))), 3)
        self.assertTrue(all(Path(item["result_path"]).exists() for item in history))


class TestToddsEncoder(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        (self.temp_root / "Textures").mkdir(parents=True, exist_ok=True)
        self.source = self.temp_root / "Textures" / "a.png"
        Image.new("RGBA", (16, 16), (255, 0, 0, 128)).save(self.source)

    def test_encode_batch_builds_filtered_source_list_command(self):
        encoder = ToddsEncoder(
            {
                "texture_tools_path": str(self.temp_root),
                "overwrite_existing": False,
                "generate_mipmaps": True,
                "scale_factor": 0.5,
            }
        )

        def inspect_todds_command(command, _cancel_event, timeout_seconds=None, tool_name="todds"):
            self.assertEqual(command[0], str(self.temp_root / "todds.exe"))
            self.assertIn("-f", command)
            self.assertIn("BC1", command)
            self.assertIn("-af", command)
            self.assertIn("BC7", command)
            self.assertIn("-on", command)
            self.assertIn("-vf", command)
            self.assertIn("-r", command)
            self.assertIn("Textures", command)
            self.assertIn("-t", command)
            self.assertIn("-p", command)
            self.assertNotIn("-fs", command)
            self.assertIn("-sc", command)
            self.assertIn("50", command)
            self.assertNotIn("-ms", command)  # -ms（max_size）不进命令（本协议无此参数）
            self.assertNotIn("-ss", command)
            input_list = Path(command[-1])
            self.assertEqual(input_list.suffix.lower(), ".txt")
            self.assertTrue(input_list.exists())
            self.assertIn(str(self.source), input_list.read_text(encoding="utf-8"))

        with patch.object(ToddsEncoder, "resolve_executable", return_value=self.temp_root / "todds.exe"), \
             patch("backend.managers.texture_encoders._ToolProcessRunner.run_command", side_effect=inspect_todds_command):
            encoder.encode_batch(
                threading.Event(),
                entries=[{"source_path": str(self.source)}],
                overwrite_existing=False,
                scale_percent=50,
            )


class TestTextureZstdPipeline(unittest.TestCase):
    """ZSTD 后台压缩管线与档位判定。

    - 单元回调把「压缩成 .dds.zstd」与剩余单元的编码重叠；
    - 编码失败/取消时已完成单元的 .zstd 保留、任务前的 DDS 恢复；
    - 编码器漏发单元回调时串行兜底，ZSTD 产物不静默丢失；
    - NVTT 在 ZSTD 投影下仍写源旁 .dds（回归 NVTT×ZSTD 整批失败）；
    - get_backend_status / get_encoder_status / start_benchmark 按所选档位判定。
    """

    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.mod_root = self.temp_root / "ExampleMod"
        (self.mod_root / "Textures").mkdir(parents=True)
        self.manager = TextureOptimizationManager()
        self.options = {
            "texture_tools_path": str(self.temp_root / "tools"),
            "process_mode": "all_overwrite",
            "output_format": "zstd",
            "generate_mipmaps": True,
            "scale_factor": 1.0,
            "skip_small_textures": True,
            "min_dimension": 128,
            "max_source_dimension": 2048,
        }

    def _write_png(self, name: str, size=(128, 128)) -> Path:
        path = self.mod_root / "Textures" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (255, 0, 0)).save(path)
        return path

    def _zstd_task(self, **extra_options) -> TextureTask:
        return TextureTask(
            id="zstd-pipeline",
            action="optimize",
            mod_paths=[str(self.mod_root)],
            options={**self.options, **extra_options},
            status="running",
        )

    @staticmethod
    def _wait_for(path: Path, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return False

    def test_unit_finalize_overlaps_encode(self):
        source = self._write_png("unit.png")
        task = self._zstd_task()

        def fake_encode_batch(_cancel_event, *, entries, overwrite_existing, scale_percent, output_callback=None, on_unit_done=None):
            for entry in entries:
                Path(entry["source_path"]).with_suffix(".dds").write_bytes(b"new-dds")
                on_unit_done([entry])
            # 编码返回前，单元回调触发的压缩应当已经完成（重叠生效的直接证据）
            self.assertTrue(self._wait_for(source.with_suffix(".dds.zstd")))

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            self.manager._optimize(task)

        self.assertTrue(source.with_suffix(".dds.zstd").exists())
        self.assertFalse(source.with_suffix(".dds").exists())  # 临时 DDS 已清理

    def test_cancelled_encode_keeps_finalized_outputs_and_restores_old_dds(self):
        good = self._write_png("good.png")
        bad = self._write_png("bad.png")
        for source in (good, bad):
            source.with_suffix(".dds").write_bytes(b"old-dds")
        task = self._zstd_task()
        finalized: list[Path] = []

        def fake_encode_batch(_cancel_event, *, entries, overwrite_existing, scale_percent, output_callback=None, on_unit_done=None):
            first = entries[0]
            Path(first["source_path"]).with_suffix(".dds").write_bytes(b"new-dds")
            on_unit_done([first])
            self.assertTrue(self._wait_for(Path(first["source_path"]).with_suffix(".dds.zstd")))
            finalized.append(Path(first["source_path"]))
            raise TextureOptCancelled("贴图优化任务已取消")

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            with self.assertRaises(TextureOptCancelled):
                self.manager._optimize(task)

        from zstandard.backend_c import ZstdDecompressor

        first = finalized[0]
        other = bad if first == good else good
        with first.with_suffix(".dds.zstd").open("rb") as compressed:
            with ZstdDecompressor().stream_reader(compressed) as reader:
                self.assertEqual(reader.read(), b"new-dds")
        # 已完成单元保留 .zstd（取消语义：已完成部分保留），任务前的 DDS 回到源旁
        self.assertEqual(first.with_suffix(".dds").read_bytes(), b"old-dds")
        self.assertFalse(other.with_suffix(".dds.zstd").exists())
        self.assertEqual(other.with_suffix(".dds").read_bytes(), b"old-dds")

    def test_missing_unit_callback_falls_back_to_serial_finalize(self):
        # 编码器不调用单元回调（协议缺失/提前返回）时，兜底压缩保证产物不静默丢失
        source = self._write_png("serial.png")
        task = self._zstd_task()

        def fake_encode_batch(_cancel_event, *, entries, overwrite_existing, scale_percent, output_callback=None, on_unit_done=None):
            for entry in entries:
                Path(entry["source_path"]).with_suffix(".dds").write_bytes(b"new-dds")

        with patch.object(ToddsEncoder, "encode_batch", side_effect=fake_encode_batch):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 1)
        self.assertTrue(source.with_suffix(".dds.zstd").exists())

    def test_nvtt_zstd_mode_writes_dds_beside_source(self):
        # 回归：NVTT 曾把 output_path（.dds.zstd 产物投影）直接当 -o，未压缩 DDS
        # 被写进 .zstd 文件且管理器找不到临时 DDS 可压缩，整批必坏
        source = self._write_png("nvtt.png")
        source.with_suffix(".dds").write_bytes(b"old-dds")
        task = self._zstd_task(encoder_backend="nvtt")

        def fake_run(command, cancel_event, timeout_seconds=None, *, tool_name=None, output_callback=None):
            batch = command[command.index("--batch") + 1]
            for line in Path(batch).read_text(encoding="utf-8").splitlines():
                target = line.split('-o "')[1].split('"')[0]
                Path(target).write_bytes(b"new-dds")

        with patch.object(NvttEncoder, "resolve_executable", return_value=self.temp_root / "nvtt_export.exe"), \
             patch.object(texture_encoders._ToolProcessRunner, "run_command", side_effect=fake_run):
            result = self.manager._optimize(task)

        self.assertEqual(result["optimized"], 1)
        self.assertTrue(source.with_suffix(".dds.zstd").exists())
        self.assertEqual(source.with_suffix(".dds").read_bytes(), b"old-dds")  # 旧 DDS 恢复

    def test_zstd_finalizer_collect_and_abort(self):
        started = threading.Event()
        release = threading.Event()

        def blocking_chunk(entries):
            started.set()
            release.wait(5)
            return list(entries), []

        finalizer = _ZstdFinalizer(blocking_chunk)
        finalizer.submit(["in-flight"])
        self.assertTrue(started.wait(5))  # 在途压缩占用消费线程
        finalizer.submit(["pending"])  # 排队未消费

        abort_done = threading.Event()

        def do_abort():
            finalizer.abort()
            abort_done.set()

        aborter = threading.Thread(target=do_abort, daemon=True)
        aborter.start()
        time.sleep(0.05)  # 在途压缩未释放前，abort 必须阻塞等待而不是立刻返回
        self.assertFalse(abort_done.is_set())
        release.set()
        self.assertTrue(abort_done.wait(5))  # 在途压缩完成后 abort 收敛
        self.assertFalse(finalizer._thread.is_alive())

    def test_zstd_finalizer_propagates_fatal_error(self):
        # 致命配置错误（缺 zstandard 等）必须整批中止：消费线程暂存、collect 重抛，
        # 而不是把每个条目记成失败后让任务「完成」
        def fatal_chunk(entries):
            raise _ZstdFatalError("当前环境缺少 zstandard，无法生成 ZSTD 贴图。")

        finalizer = _ZstdFinalizer(fatal_chunk)
        finalizer.submit([{"entry": 1}])
        with self.assertRaises(_ZstdFatalError):
            finalizer.collect()
        self.assertFalse(finalizer._thread.is_alive())

    def test_get_backend_status_resolves_selected_backend(self):
        with patch.object(TexconvEncoder, "resolve_executable", return_value=self.temp_root / "texconv.exe"), \
             patch.object(ToddsEncoder, "resolve_executable", side_effect=TextureOptError("未找到 todds.exe。")):
            status = self.manager.get_backend_status({**self.options, "output_format": "dds", "encoder_backend": "texconv"})
        self.assertTrue(status["available"])
        self.assertEqual(status["backend"], "texconv")
        self.assertEqual(status["resolved_path"], str(self.temp_root / "texconv.exe"))

        # 所选档位缺失 → 明确不可用并指明档位，而不是静默回落 todds
        with patch.object(TexconvEncoder, "resolve_executable", side_effect=TextureOptError("未找到 texconv.exe。")), \
             patch.object(ToddsEncoder, "resolve_executable", return_value=self.temp_root / "todds.exe"):
            status = self.manager.get_backend_status({**self.options, "output_format": "dds", "encoder_backend": "texconv"})
        self.assertFalse(status["available"])
        self.assertEqual(status["backend"], "texconv")

    def test_get_encoder_status_plan_reflects_selected_backend(self):
        texconv_info_patch = patch(
            "backend.managers.mgr_texture_opt.texconv_info",
            return_value={"gpu_directcompute": True, "version": "1.0"},
        )
        with patch.object(ToddsEncoder, "resolve_executable", return_value=self.temp_root / "todds.exe"), \
             patch.object(TexconvEncoder, "resolve_executable", return_value=self.temp_root / "texconv.exe"), \
             patch.object(NvttEncoder, "resolve_executable", return_value=self.temp_root / "nvtt_export.exe"), \
             texconv_info_patch:
            status = self.manager.get_encoder_status({**self.options, "output_format": "dds", "encoder_backend": "nvtt"})
        plan = status["plan"]
        self.assertEqual(plan["selected_backend"], "nvtt")
        self.assertEqual(plan["effective_backend"], "nvtt")
        self.assertEqual(plan["nvtt_shards"], 6)

        # 所选档位缺失 → effective 回退 auto 落点（todds 可用即 todds）
        with patch.object(NvttEncoder, "resolve_executable", side_effect=TextureOptError("未找到 nvtt_export.exe。")), \
             patch.object(ToddsEncoder, "resolve_executable", return_value=self.temp_root / "todds.exe"), \
             patch.object(TexconvEncoder, "resolve_executable", return_value=self.temp_root / "texconv.exe"), \
             texconv_info_patch:
            status = self.manager.get_encoder_status({**self.options, "output_format": "dds", "encoder_backend": "nvtt"})
        plan = status["plan"]
        self.assertEqual(plan["effective_backend"], plan["backend"])

    def test_start_benchmark_requires_texconv(self):
        with patch.object(TexconvEncoder, "resolve_executable", side_effect=TextureOptError("未找到 texconv.exe。")):
            with self.assertRaises(TextureOptError) as ctx:
                self.manager.start_benchmark([str(self.mod_root)], options=self.options)
        self.assertIn("texconv", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
