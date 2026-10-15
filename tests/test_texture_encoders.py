# tests/test_texture_encoders.py
"""编码器叶子层测试。

覆盖四件事：

- ``resolve_worker_plan`` 的机器自适应数学：cpu=clamp(逻辑核−2, 2, 6)、
  gpu 默认 3 / 探测失败 0、显式覆盖优先于自动值；
- ``TexconvEncoder._build_invocation_units`` 分片分组：目录 × 路由 × mip
  级数，``has_alpha`` 缺失保守按 alpha；
- mip 级数 = ``max(w,h).bit_length()``（``-m N`` 是精确级数语义）；
- texconv 命令构造（基准逐字）与 ``-flist`` 生命周期、GPU→CPU 降级、
  工厂落点（GUI 默认不变）、``ToolRunError`` 可恢复映射、
  ``_ProgressCounter`` 单调不超 total。

进程真实执行不在本文件范围（由 ``_ToolProcessRunner`` 与
``test_texture_opt_batch_recovery`` 覆盖）；本文件一律 mock ``run_command`` 捕获 argv。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from backend.managers import texture_encoders
from backend.managers.texture_encoders import (
    NvttEncoder,
    TexconvEncoder,
    TextureOptError,
    ToddsEncoder,
    ToolRunError,
    _ProgressCounter,
    _TexconvUnit,
    _pending_entries,
    build_invocation_units,
    create_encoder,
    resolve_auto_backend,
    resolve_mip_levels,
    resolve_worker_plan,
)

FAKE_TEXCONV = Path(r"C:\RimCrowTools\todds\texconv.exe")


def _entry(source_path: Any, *, has_alpha: bool = True, width: int = 64, height: int = 64) -> dict[str, Any]:
    """扫描管线产物条目（entries 协议）。"""
    path = str(source_path)
    return {
        "source_path": path,
        "has_alpha": has_alpha,
        "width": width,
        "height": height,
        "output_path": str(Path(path).with_suffix(".dds")),
    }


def _argv_pair(command: list[str], flag: str) -> str:
    """取 ``[flag, value]`` 对的值；缺失时抛 IndexError（等价于断言失败）。"""
    return command[command.index(flag) + 1]


class TestResolveWorkerPlan(unittest.TestCase):
    """cpu=clamp(核数−2, 2, 6)，gpu 默认 3、探测失败 0，显式覆盖优先。"""

    @staticmethod
    def _plan(options: dict[str, Any] | None = None, *, cores: int = 16, gpu_available: bool | None = None) -> dict[str, int]:
        with mock.patch.object(texture_encoders.os, "cpu_count", return_value=cores):
            return resolve_worker_plan(options or {}, gpu_available=gpu_available)

    def test_cpu_auto_follows_logical_cores(self):
        self.assertEqual(self._plan(cores=16)["cpu_workers"], 6)  # clamp(14, 2, 6)
        self.assertEqual(self._plan(cores=5)["cpu_workers"], 3)  # clamp(3, 2, 6)
        self.assertEqual(self._plan(cores=4)["cpu_workers"], 2)  # clamp(2, 2, 6) = 下限
        self.assertEqual(self._plan(cores=3)["cpu_workers"], 2)  # 核数−2 < 下限 → 2
        self.assertEqual(self._plan(cores=64)["cpu_workers"], 6)  # 上限 6

    def test_reports_logical_cores(self):
        self.assertEqual(self._plan(cores=24)["cpu_logical"], 24)

    def test_explicit_override_beats_auto(self):
        plan = self._plan({"cpu_workers": 4, "gpu_workers": 7}, cores=16, gpu_available=False)
        self.assertEqual(plan["cpu_workers"], 4)
        self.assertEqual(plan["gpu_workers"], 7)

    def test_gpu_default_probe_and_failure(self):
        self.assertEqual(self._plan(cores=16)["gpu_workers"], 3)  # 未探测：乐观默认（编码前会重新探测）
        self.assertEqual(self._plan(cores=16, gpu_available=True)["gpu_workers"], 3)
        self.assertEqual(
            self._plan(cores=16, gpu_available=False)["gpu_workers"],
            0,  # 探测失败 → alpha 全走 CPU BC7
        )

    def test_out_of_range_override_falls_back_to_auto(self):
        # 1..12 之外 / 非整数 = 无效覆盖，回落自动值（gpu 侧叠加探测结果）
        for bad in (0, 13, -1, "abc", None, ""):
            with self.subTest(override=bad):
                plan = self._plan({"cpu_workers": bad, "gpu_workers": bad}, cores=16, gpu_available=False)
                self.assertEqual(plan["cpu_workers"], 6)
                self.assertEqual(plan["gpu_workers"], 0)


class TestInvocationUnits(unittest.TestCase):
    """分片分组：目录 × 路由 × mip 级数；``has_alpha`` 缺失保守按 alpha。"""

    @staticmethod
    def _units(entries: list[dict[str, Any]], *, gpu_workers: int = 3, **options: Any) -> list[_TexconvUnit]:
        return TexconvEncoder(options)._build_invocation_units(entries, gpu_workers=gpu_workers)

    def test_same_directory_route_and_mips_merge_into_one_unit(self):
        units = self._units(
            [
                _entry(r"E:/Mods/ExampleMod/Textures/a.png"),
                _entry(r"E:/Mods/ExampleMod/Textures/b.png"),
            ]
        )
        self.assertEqual(len(units), 1)
        self.assertEqual(len(units[0].entries), 2)
        self.assertEqual(units[0].directory, str(Path(r"E:/Mods/ExampleMod/Textures")))
        self.assertEqual(units[0].route, "alpha")
        self.assertEqual(units[0].mip_levels, 7)

    def test_alpha_and_opaque_split(self):
        units = self._units(
            [
                _entry(r"E:/M/Textures/a.png", has_alpha=True),
                _entry(r"E:/M/Textures/b.png", has_alpha=False),
            ]
        )
        self.assertEqual({unit.route for unit in units}, {"alpha", "opaque"})

    def test_different_directory_splits(self):
        units = self._units(
            [
                _entry(r"E:/M1/Textures/a.png"),
                _entry(r"E:/M2/Textures/b.png"),
            ]
        )
        self.assertEqual(len(units), 2)
        self.assertEqual({unit.directory for unit in units}, {str(Path(r"E:/M1/Textures")), str(Path(r"E:/M2/Textures"))})

    def test_different_mip_level_splits(self):
        units = self._units(
            [
                _entry(r"E:/M/Textures/a.png", width=64, height=64),
                _entry(r"E:/M/Textures/b.png", width=256, height=256),
            ]
        )
        self.assertEqual(len(units), 2)
        self.assertEqual({unit.mip_levels for unit in units}, {7, 9})

    def test_missing_has_alpha_is_conservative_alpha(self):
        # 数据损失方向必须保守：未知 → BC7（体积代价），绝不静默进 BC1
        entry = _entry(r"E:/M/Textures/a.png")
        entry.pop("has_alpha")
        units = self._units([entry])
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].route, "alpha")

    def test_generate_mipmaps_off_yields_single_mip_for_every_unit(self):
        # texconv 的「去掉 mip」是 -m 1（-m 0 = 生成全链），见 TestMipLevels
        units = self._units(
            [
                _entry(r"E:/M/Textures/a.png", has_alpha=True),
                _entry(r"E:/M/Textures/b.png", has_alpha=False),
            ],
            generate_mipmaps=False,
        )
        self.assertEqual({unit.mip_levels for unit in units}, {1})

    def test_use_gpu_only_for_alpha_route_with_workers(self):
        entries = [
            _entry(r"E:/M/Textures/a.png", has_alpha=True),
            _entry(r"E:/M/Textures/b.png", has_alpha=False),
        ]
        with_workers = self._units(entries, gpu_workers=3)
        self.assertEqual({unit.route: unit.use_gpu for unit in with_workers}, {"alpha": True, "opaque": False})
        without_workers = self._units(entries, gpu_workers=0)
        self.assertEqual({unit.route: unit.use_gpu for unit in without_workers}, {"alpha": False, "opaque": False})

    def test_odd_dimensions_keep_logical_mip_levels(self):
        # ceil4 只是 BC 存储补齐，mip 链仍按逻辑尺寸
        # （3×3 → 2 级而非 3 级）；编码输入的取整在 prepare_unit_inputs
        units = self._units([_entry(r"E:/M/Textures/a.png", width=1023, height=7)])
        self.assertEqual(units[0].mip_levels, 10)


class TestMipLevels(unittest.TestCase):
    """``-m N`` 级数取值（官方语义 + 本机实测）。

    实测方法：64px / 100px PNG → texconv 转 DDS → 读 DDS 头 offset 28 的
    ``dwMipMapCount``。结果：``-m 0``→7（全链）、``-m 1``→1、``-m 7``→7、
    ``-m 10``→``E_INVALIDARG 80070057``。即 ``-m 0`` **不是**「不生成 mip」。
    """

    @staticmethod
    def _mips(width: int, height: int, *, generate_mipmaps: bool = True) -> int:
        units = TexconvEncoder({"generate_mipmaps": generate_mipmaps})._build_invocation_units(
            [_entry(r"E:/M/Textures/a.png", width=width, height=height)],
            gpu_workers=3,
        )
        return units[0].mip_levels

    def test_full_chain_is_bit_length_of_max_dimension(self):
        for dimension, expected in ((1, 1), (3, 2), (64, 7), (100, 7), (128, 8), (256, 9), (1024, 11)):
            with self.subTest(dimension=dimension):
                self.assertEqual(self._mips(dimension, dimension), expected)
                self.assertEqual(self._mips(dimension, 1), expected)  # 取 max(w, h)

    def test_generate_mipmaps_off_yields_single_mip_not_zero(self):
        # 回归点：0 在 texconv 里 = 生成全链，会与「不生成」意图完全相反
        self.assertEqual(self._mips(64, 64, generate_mipmaps=False), 1)
        self.assertEqual(resolve_mip_levels(64, 64, generate_mipmaps=False), 1)

    def test_unknown_dimensions_fall_back_to_texconv_default(self):
        # 尺寸未知时交回 texconv 自身缺省（= 全链），仅在 generate_mipmaps=True 分支
        self.assertEqual(self._mips(0, 0), 0)
        self.assertEqual(resolve_mip_levels(0, 0), 0)
        self.assertEqual(resolve_mip_levels(0, 0, generate_mipmaps=False), 1)

    def test_resolve_mip_levels_is_the_single_source_of_truth(self):
        # 分片分组必须与纯函数同源，避免两处各写一套级数规则
        self.assertEqual(self._mips(64, 64), resolve_mip_levels(64, 64))
        self.assertEqual(self._mips(64, 64, generate_mipmaps=False), resolve_mip_levels(64, 64, generate_mipmaps=False))

    def test_scaling_groups_by_target_dimensions(self):
        """分组键必须反映**真正喂给编码器的图像**。

        预缩放后实际尺寸小于原图：256×256 按原图分组给 `-m 9`，预缩放后实际是 128×128
        （最大 8 级）→ texconv `E_INVALIDARG 80070057`。
        """
        scaled = TexconvEncoder({"generate_mipmaps": True})._build_invocation_units(
            [_entry(r"E:/M/Textures/a.png", width=256, height=256)],
            gpu_workers=1,
            scale_percent=50,
        )
        self.assertEqual(scaled[0].mip_levels, 8)  # 目标 128 → 全链 8 级，不是原图的 9

        # 原尺寸同属一个 bit_length、但缩放后目标不同的必须拆成不同分片
        # 130 → ceil4(65)=68 → 7 级；255 → ceil4(128)=128 → 8 级
        units = build_invocation_units(
            [
                _entry(r"E:/M/Textures/a.png", width=130, height=130),
                _entry(r"E:/M/Textures/b.png", width=255, height=255),
            ],
            gpu_workers=1,
            generate_mipmaps=True,
            scale_percent=50,
        )
        self.assertEqual({unit.mip_levels for unit in units}, {7, 8})
        self.assertEqual(len(units), 2)

        # 不缩放时行为不变（仍按原尺寸）
        unchanged = TexconvEncoder({"generate_mipmaps": True})._build_invocation_units(
            [_entry(r"E:/M/Textures/a.png", width=256, height=256)],
            gpu_workers=1,
            scale_percent=None,
        )
        self.assertEqual(unchanged[0].mip_levels, 9)


class TestTexconvCommand(unittest.TestCase):
    """基准逐字命令与 argv 文件操作数（含分块与失败隔离）。"""

    def setUp(self) -> None:
        self.cancel = threading.Event()
        self.calls: list[dict[str, Any]] = []
        self.progress: list[str] = []
        self.emit_writing_lines = 0
        self.fail_on_calls: set[int] = set()
        patchers = [
            mock.patch.object(TexconvEncoder, "resolve_executable", return_value=FAKE_TEXCONV),
            mock.patch.object(texture_encoders._ToolProcessRunner, "run_command", side_effect=self._record),
            # 真探测会起子进程；本文件只关心分片调度，GPU 可用性一律给定值
            mock.patch.object(texture_encoders, "probe_gpu", return_value=False),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _record(
        self,
        command: list[str],
        cancel_event: threading.Event,
        timeout_seconds: float | None = None,
        *,
        tool_name: str = "",
        output_callback: Any = None,
    ) -> None:
        # texconv argv 直传后，-o 之后的全部操作数都是本轮要编码的文件路径
        output_flag_index = command.index("-o")
        self.calls.append(
            {
                "command": list(command),
                "inputs": list(command[output_flag_index + 2 :]),
                "tool_name": tool_name,
                "timeout_seconds": timeout_seconds,
            }
        )
        if len(self.calls) in self.fail_on_calls:
            raise ToolRunError(f"{tool_name or 'texconv'} 执行失败: synthetic failure", kind="failed", log_path="C:/logs/texconv_1.log")
        if output_callback is not None:
            for index in range(self.emit_writing_lines):
                output_callback(f"writing out_{index}.dds")

    def _encode(
        self,
        entries: list[dict[str, Any]],
        *,
        overwrite: bool = True,
        scale_percent: int | None = None,
        on_unit_done: Any = None,
        **options: Any,
    ) -> list[dict[str, Any]]:
        encoder = TexconvEncoder({"gpu_workers": 1, "cpu_workers": 2, **options})
        encoder.encode_batch(
            self.cancel,
            entries=entries,
            overwrite_existing=overwrite,
            scale_percent=scale_percent,
            output_callback=self.progress.append,
            on_unit_done=on_unit_done,
        )
        return self.calls

    def test_unit_done_fires_once_per_unit_after_success(self):
        # 单元 = 目录×路由×mip：两个目录 → 两次回调，条目与单元一一对应
        done: list[list[dict[str, Any]]] = []
        self._encode(
            [
                _entry(r"E:/M/Textures/a.png", width=64, height=64),
                _entry(r"E:/M/Textures2/b.png", width=64, height=64),
            ],
            on_unit_done=done.append,
        )
        self.assertEqual(len(done), 2)
        self.assertEqual(
            sorted(Path(entry["source_path"]).name for group in done for entry in group),
            ["a.png", "b.png"],
        )

    def test_single_gpu_unit_failure_still_degrades_to_cpu(self):
        # 偶发（TDR/坏图）容错：GPU 块失败 + 逐文件重试也失败（call1/call2）才进降级
        # 路径，首个失败降级 CPU 重跑，整批照常完成
        self.fail_on_calls = {1, 2}
        calls = self._encode([_entry(r"E:/M/Textures/a.png", width=64, height=64)])
        self.assertGreaterEqual(len(calls), 3)  # GPU 块 + 逐文件 + CPU 重跑
        self.assertIn("-nogpu", " ".join(calls[-1]["command"]))

    def test_repeated_gpu_unit_failures_trip_breaker(self):
        # 同批第二个 GPU 分片失败 → 熔断快速失效：不再静默降级（纯 CPU texconv BC7
        # 慢于 todds，是用户不知情的性能悬崖），错误消息必须给出换档建议
        # 调用序：unit1 GPU 块(1)失败 → 逐文件(2)失败 → 降级 CPU(3)成功；
        #         unit2 GPU 块(4)失败 → 逐文件(5)失败 → 计数到 2 → 熔断
        self.fail_on_calls = {1, 2, 4, 5}
        with self.assertRaises(ToolRunError) as ctx:
            self._encode([
                _entry(r"E:/M1/Textures/a.png", width=64, height=64),
                _entry(r"E:/M2/Textures/b.png", width=64, height=64),
            ])
        self.assertIn("建议", str(ctx.exception))
        self.assertIn("todds", str(ctx.exception))

    def test_unit_done_not_fired_when_unit_fails(self):
        # 失败单元不回调：其条目由管理器的失败/重试路径处理，不能被当成已落盘。
        # 用「输入准备失败」构造终结失败——文件级失败会被逐文件隔离吞掉、
        # GPU 单元会降级 CPU 重跑，都不会让单元终结（见既有隔离/降级用例）。
        done: list[list[dict[str, Any]]] = []
        with mock.patch.object(texture_encoders, "prepare_unit_inputs", side_effect=OSError("disk full")):
            with self.assertRaises(ToolRunError):
                self._encode([_entry(r"E:/M/Textures/a.png", width=64, height=64)], on_unit_done=done.append)
        self.assertEqual(done, [])

    def test_gpu_unit_is_bc7_without_nogpu(self):
        calls = self._encode([_entry(r"E:/M/Textures/a.png", width=64, height=64)])
        self.assertEqual(len(calls), 1)
        command = calls[0]["command"]
        self.assertEqual(command[0], str(FAKE_TEXCONV))
        for flag in ("-nologo", "-y", "-vflip"):
            self.assertIn(flag, command)
        self.assertEqual(_argv_pair(command, "-m"), "7")
        self.assertEqual(_argv_pair(command, "-f"), "BC7_UNORM")
        self.assertNotIn("-nogpu", command)
        self.assertEqual(_argv_pair(command, "-o"), str(Path(r"E:/M/Textures")))
        self.assertEqual(calls[0]["inputs"], [r"E:/M/Textures/a.png"])  # 文件操作数 argv 直传
        self.assertNotIn("-flist", command)
        self.assertEqual(calls[0]["tool_name"], "texconv")

    def test_no_mipmaps_emits_m1_not_m0(self):
        # -m 0 在 texconv 里是「生成全链」，与「不生成」意图相反（实测 64px → 7 级）
        calls = self._encode([_entry(r"E:/M/Textures/a.png", width=64, height=64)], generate_mipmaps=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(_argv_pair(calls[0]["command"], "-m"), "1")

    def test_cpu_opaque_unit_is_nogpu_bc1(self):
        calls = self._encode([_entry(r"E:/M/Textures/b.png", has_alpha=False, width=64, height=64)])
        self.assertEqual(len(calls), 1)
        command = calls[0]["command"]
        self.assertIn("-nogpu", command)
        self.assertEqual(_argv_pair(command, "-f"), "BC1_UNORM")
        self.assertEqual(_argv_pair(command, "-o"), str(Path(r"E:/M/Textures")))

    def test_alpha_without_gpu_workers_keeps_bc7_on_cpu(self):
        calls = self._encode([_entry(r"E:/M/Textures/a.png", width=64, height=64)], gpu_workers=0)
        self.assertEqual(len(calls), 1)
        command = calls[0]["command"]
        self.assertIn("-nogpu", command)
        self.assertEqual(_argv_pair(command, "-f"), "BC7_UNORM")  # 降级不降质量档

    def test_units_are_sliced_per_directory_and_route(self):
        calls = self._encode(
            [
                _entry(r"E:/M1/Textures/a.png", has_alpha=True),
                _entry(r"E:/M1/Textures/b.png", has_alpha=False),
                _entry(r"E:/M2/Textures/c.png", has_alpha=True),
            ]
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            {(call["command"][call["command"].index("-o") + 1], _argv_pair(call["command"], "-f")) for call in calls},
            {
                (str(Path(r"E:/M1/Textures")), "BC7_UNORM"),
                (str(Path(r"E:/M1/Textures")), "BC1_UNORM"),
                (str(Path(r"E:/M2/Textures")), "BC7_UNORM"),
            },
        )

    def test_flist_holds_all_paths_and_is_removed_afterwards(self):
        paths = [r"E:/M/Textures/a.png", r"E:/M/Textures/b.png", r"E:/M/Textures/c.png"]
        calls = self._encode([_entry(path) for path in paths])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["inputs"], paths)  # 同一分片的全部条目一次 argv 直传

    def test_progress_lines_aggregate_writing_output(self):
        self.emit_writing_lines = 2
        calls = self._encode(
            [
                _entry(r"E:/M/Textures/a.png"),
                _entry(r"E:/M/Textures/b.png"),
            ]
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.progress, ["Progress: 1/2", "Progress: 2/2"])

    def test_gpu_failure_falls_back_to_cpu_once(self):
        # 块失败 → 同路由逐文件隔离仍失败 → 整片 CPU 降级（fail 1=GPU 块、2=GPU 单文件）
        self.fail_on_calls = {1, 2}
        calls = self._encode([_entry(r"E:/M/Textures/a.png", width=64, height=64)])
        self.assertEqual(len(calls), 3)
        self.assertNotIn("-nogpu", calls[0]["command"])  # 第一次 GPU 块
        self.assertNotIn("-nogpu", calls[1]["command"])  # GPU 单文件隔离重试
        self.assertEqual(calls[1]["inputs"], [r"E:/M/Textures/a.png"])
        self.assertIn("-nogpu", calls[2]["command"])  # 第三次整片降级 CPU
        self.assertEqual(_argv_pair(calls[2]["command"], "-f"), "BC7_UNORM")  # 降级不降质量档
        self.assertEqual(_argv_pair(calls[0]["command"], "-o"), _argv_pair(calls[2]["command"], "-o"))
        self.assertEqual(calls[0]["inputs"], calls[2]["inputs"])  # 同一批条目

    def test_gpu_chunk_failure_with_single_file_success_stays_on_gpu(self):
        # 块失败但单文件重跑成功 → 文件已编码，不再触发 CPU 降级
        self.fail_on_calls = {1}
        calls = self._encode(
            [
                _entry(r"E:/M/Textures/a.png", width=64, height=64),
                _entry(r"E:/M/Textures/b.png", width=64, height=64),
            ]
        )
        self.assertEqual(len(calls), 3)  # GPU 块 + 2 次 GPU 单文件隔离
        for call in calls:
            self.assertNotIn("-nogpu", call["command"])

    def test_cpu_chunk_failure_isolates_bad_file_without_raising(self):
        # CPU 块失败 → 逐文件隔离；坏文件记日志、好文件产出，不上抛（交管理器 verify 按条目标失败）
        self.fail_on_calls = {1, 3}  # CPU 块 + b.png 单文件失败
        calls = self._encode(
            [
                _entry(r"E:/M/Textures/a.png", has_alpha=False, width=64, height=64),
                _entry(r"E:/M/Textures/b.png", has_alpha=False, width=64, height=64),
            ]
        )
        self.assertEqual(len(calls), 3)  # CPU 块 + a/b 两次单文件隔离
        for call in calls:
            self.assertIn("-nogpu", call["command"])
        self.assertEqual(calls[1]["inputs"], [r"E:/M/Textures/a.png"])
        self.assertEqual(calls[2]["inputs"], [r"E:/M/Textures/b.png"])

    def test_timeout_is_not_isolated_and_propagates(self):
        # 超时不是「个别文件坏」，隔离只会放大浪费 → 立即上抛
        self.fail_on_calls = {1}

        def fail_timeout(command, cancel_event, timeout_seconds=None, *, tool_name="", output_callback=None):
            raise ToolRunError(f"{tool_name} 执行超时: synthetic", kind="timeout", log_path="C:/logs/texconv_1.log")

        with mock.patch.object(texture_encoders._ToolProcessRunner, "run_command", side_effect=fail_timeout):
            with self.assertRaises(ToolRunError) as ctx:
                self._encode([_entry(r"E:/M/Textures/a.png", width=64, height=64)])
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_gpu_and_cpu_all_fail_then_error_propagates_from_fallback(self):
        # kind=failed 已被隔离语义吸收（坏文件交管理器按条目标失败）；
        # 降级 CPU 后再遇 kind=timeout 才会原样上抛（隔离只针对 failed）。
        self.fail_on_calls = {1, 2}  # GPU 块 + GPU 单文件

        def fail_timeout(command, cancel_event, timeout_seconds=None, *, tool_name="", output_callback=None):
            raise ToolRunError(f"{tool_name} 执行超时: synthetic", kind="timeout", log_path="C:/logs/texconv_2.log")

        real_record = self._record
        call_counter = {"n": 0}

        def mixed_record(command, cancel_event, timeout_seconds=None, *, tool_name="", output_callback=None):
            call_counter["n"] += 1
            if call_counter["n"] >= 3:  # 第 3 次起是 CPU 降级块 → 超时
                fail_timeout(command, cancel_event, timeout_seconds, tool_name=tool_name, output_callback=output_callback)
            real_record(command, cancel_event, timeout_seconds, tool_name=tool_name, output_callback=output_callback)

        with mock.patch.object(texture_encoders._ToolProcessRunner, "run_command", side_effect=mixed_record):
            with self.assertRaises(ToolRunError) as ctx:
                self._encode([_entry(r"E:/M/Textures/a.png", width=64, height=64)])
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertTrue(ctx.exception.log_path)

    def test_existing_outputs_are_skipped_incrementally(self):
        # overwrite=false + 输出已存在 → 不派进程（增量补缺语义）
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            entries = []
            for name in ("a", "b"):
                (root / f"{name}.png").write_bytes(b"\x89PNG")
                (root / f"{name}.dds").write_bytes(b"DDS ")  # 已有输出
                entries.append(_entry(root / f"{name}.png"))
            calls = self._encode(entries, overwrite=False)
        self.assertEqual(calls, [])
        self.assertEqual(self.progress, [])  # done_base 直接补满，无多余进度行

    def test_empty_entries_short_circuit(self):
        encoder = TexconvEncoder({"gpu_workers": 1})
        encoder.encode_batch(self.cancel, entries=[], overwrite_existing=True, scale_percent=None)
        self.assertEqual(self.calls, [])


class TestFactoryAndBackendResolution(unittest.TestCase):
    """工厂落点与 auto 解析（GUI 默认 todds 保持不变）。"""

    def test_default_and_explicit_todds_keep_gui_behaviour(self):
        self.assertIsInstance(create_encoder({}), ToddsEncoder)
        self.assertIsInstance(create_encoder({"encoder_backend": "todds"}), ToddsEncoder)
        self.assertIsInstance(create_encoder({"encoder_backend": ""}), ToddsEncoder)

    def test_unknown_backend_degrades_to_todds(self):
        self.assertIsInstance(create_encoder({"encoder_backend": "directx"}), ToddsEncoder)

    def test_auto_is_independent_of_scaling(self):
        # 验证：缩放由包装层预缩放承接 → auto 不按「是否缩放」挑后端，
        # create_encoder 也不接收 needs_scaling。
        import inspect

        self.assertNotIn("needs_scaling", inspect.signature(create_encoder).parameters)
        with mock.patch.object(texture_encoders, "resolve_auto_backend", return_value="texconv") as auto:
            self.assertIsInstance(create_encoder({"encoder_backend": "auto"}), TexconvEncoder)
        auto.assert_called_once()

    def test_auto_resolves_to_texconv_or_todds_by_availability(self):
        with mock.patch.object(texture_encoders, "resolve_auto_backend", return_value="texconv"):
            self.assertIsInstance(create_encoder({"encoder_backend": "auto"}), TexconvEncoder)
        with mock.patch.object(texture_encoders, "resolve_auto_backend", return_value="todds"):
            self.assertIsInstance(create_encoder({"encoder_backend": "auto"}), ToddsEncoder)

    def test_explicit_texconv_backend_wins(self):
        self.assertIsInstance(create_encoder({"encoder_backend": "texconv"}), TexconvEncoder)

    def test_explicit_nvtt_backend_wins(self):
        self.assertIsInstance(create_encoder({"encoder_backend": "nvtt"}), NvttEncoder)

    def test_resolve_auto_backend_prefers_todds_quality_baseline(self):
        # 质量恒定原则 → todds 在位时 auto 落 todds（质量基准）
        with mock.patch.object(ToddsEncoder, "resolve_executable", return_value=Path(r"C:\t\todds.exe")):
            self.assertEqual(resolve_auto_backend({}), "todds")

    def test_resolve_auto_backend_falls_back_to_texconv_without_todds(self):
        # todds 缺位 + texconv 在 → texconv（速度档，质量代价由 GUI 标注）
        with mock.patch.object(ToddsEncoder, "resolve_executable", side_effect=TextureOptError("missing")), \
             mock.patch.object(TexconvEncoder, "resolve_executable", return_value=FAKE_TEXCONV):
            self.assertEqual(resolve_auto_backend({}), "texconv")

    def test_resolve_auto_backend_double_missing_still_todds_for_causal_error(self):
        # 双缺 → todds（其 resolve_executable 的报错带安装指引，因果最完整）
        with mock.patch.object(ToddsEncoder, "resolve_executable", side_effect=TextureOptError("missing")), \
             mock.patch.object(TexconvEncoder, "resolve_executable", side_effect=TextureOptError("missing")):
            self.assertEqual(resolve_auto_backend({}), "todds")


class TestErrorContract(unittest.TestCase):
    """验证：ToolRunError 才是可恢复的「工具真跑过」错误。"""

    def test_tool_run_error_carries_kind_and_log_path(self):
        exc = ToolRunError("todds 执行失败: bad source", kind="failed", log_path=r"C:\logs\todds_a.log")
        self.assertIsInstance(exc, TextureOptError)
        self.assertIsInstance(exc, ToolRunError)
        self.assertEqual(exc.kind, "failed")
        self.assertEqual(exc.log_path, r"C:\logs\todds_a.log")
        self.assertEqual(str(exc), "todds 执行失败: bad source")

    def test_kind_defaults_to_failed_and_blank_normalizes(self):
        self.assertEqual(ToolRunError("x", kind="").kind, "failed")
        self.assertEqual(ToolRunError("x", kind="timeout").kind, "timeout")

    def test_config_error_is_not_recoverable(self):
        # 启动失败等配置类错误不是 ToolRunError → 管理器不进逐条重试管线
        self.assertNotIsInstance(TextureOptError("未找到 todds.exe。请在贴图优化中心下载 todds。"), ToolRunError)


class TestTexconvPrescale(unittest.TestCase):
    """验证：非 todds 编码器由包装层做 LANCZOS 预缩放，编码器本身不拒绝缩放。

    断言三件事：读的是**临时文件**（不是源文件）、尺寸确实是 50%、源 PNG 未被改写；
    并且临时文件名保持原 stem（texconv `-o <源目录>` 才能零搬运落到 output_path）。
    """

    def test_scale_goes_through_prescale_instead_of_rejection(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "a.png"
            Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(source)
            entry = _entry(source)
            seen: dict[str, Any] = {}

            def fake_run(command, cancel_event, timeout_seconds=None, *, tool_name=None, output_callback=None):
                output_flag_index = command.index("-o")
                inputs = list(command[output_flag_index + 2 :])
                seen["inputs"] = inputs
                seen["sizes"] = [Image.open(path).size for path in inputs]
                seen["in_source_dir"] = all(Path(path).parent == root for path in inputs)
                seen["cpu"] = "-nogpu" in command

            with mock.patch.object(TexconvEncoder, "resolve_executable", return_value=FAKE_TEXCONV), \
                 mock.patch.object(texture_encoders._ToolProcessRunner, "run_command", side_effect=fake_run):
                TexconvEncoder({"gpu_workers": 1}).encode_batch(
                    threading.Event(),
                    entries=[entry],
                    overwrite_existing=True,
                    scale_percent=50,
                )

            self.assertEqual(len(seen["inputs"]), 1)
            prescaled = Path(seen["inputs"][0])
            self.assertNotEqual(prescaled, source)          # 读的是临时文件
            self.assertEqual(prescaled.name, "a.png")       # stem 保持 → 零搬运
            self.assertFalse(seen["in_source_dir"])         # 不在源目录里（源 PNG 不被覆盖）
            self.assertEqual(seen["sizes"], [(32, 32)])     # ceil4(round(64 * 0.5)) = 32
            self.assertFalse(prescaled.parent.exists())     # 跑完已清理
            with Image.open(source) as original:
                self.assertEqual(original.size, (64, 64))   # 源 PNG 字节未被改写


class TestNvttEncoder(unittest.TestCase):
    """NVTT 分批/分块/进度约定（成功路径静默 → 进度按块自推）。"""

    def test_executable_resolution_prefers_options_then_tools_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp)
            fake = tools_dir / "nvtt_export.exe"
            fake.write_bytes(b"MZ")
            resolved = NvttEncoder({"texture_tools_path": str(tools_dir)}).resolve_executable()
            self.assertEqual(resolved, fake)

    def test_missing_executable_is_causal_with_install_hint(self):
        # 屏蔽全部候选位（自定义路径 / texture_tools / Program Files×2 / PATH）→ 必须给安装指引
        import os

        with mock.patch.object(texture_encoders, "resolve_texture_tools_path", return_value=Path("X:/nowhere")), \
             mock.patch.object(texture_encoders.shutil, "which", return_value=None), \
             mock.patch.dict(os.environ, {"ProgramFiles": "X:/nowhere_pf", "ProgramFiles(x86)": "X:/nowhere_x86"}):
            with self.assertRaises(TextureOptError) as ctx:
                NvttEncoder({}).resolve_executable()
        message = str(ctx.exception)
        self.assertIn("nvtt_export.exe", message)
        self.assertIn("download_texture_encoder", message)
        self.assertNotIsInstance(ctx.exception, ToolRunError)  # 配置类 → 不进逐条重试

    def test_shards_default_six_and_gpu_workers_overrides(self):
        self.assertEqual(NvttEncoder({}).resolve_shards(), 6)          # N9：不是 texconv 的 3
        self.assertEqual(NvttEncoder({"gpu_workers": 2}).resolve_shards(), 2)
        self.assertEqual(NvttEncoder({"gpu_workers": 99}).resolve_shards(), 6)  # 1..12 外无效 → 缺省
        # shard8 实测驱动排队劣化（88s vs 69s）→ 有效覆盖也钳到实测上限 6
        self.assertEqual(NvttEncoder({"gpu_workers": 8}).resolve_shards(), 6)
        self.assertEqual(NvttEncoder({"gpu_workers": 12}).resolve_shards(), 6)

    def test_chunking_splits_by_fixed_size(self):
        from backend.managers.texture_encoders import _chunks

        self.assertEqual(_chunks(list(range(5)), 2), [[0, 1], [2, 3], [4]])
        self.assertEqual(_chunks(list(range(4)), 2), [[0, 1], [2, 3]])
        self.assertEqual(_chunks([], 2), [])
        self.assertEqual(_chunks([1], 0), [[1]])   # 非法 size 退化为 1，不产生空块

    def test_argv_chunks_respects_command_length_limit(self):
        from backend.managers.texture_encoders import TEXCONV_MAX_COMMAND_CHARS, _argv_chunks

        base = ["texconv.exe", "-nologo", "-y", "-o", r"E:\M\Textures"]
        args = [rf"E:\M\Textures\file_{index:06d}.png" for index in range(1500)]
        chunks = _argv_chunks(base, args)
        self.assertGreater(len(chunks), 1)
        flattened = [arg for chunk in chunks for arg in chunk]
        self.assertEqual(flattened, args)                       # 保序不丢
        for chunk in chunks:
            total = sum(len(part) + 3 for part in base + chunk)
            self.assertLessEqual(total, TEXCONV_MAX_COMMAND_CHARS)
        # 单参数超限独占一块，不抛错（交 texconv 自己报路径问题）
        long_arg = "E:\\" + "长" * 30000
        self.assertEqual(_argv_chunks(base, [long_arg]), [[long_arg]])


class TestPendingEntries(unittest.TestCase):
    """增量判断 = todds ``-on`` 语义（输出存在且不旧于源才跳过）。"""

    @staticmethod
    def _entry_pair(tmp: Path, *, name: str, output_older: bool) -> dict[str, Any]:
        source = tmp / f"{name}.png"
        output = tmp / f"{name}.dds"
        if output_older:
            output.write_bytes(b"DDS-old")
            time.sleep(0.02)
            source.write_bytes(b"\x89PNG-new")
        else:
            source.write_bytes(b"\x89PNG-new")
            time.sleep(0.02)
            output.write_bytes(b"DDS-new")
        return {
            "source_path": str(source),
            "output_path": str(output),
            "has_alpha": True,
            "width": 64,
            "height": 64,
        }

    def test_missing_output_is_pending(self):
        entry = {
            "source_path": r"E:/M/Textures/missing.png",
            "output_path": r"E:/M/Textures/missing.dds",
            "has_alpha": True,
            "width": 64,
            "height": 64,
        }
        self.assertEqual(_pending_entries([entry], overwrite_existing=False), [entry])

    def test_output_newer_than_source_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self._entry_pair(Path(tmp), name="fresh", output_older=False)
            self.assertEqual(_pending_entries([entry], overwrite_existing=False), [])

    def test_output_older_than_source_is_pending(self):
        # mod 更新场景：源比输出新 → 必须重编，不能把陈旧 DDS 当有效产物
        with tempfile.TemporaryDirectory() as tmp:
            entry = self._entry_pair(Path(tmp), name="stale", output_older=True)
            self.assertEqual(_pending_entries([entry], overwrite_existing=False), [entry])

    def test_overwrite_makes_everything_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self._entry_pair(Path(tmp), name="fresh", output_older=False)
            self.assertEqual(_pending_entries([entry], overwrite_existing=True), [entry])

    def test_unreadable_mtime_falls_back_to_pending(self):
        # 源/输出 mtime 不可得 → 宁可重编不留旧
        entry = {
            "source_path": r"E:/nonexistent/missing.png",
            "output_path": r"E:/nonexistent/missing.dds",
            "has_alpha": True,
            "width": 64,
            "height": 64,
        }
        self.assertEqual(_pending_entries([entry], overwrite_existing=False), [entry])


class TestTexconvCeil4Round(unittest.TestCase):
    """非 4 倍数源必须 ceil4 取整后编码（奇数宽 BC 会被 RimWorld 拒载）。"""

    def test_non_multiple_of_four_sources_are_ceil4_rounded(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "odd.png"
            Image.new("RGBA", (1270, 1632), (255, 0, 0, 255)).save(source)
            entry = _entry(source, width=1270, height=1632)
            seen: dict[str, Any] = {}

            def fake_run(command, cancel_event, timeout_seconds=None, *, tool_name=None, output_callback=None):
                output_flag_index = command.index("-o")
                inputs = list(command[output_flag_index + 2 :])
                seen["inputs"] = inputs
                seen["sizes"] = [Image.open(path).size for path in inputs]

            with mock.patch.object(TexconvEncoder, "resolve_executable", return_value=FAKE_TEXCONV), \
                 mock.patch.object(texture_encoders._ToolProcessRunner, "run_command", side_effect=fake_run):
                TexconvEncoder({"gpu_workers": 0}).encode_batch(
                    threading.Event(),
                    entries=[entry],
                    overwrite_existing=True,
                    scale_percent=None,
                )

            self.assertEqual(seen["sizes"], [(1272, 1632)])   # ceil4(1270) = 1272
            self.assertEqual(Path(seen["inputs"][0]).name, "odd.png")  # stem 保持

    def test_multiple_of_four_sources_skip_the_round_trip(self):
        # 已是 4 倍数 → 不建临时目录、直读源文件
        seen: dict[str, Any] = {}
        entry = _entry(r"E:/M/Textures/fine.png", width=64, height=64)

        def fake_run(command, cancel_event, timeout_seconds=None, *, tool_name=None, output_callback=None):
            seen["argv"] = list(command)

        with mock.patch.object(TexconvEncoder, "resolve_executable", return_value=FAKE_TEXCONV), \
             mock.patch.object(texture_encoders._ToolProcessRunner, "run_command", side_effect=fake_run):
            TexconvEncoder({"gpu_workers": 0}).encode_batch(
                threading.Event(),
                entries=[entry],
                overwrite_existing=True,
                scale_percent=None,
            )
        self.assertIn(r"E:/M/Textures/fine.png", seen["argv"])


class TestNvttBatchLines(unittest.TestCase):
    """batch 行路径必须带引号（空格路径不加引号整份解析失败）。"""

    @staticmethod
    def _run_nvtt(entry: dict[str, Any], seen: list[str], *, on_unit_done: Any = None) -> None:
        def fake_run(command, cancel_event, timeout_seconds=None, *, tool_name=None, output_callback=None):
            batch = command[command.index("--batch") + 1]
            seen.append(Path(batch).read_text(encoding="utf-8"))

        with mock.patch.object(NvttEncoder, "resolve_executable", return_value=Path(r"C:\n\nvtt_export.exe")), \
             mock.patch.object(texture_encoders._ToolProcessRunner, "run_command", side_effect=fake_run):
            NvttEncoder({"gpu_workers": 6}).encode_batch(
                threading.Event(),
                entries=[entry],
                overwrite_existing=True,
                scale_percent=None,
                on_unit_done=on_unit_done,
            )

    def test_batch_lines_quote_paths_with_spaces(self):
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "Space Mod" / "Textures"
            src_dir.mkdir(parents=True)
            src = src_dir / "a.png"
            src.write_bytes(b"\x89PNG")
            entry = {
                "source_path": str(src),
                "has_alpha": True,
                "width": 64,
                "height": 64,
                "output_path": str(root / "Space Mod" / "a.dds"),
            }
            self._run_nvtt(entry, seen)

        self.assertEqual(len(seen), 1)
        line = seen[0].strip()
        self.assertIn('--save-flip-y -o "', line)
        self.assertIn('" "', line)
        self.assertEqual(line.count('"'), 4)  # -o 与 input 各一对引号

    def test_batch_lines_use_forward_slashes(self):
        """batch 行里反斜杠是转义符（`E:\\G` 报 unrecognized escape
        sequence、`\\a` 被吃掉，三组对照复现）→ 行内路径必须是正斜杠形式。"""
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "Mods" / "Textures"
            src_dir.mkdir(parents=True)
            src = src_dir / "a.png"
            src.write_bytes(b"\x89PNG")
            entry = {
                "source_path": str(src),
                "has_alpha": True,
                "width": 64,
                "height": 64,
                "output_path": str(root / "Mods" / "a.dds"),
            }
            self._run_nvtt(entry, seen)

        self.assertEqual(len(seen), 1)
        line = seen[0].strip()
        self.assertNotIn("\\", line)  # 行内不得残留反斜杠（Windows 源路径会被 as_posix 归一）

    def test_batch_targets_source_beside_dds_even_when_output_path_is_zstd(self):
        """output_path 在 ZSTD 模式是 .dds.zstd 产物投影；``-o`` 必须仍指向源旁的
        .dds——直接写投影路径会把未压缩 DDS 存成 .zstd 文件，且管理器随后找不到
        可压缩的临时 DDS，整批必坏（回归：NVTT×ZSTD）。"""
        seen: list[str] = []
        done: list[list[dict[str, Any]]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "Mods" / "Textures" / "a.png"
            src.parent.mkdir(parents=True)
            src.write_bytes(b"\x89PNG")
            entry = {
                "source_path": str(src),
                "has_alpha": True,
                "width": 64,
                "height": 64,
                "output_path": str(src.with_suffix(".dds.zstd")),
            }
            self._run_nvtt(entry, seen, on_unit_done=done.append)

            line = seen[0].strip()
            target = line.split('-o "')[1].split('"')[0]
            self.assertEqual(Path(target), src.with_suffix(".dds"))
            self.assertNotIn(".zstd", target)
        self.assertEqual(len(done), 1)  # 分块成功后仍要交单元回调


class TestToddsEncoder(unittest.TestCase):
    """todds 无单元概念：单元回调退化为整批一次。"""

    def test_unit_done_fires_once_for_whole_batch(self):
        done: list[list[dict[str, Any]]] = []
        entries = [
            {"source_path": r"E:\M\Textures\a.png"},
            {"source_path": r"E:\M\Textures\b.png"},
        ]
        with mock.patch.object(ToddsEncoder, "resolve_executable", return_value=Path(r"C:\t\todds.exe")), \
             mock.patch.object(texture_encoders._ToolProcessRunner, "run_command"):
            ToddsEncoder({"texture_tools_path": r"C:\t"}).encode_batch(
                threading.Event(),
                entries=entries,
                overwrite_existing=False,
                scale_percent=None,
                on_unit_done=done.append,
            )
        self.assertEqual(len(done), 1)
        self.assertEqual(len(done[0]), 2)


class TestProgressCounter(unittest.TestCase):
    """验证：``Progress: done/total`` 行协议，单调且不超 total。"""

    def test_emits_progress_lines(self):
        lines: list[str] = []
        counter = _ProgressCounter(3, 0, lines.append)
        counter.advance()
        counter.advance(2)
        self.assertEqual(lines, ["Progress: 1/3", "Progress: 3/3"])

    def test_never_exceeds_total_and_stays_monotonic(self):
        lines: list[str] = []
        counter = _ProgressCounter(2, 0, lines.append)
        for _ in range(10):
            counter.advance()
        done = [int(line.split(" ")[1].split("/")[0]) for line in lines]
        total = [int(line.split("/")[1]) for line in lines]
        self.assertEqual(lines[-1], "Progress: 2/2")
        self.assertEqual(done, sorted(done))
        self.assertTrue(all(value <= limit for value, limit in zip(done, total)))

    def test_done_base_is_clamped_and_total_has_floor(self):
        lines: list[str] = []
        counter = _ProgressCounter(0, 5, lines.append)  # total=0 → 1；done_base 超界 → 夹到 1
        counter.advance()
        self.assertEqual(lines, ["Progress: 1/1"])
        self.assertEqual(counter._done, 1)

    def test_counts_only_writing_prefix_lines(self):
        lines: list[str] = []
        counter = _ProgressCounter(4, 0, lines.append)
        counter.on_process_line("writing /tmp/a.dds")
        counter.on_process_line("  Writing /tmp/b.dds")  # 允许前导空白 + 大小写不敏感
        counter.on_process_line("wrote /tmp/c.dds")  # 非 writing 前缀
        counter.on_process_line("")
        counter.on_process_line(None)
        self.assertEqual(lines, ["Progress: 1/4", "Progress: 2/4"])

    def test_without_callback_no_emission_and_no_state_change(self):
        counter = _ProgressCounter(3, 0, None)
        counter.advance()
        counter.on_process_line("writing x")
        self.assertEqual(counter._done, 0)


if __name__ == "__main__":
    unittest.main()
