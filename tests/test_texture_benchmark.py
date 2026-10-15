# tests/test_texture_benchmark.py
"""分片数采样基准与手动覆盖持久化测试。

覆盖：
- 覆盖值持久化 roundtrip 与 _build_options 生效链（显式 > 覆盖 > 基准推荐 > 默认）；
- get_encoder_status 的 plan.source 判定（default / benchmark / manual）；
- 采样：像素预算、尺寸过滤、alpha 池空回退、样本数上限；
- 择优：mpix_per_sec 最大者胜出；
- 分组执行：临时分片目录、进程参数（gpu_workers/cpu_workers）、产出清理。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image

import backend.managers.mgr_texture_opt as texture_opt_module
from backend.managers.mgr_texture_opt import (
    MAX_TEXTURE_WORKERS,
    MIN_TEXTURE_WORKERS,
    TextureOptimizationManager,
)


class TestWorkerBenchmark(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.benchmark_path = self.temp_root / "texture_benchmark.json"
        patcher = mock.patch.object(texture_opt_module, "TEXTURE_BENCHMARK_PATH", self.benchmark_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = TextureOptimizationManager()

    def _write_png(self, relative_path: str, size=(512, 512)) -> Path:
        path = self.temp_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", size, (255, 0, 0, 128)).save(path)
        return path

    # ---- 覆盖值持久化与生效链 ----

    def test_set_worker_overrides_persists_and_normalizes(self):
        self.manager.set_worker_overrides(2, 99)  # 99 越界 → 归零（跟随推荐）
        state = json.loads(self.benchmark_path.read_text(encoding="utf-8"))
        self.assertEqual(state["overrides"], {"gpu_workers": 2, "cpu_workers": 0})

    def test_build_options_prefers_explicit_then_override_then_benchmark(self):
        self.manager.set_worker_overrides(2, 4)
        merged = self.manager._build_options({})
        self.assertEqual(merged["gpu_workers"], 2)
        self.assertEqual(merged["cpu_workers"], 4)
        # 显式 options 赢过持久化覆盖
        merged = self.manager._build_options({"gpu_workers": 5})
        self.assertEqual(merged["gpu_workers"], 5)
        self.assertEqual(merged["cpu_workers"], 4)

    def test_build_options_falls_back_to_benchmark_recommendation(self):
        self.benchmark_path.write_text(
            json.dumps({"schema": 1, "benchmark": {"recommended": {"gpu_workers": 3, "cpu_workers": 6}}, "overrides": {"gpu_workers": 0, "cpu_workers": 0}}),
            encoding="utf-8",
        )
        merged = self.manager._build_options({})
        self.assertEqual(merged["gpu_workers"], 3)
        self.assertEqual(merged["cpu_workers"], 6)
        # 手动覆盖压过基准推荐
        self.benchmark_path.write_text(
            json.dumps({"schema": 1, "benchmark": {"recommended": {"gpu_workers": 3, "cpu_workers": 6}}, "overrides": {"gpu_workers": 4, "cpu_workers": 0}}),
            encoding="utf-8",
        )
        merged = self.manager._build_options({})
        self.assertEqual(merged["gpu_workers"], 4)
        self.assertEqual(merged["cpu_workers"], 6)

    # ---- get_encoder_status 来源标注 ----

    def _patch_encoders_for_status(self, gpu_probe: bool | None):
        fake_exe = self.temp_root / "texconv.exe"
        fake_exe.write_bytes(b"MZ")
        return [
            mock.patch.object(texture_opt_module.TexconvEncoder, "resolve_executable", return_value=fake_exe),
            mock.patch.object(texture_opt_module.ToddsEncoder, "resolve_executable", return_value=self.temp_root / "todds.exe"),
            mock.patch.object(texture_opt_module.NvttEncoder, "resolve_executable", return_value=self.temp_root / "nvtt.exe"),
            mock.patch.object(texture_opt_module, "texconv_info", return_value={"gpu_directcompute": gpu_probe, "version": "2026.5.8.1"}),
        ]

    def test_status_source_is_default_without_state(self):
        patchers = self._patch_encoders_for_status(True)
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        status = self.manager.get_encoder_status()
        self.assertEqual(status["plan"]["source"], "default")
        self.assertEqual(status["plan"]["overrides"], {"gpu_workers": 0, "cpu_workers": 0})
        self.assertIsNone(status["plan"]["benchmark"])

    def test_status_source_is_manual_and_benchmark(self):
        patchers = self._patch_encoders_for_status(True)
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.benchmark_path.write_text(
            json.dumps({"schema": 1, "benchmark": {"recommended": {"gpu_workers": 4, "cpu_workers": 6}}, "overrides": {"gpu_workers": 0, "cpu_workers": 0}}),
            encoding="utf-8",
        )
        status = self.manager.get_encoder_status()
        self.assertEqual(status["plan"]["source"], "benchmark")
        self.assertEqual(status["plan"]["gpu_workers"], 4)
        self.manager.set_worker_overrides(0, 2)
        status = self.manager.get_encoder_status()
        self.assertEqual(status["plan"]["source"], "manual")
        self.assertEqual(status["plan"]["cpu_workers"], 2)

    # ---- 采样 ----

    def _entry(self, relative_path: str, width: int, height: int, *, has_alpha: bool = True) -> dict[str, Any]:
        path = self._write_png(relative_path, (width, height))
        return {
            "source_path": str(path),
            "output_path": str(path.with_suffix(".dds")),
            "has_alpha": has_alpha,
            "width": width,
            "height": height,
        }

    def test_sample_respects_budget_count_and_size_filter(self):
        entries = [self._entry(f"m{i}/tex.png", 2048, 2048) for i in range(20)]  # 每张 ~4.2 MPix
        sampled = self.manager._sample_benchmark_entries(entries, route="alpha")
        self.assertLessEqual(len(sampled), 8)
        total_mpix = sum(int(item["width"]) * int(item["height"]) / 1_000_000 for item in sampled)
        self.assertLessEqual(total_mpix, 24.0 + 4.2)  # 预算 + 单张越界余量
        tiny = [self._entry(f"tiny{i}/t.png", 64, 64) for i in range(30)]
        self.assertEqual(self.manager._sample_benchmark_entries(tiny, route="alpha"), [])  # 尺寸不足全滤掉

    def test_sample_route_filters_and_any_fallback(self):
        # route="alpha" 只收透明样本（GPU 组负载）；"opaque" 只收不透明（CPU 组 BC1 负载）；
        # "any" 不过滤路由，作为 opaque 池不足时的兜底
        entries = [self._entry("a/a.png", 512, 512, has_alpha=True)]
        self.assertEqual(len(self.manager._sample_benchmark_entries(entries, route="alpha")), 1)
        self.assertEqual(len(self.manager._sample_benchmark_entries(entries, route="any")), 1)
        self.assertEqual(len(self.manager._sample_benchmark_entries(entries, route="opaque")), 0)
        opaque_present = [self._entry("b/b.png", 512, 512, has_alpha=False)]
        self.assertEqual(len(self.manager._sample_benchmark_entries(opaque_present, route="alpha")), 0)
        self.assertEqual(len(self.manager._sample_benchmark_entries(opaque_present, route="opaque")), 1)

    # ---- 择优 ----

    def test_pick_best_prefers_highest_throughput(self):
        results = [
            {"scope": "gpu_workers", "workers": 1, "seconds": 2.0, "mpix_per_sec": 5.0},
            {"scope": "gpu_workers", "workers": 3, "seconds": 1.0, "mpix_per_sec": 12.0},
            {"scope": "gpu_workers", "workers": 6, "seconds": 0.0, "mpix_per_sec": 0},
        ]
        best = self.manager._pick_benchmark_best(results)
        self.assertEqual(best["workers"], 3)
        self.assertIsNone(self.manager._pick_benchmark_best([{"workers": 1, "seconds": 0, "mpix_per_sec": 0}]))

    # ---- 分组执行 ----

    def test_benchmark_group_copies_samples_and_clears_outputs(self):
        samples = [self._entry(f"m{i}/tex.png", 256, 256) for i in range(3)]
        work_dir = self.temp_root / "bench"
        work_dir.mkdir()
        seen: dict[str, Any] = {}

        def fake_encode(self_encoder, cancel_event, *, entries, overwrite_existing, scale_percent, output_callback=None):
            seen["gpu_workers"] = self_encoder.options["gpu_workers"]
            seen["cpu_workers"] = self_encoder.options["cpu_workers"]
            seen["sources"] = sorted(str(item["source_path"]) for item in entries)
            seen["in_work_dir"] = all(str(item["source_path"]).startswith(str(work_dir)) for item in entries)
            for item in entries:  # 模拟 texconv 产出
                Path(item["output_path"]).write_bytes(b"DDS ")

        with mock.patch.object(texture_opt_module.TexconvEncoder, "encode_batch", fake_encode):
            result = self.manager._run_benchmark_group(
                samples=samples,
                work_dir=work_dir,
                workers_key="gpu_workers",
                workers_value=3,
                options={"gpu_workers": 0, "cpu_workers": 0},
                cancel_event=threading.Event(),
            )
        self.assertEqual(seen["gpu_workers"], 3)
        self.assertEqual(seen["cpu_workers"], 2)  # 另一维固定小值，避免干扰计时
        self.assertTrue(seen["in_work_dir"])  # 样本复制进临时目录（源只读）
        self.assertEqual(len(seen["sources"]), 3)
        self.assertEqual(result["workers"], 3)
        self.assertGreater(result["mpix_per_sec"], 0)
        self.assertEqual(list(work_dir.rglob("*.dds")), [])  # 产出已清理，不污染下一轮


if __name__ == "__main__":
    unittest.main()


class TestBenchmarkTimestampContract(unittest.TestCase):
    """本用例验证 `mgr_texture_opt.py` 里写的是 `datetime.datetime.now()` 而不是
    `datetime.now()`（模块导入无该属性，基准任务写 sampled_at 时会抛
    module 'datetime' has no attribute 'now'）。
    以源码断言约束：本模块 `import datetime`（模块），不允许出现裸 `datetime.now(` 调用。"""

    def test_no_bare_datetime_module_now_call(self):
        import re

        source = Path(texture_opt_module.__file__).read_text(encoding="utf-8")
        # (?<!datetime\.) 排除合法的 datetime.datetime.now(
        self.assertIsNone(
            re.search(r"(?<!datetime\.)\bdatetime\.now\(", source),
            "mgr_texture_opt.py 使用了 `import datetime`（模块），裸 datetime.now() 会抛 "
            "AttributeError；应写 datetime.datetime.now()",
        )

    def test_sampled_at_payload_timestamp_is_isoformat(self):
        # 验证：该表达式可直接执行（与 mgr_texture_opt.py 内的用法同形态）
        stamp = texture_opt_module.datetime.datetime.now().isoformat(timespec="seconds")
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


class TestActiveTargetsForBenchmark(unittest.TestCase):
    """基准采样必须拿「全部启用模组」——resolve_targets 空 package_ids 恒返回空。"""

    def test_collect_active_targets_uses_resolver_without_disabled(self):
        with mock.patch.object(texture_opt_module, "TextureTargetResolver") as resolver_cls:
            texture_opt_module.TextureOptimizationManager().collect_active_targets("ctx")
            resolver_cls.assert_called_once_with("ctx")
            resolver_cls.return_value.collect_installed_targets.assert_called_once_with(include_disabled=False)
