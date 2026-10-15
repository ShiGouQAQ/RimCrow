# backend/managers/texture_encoders.py
"""贴图编码器叶子层：进程封装、todds/texconv 双编码器、工厂与按机器容量选择编码器、并发数的参数。

本模块位于 ``mgr_texture_opt`` 之下（依赖方向：manager → 本模块，单向无环），
不 import 任何 manager / api / database——编码器只消费扫描产物（entries）与
options，对上层暴露四件事：

- 编码器协议：``encode_batch(cancel_event, *, entries, overwrite_existing,
  scale_percent, output_callback, on_unit_done=None)``。``entries`` 是扫描管线
  产物（每条含 ``source_path`` / ``has_alpha`` / ``width`` / ``height`` /
  ``output_path``），todds 只取 source_path，texconv 用 has_alpha 分流、
  width/height 算 mip 级数，**不重读 PNG 头**（头信息在扫描阶段已读取，
  编码阶段不重复读）。
- 单元完成回调：``on_unit_done(entries)`` 在一批条目的 DDS **全部落盘**后由
  编码器线程调用（texconv = 一个 目录×路由×mip 单元、NVTT = 一个分块、
  todds 无单元概念 = 整批一次）。管理器的 ZSTD 管线用它把「压缩为 .dds.zstd」
  与剩余单元的编码重叠起来；传 None 表示调用方不需要。
- **写盘位置不变式**：三个编码器都把 DDS 写在源图旁边（``source.with_suffix('.dds')``），
  ``output_path`` 只是扫描层的产物投影（ZSTD 模式下是 .dds.zstd），编码器一律
  不直接用它当写盘目标——ZSTD 由管理器统一压缩，编码器只产出 DDS。
- 进度协议：编码器经 ``output_callback`` 发 ``Progress: <done>/<total>``
  行——todds 原生输出该形态，TexconvEncoder 聚合各分片 ``writing`` 行合成；
  管理器端 ``TEXTURE_PROGRESS_PATTERN`` 消费端不变。
- 可恢复错误：``ToolRunError``（kind=failed/timeout + log_path）表示
  「工具真实跑过但本批失败」，管理器按类型判定进入既有逐条重试管线；启动失败等
  其余 ``TextureOptError`` 不重试。错误消息文本与既有字符串形态逐字一致。
- 按机器容量选择编码器与并发数：``resolve_worker_plan``（CPU 分片 clamp(核数−2, 2, 6)，
  GPU 分片默认 3、``probe_gpu`` 实测 DirectCompute 可用性后可降为 0）与
  ``resolve_auto_backend``（texconv 可用 → texconv，否则 todds）。

texconv 混合命令来自基准实测（``docs/texture-encoder-benchmark.md``，
两会话 n=5 配对轮）：BC7 走 DirectCompute GPU、BC1 走 CPU（``-nogpu``），生产化
差异仅原地输出（按 目录×路由×mip 级数 分组调用）与 mip 链（``-m N`` 是精确级数
语义，全链 = ``floor(log2(max(w,h)))+1``；
⚠ ``-m 0`` 在 texconv 里是「生成全链」而非「不生成」，见 ``resolve_mip_levels``）。

texconv 文件操作数一律 argv 直传（按 ``TEXCONV_MAX_COMMAND_CHARS`` 分块），不写
``-flist``：texconv 2026.5.8.1 的 ``-flist`` 按非 UTF-8 解码，非 ASCII 路径整体
乱码（实测；基准语料全 ASCII 故未暴露）。todds/NVTT 的 UTF-8 列表
文件行为正确，不受此限制。分块 kind=failed 时逐文件隔离，坏文件不放大成整批重试。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Callable

from backend.settings import TOOLS_DIR
from backend.utils.logger import logger

__all__ = [
    "MAX_TEXTURE_WORKERS",
    "MIN_TEXTURE_WORKERS",
    "NvttEncoder",
    "TexconvEncoder",
    "TextureOptCancelled",
    "TextureOptError",
    "ToddsEncoder",
    "ToolRunError",
    "TEXCONV_MAX_COMMAND_CHARS",
    "_Job",
    "build_invocation_units",
    "create_encoder",
    "prepare_unit_inputs",
    "probe_gpu",
    "resolve_auto_backend",
    "resolve_mip_levels",
    "resolve_texture_tools_path",
    "resolve_worker_plan",
    "run_jobs",
    "texconv_info",
]

TEXTURE_PROGRESS_EMIT_PATTERN = "Progress: {done}/{total}"

# texconv argv 直传的分块上限：Windows CreateProcessW 命令行整体上限 32767 字符，
# 留出 exe 路径 + 旗标 + 引号转义余量（按 UTF-16 计，中文路径每字符占 1 位）。
TEXCONV_MAX_COMMAND_CHARS = 24000

# 显式覆盖值（gpu_workers/cpu_workers）的合法区间：工具层校验、_build_options 归一化、
# _clamp_workers 三处共用，禁止各自硬编码（改区间只改这里）。
MIN_TEXTURE_WORKERS = 1
MAX_TEXTURE_WORKERS = 12


class TextureOptError(RuntimeError):
    pass


class TextureOptCancelled(TextureOptError):
    pass


class ToolRunError(TextureOptError):
    """外部编码进程已启动但执行失败/超时（可恢复：进入逐条重试管线）。

    ``kind``: "failed"（退出码非 0）| "timeout"（连续无输出超时）；
    ``log_path`` 是保留下来的进程日志路径。消息文本与既有字符串形态逐字一致；
    管理器按 ``isinstance(exc, ToolRunError)`` 分流，不做消息前缀字符串匹配。
    """

    def __init__(self, message: str, *, kind: str, log_path: str = "") -> None:
        super().__init__(message)
        self.kind = str(kind or "failed")
        self.log_path = str(log_path or "")


def resolve_texture_tools_path(options: dict[str, Any]) -> Path:
    raw_texture_tools_path = str(options.get("texture_tools_path") or "").strip()
    if raw_texture_tools_path:
        candidate = Path(raw_texture_tools_path)
        return candidate.parent if candidate.suffix.lower() == ".exe" else candidate
    return TOOLS_DIR / "texture_tools"


def _windows_silent_process_kwargs() -> dict[str, Any]:
    if platform.system() != "Windows":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW),
    }


class _ToolProcessRunner:
    @staticmethod
    def run_command(
        command: list[str],
        cancel_event: threading.Event,
        timeout_seconds: float | None = None,
        *,
        tool_name: str = "todds",
        output_callback: Callable[[str], None] | None = None,
    ) -> None:
        creationflags = 0
        startupinfo = None
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        log_path: Path | None = None
        process = None
        last_activity_at = time.monotonic()
        was_cancelled = False
        timeout_detail = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, suffix=".log") as log_file:
                log_path = Path(log_file.name)
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                )
                reader_error: Exception | None = None

                def consume_output() -> None:
                    nonlocal last_activity_at, reader_error
                    if not process or not process.stdout: return
                    try:
                        for line in process.stdout:
                            last_activity_at = time.monotonic()
                            log_file.write(line)
                            if output_callback:
                                output_callback(line.rstrip("\r\n"))
                    except Exception as exc:
                        reader_error = exc

                reader = threading.Thread(target=consume_output, daemon=True, name=f"{tool_name}-output")
                reader.start()
                while True:
                    if cancel_event.is_set():
                        _ToolProcessRunner.terminate_process(process)
                        was_cancelled = True
                        break
                    if process.poll() is not None:
                        break
                    if timeout_seconds and (time.monotonic() - last_activity_at) > float(timeout_seconds):
                        _ToolProcessRunner.terminate_process(process)
                        timeout_detail = "__timeout__"
                        break
                    if reader_error is not None:
                        _ToolProcessRunner.terminate_process(process)
                        raise TextureOptError(f"{tool_name} 输出读取失败: {reader_error}")
                    time.sleep(0.1)
                reader.join(timeout=2)
        finally:
            if process and process.poll() is None:
                _ToolProcessRunner.terminate_process(process)

        if not process:
            raise TextureOptError(f"{tool_name} 进程未能启动")

        detail = _ToolProcessRunner.read_process_log(log_path)
        if was_cancelled:
            _ToolProcessRunner.cleanup_log_file(log_path)
            raise TextureOptCancelled("贴图优化任务已取消")
        if timeout_detail:
            preserved_log = _ToolProcessRunner.preserve_process_log(log_path, tool_name)
            raise ToolRunError(
                f"{tool_name} 执行超时（连续 {int(timeout_seconds or 0)} 秒无输出）: {detail or '无输出'}"
                + (f" [日志: {preserved_log}]" if preserved_log else ""),
                kind="timeout",
                log_path=preserved_log or "",
            )
        if process.returncode != 0:
            preserved_log = _ToolProcessRunner.preserve_process_log(log_path, tool_name)
            raise ToolRunError(
                f"{tool_name} 执行失败: {detail or '未知错误'}"
                + (f" [日志: {preserved_log}]" if preserved_log else ""),
                kind="failed",
                log_path=preserved_log or "",
            )
        _ToolProcessRunner.cleanup_log_file(log_path)

    @staticmethod
    def terminate_process(process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        except OSError:
            pass

    @staticmethod
    def read_process_log(log_path: Path | None, limit: int = 2000) -> str:
        if not log_path or not log_path.exists(): return ""
        try:
            with log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max(1, int(limit))), os.SEEK_SET)
                data = handle.read()
        except OSError: return ""
        return data.decode("utf-8", errors="replace").strip() if data else ""

    @staticmethod
    def cleanup_log_file(log_path: Path | None) -> None:
        if not log_path: return
        try: log_path.unlink()
        except OSError: pass

    @staticmethod
    def preserve_process_log(log_path: Path | None, tool_name: str) -> str:
        if not log_path or not log_path.exists(): return ""
        preserved_path = log_path.with_name(f"{tool_name}_{log_path.stem}.log")
        try:
            if preserved_path.exists():
                preserved_path.unlink()
            log_path.replace(preserved_path)
            return str(preserved_path)
        except OSError:
            return str(log_path)


class ToddsEncoder:
    tool_name = "todds"
    OPAQUE_FORMAT = "BC1"
    ALPHA_FORMAT = "BC7"

    def __init__(self, options: dict[str, Any]):
        self.options = options

    def resolve_executable(self) -> Path:
        if platform.system() != "Windows":
            raise TextureOptError("todds 后端当前仅支持 Windows 自动集成。")
        texture_tools_path = resolve_texture_tools_path(self.options)
        candidates = [
            str(self.options.get("todds_path") or "").strip(),
            str(texture_tools_path / "todds.exe"),
            str(TOOLS_DIR / "texture_tools" / "todds.exe"),
            shutil.which("todds.exe") or "",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists(): return Path(candidate)
        raise TextureOptError("未找到 todds.exe。请在贴图优化中心下载 todds。")

    def encode_batch(
        self,
        cancel_event: threading.Event,
        *,
        entries: list[dict[str, Any]],
        overwrite_existing: bool,
        scale_percent: int | None,
        output_callback: Callable[[str], None] | None = None,
        on_unit_done: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        normalized_sources = [
            str(Path(str(entry.get("source_path") or "")))
            for entry in entries
            if str(entry.get("source_path") or "")
        ]
        if not normalized_sources: return

        command = [
            str(self.resolve_executable()),
            "-f",
            self.OPAQUE_FORMAT,
            "-af",
            self.ALPHA_FORMAT,
            "-o" if overwrite_existing else "-on",
            "-vf",
            "-r",
            "Textures",
            "-t",
            "-p",
        ]
        if not bool(self.options.get("generate_mipmaps", True)):
            command.append("-nm")
        if scale_percent is None:
            command.append("-fs")
        else:
            command.extend(["-sc", str(int(scale_percent))])

        timeout_seconds = max(300, int(self.options.get("encode_batch_timeout_seconds", 600) or 600))
        file_list = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, suffix=".txt") as handle:
                file_list = Path(handle.name)
                for source_path in normalized_sources:
                    handle.write(f"{source_path}\n")
            command.append(str(file_list))
            run_kwargs = {
                "timeout_seconds": timeout_seconds,
                "tool_name": "todds",
            }
            if output_callback is not None:
                run_kwargs["output_callback"] = output_callback
            _ToolProcessRunner.run_command(
                command,
                cancel_event,
                **run_kwargs,
            )
            # todds 单进程跑完整批、无单元概念：进程成功即整批 DDS 落盘，
            # 单元回调退化为整批一次（ZSTD 管线对 todds 无重叠收益，语义保持一致）。
            if on_unit_done is not None:
                on_unit_done(list(entries))
        finally:
            if file_list and file_list.exists():
                try:
                    file_list.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# texconv 混合编码器
# ---------------------------------------------------------------------------

_TEXCONV_VERSION_PATTERN = re.compile(r"Version\s+(\S+)")


def resolve_mip_levels(width: int, height: int, *, generate_mipmaps: bool = True) -> int:
    """texconv ``-m N`` 的级数取值。

    官方语义（DirectXTex wiki：``-m`` defaults to **0 which is generate all mipmaps**、
    ``Use -m 1 to remove mipmaps``）+ 本机实测（读 DDS 头 mipMapCount）：

    ============ ==========================================================
    输入          返回 ``-m``
    ============ ==========================================================
    不生成 mip    ``1``（单 mip）——**不是 0**，``-m 0`` 会生成全链
    生成 + 已知   ``max(w, h).bit_length()`` = ``floor(log2(max)) + 1`` = 全链
                  级数（64px→7、100px→7、256px→9；与 DirectXTex
                  ``CalculateMipLevels``「反复对半至 1×1」同义）
    生成 + 未知   ``0``（交给 texconv 自身缺省 = 全链）
    ============ ==========================================================
    """
    if not generate_mipmaps:
        return 1
    longest = max(int(width or 0), int(height or 0))
    return longest.bit_length() if longest > 0 else 0


@dataclass
class _TexconvUnit:
    """一次编码进程调用的调度单元：单目录 × 单路由 × 单 mip 级数。"""

    directory: str
    route: str  # "alpha"（BC7）| "opaque"（BC1）
    mip_levels: int  # 见 resolve_mip_levels：1=不生成，0=交给 texconv 缺省全链
    use_gpu: bool
    entries: list[dict[str, Any]] = field(default_factory=list)
    # 预缩放后的输入目录（文件名保持原 stem，使 texconv `-o <源目录>` 能零搬运落到
    # output_path）；None = 未缩放，直接读 source_path。只由 run_unit 写入并在 finally 清空。
    input_dir: Path | None = None
    # 非 4 倍数源的 ceil4 取整副本（源文件名 → 临时路径），优先级高于 input_dir。
    input_overrides: dict[str, str] = field(default_factory=dict)


class _ProgressCounter:
    """跨分片聚合的 ``Progress: done/total`` 行发射器（消费端既有节流不变）。"""

    def __init__(self, total: int, done_base: int, output_callback: Callable[[str], None] | None):
        self._lock = threading.Lock()
        self._total = max(1, int(total))
        self._done = max(0, min(int(done_base), self._total))
        self._callback = output_callback

    def advance(self, count: int = 1) -> None:
        if self._callback is None:
            return
        with self._lock:
            self._done = min(self._total, self._done + max(1, int(count)))
            current = self._done
        self._callback(TEXTURE_PROGRESS_EMIT_PATTERN.format(done=current, total=self._total))

    def on_process_line(self, line: str) -> None:
        """分片进程输出行入口：按 texconv 的 ``writing <path>`` 行计数（只认 ASCII 前缀）。"""
        if str(line or "").lstrip().lower().startswith("writing"):
            self.advance()


def _ceil4(value: int) -> int:
    """BC 块要求 4 的倍数（与 todds `-fs` 的 next-multiple-of-4 同语义），最小 4。"""
    return max(4, -(-int(value) // 4) * 4)


def _unit_input_path(unit: "_TexconvUnit", entry: dict[str, Any]) -> str:
    """分片实际要读的文件：ceil4 取整副本 → 预缩放目录 → 源文件。"""
    source = str(entry.get("source_path") or "")
    name = Path(source).name
    override = unit.input_overrides.get(name)
    if override:
        return override
    if unit.input_dir is not None and (unit.input_dir / name).exists():
        return str(unit.input_dir / name)
    return source


def prepare_unit_inputs(
    unit: "_TexconvUnit", *, scale_percent: int | None
) -> tuple[Path | None, dict[str, str]]:
    """预缩放与 ceil4 取整共用的输入准备：只重写需要变化的文件。

    - ``scale_percent`` 非 None → 整单元按 ``ceil4(round(dim×pct/100))`` 重写
      （目标天然是 4 的倍数）；
    - ``scale_percent`` 为 None → 只把**非 4 倍数源** ceil4 取整（texconv 保留
      1270×1632 奇数逻辑宽、NVTT 头里逻辑宽同为奇数，RimWorld 1.6 对非 4 倍数 BC 一律
      回退读 PNG → 转码静默失效；todds ``-fs`` 的 1272 取整无此问题）；
    - 两者都不需要 → ``(None, {})``，所有文件直读源路径，不建临时目录。

    返回 (临时目录 | None, 源文件名 → 临时路径 映射)；临时目录由调用方负责清理。
    """
    from PIL import Image

    targets: dict[Path, tuple[int, int]] = {}
    for entry in unit.entries:
        source = Path(str(entry.get("source_path") or ""))
        width = int(entry.get("width") or 0)
        height = int(entry.get("height") or 0)
        if width <= 0 or height <= 0:
            continue
        if scale_percent is not None:
            targets[source] = (
                _ceil4(round(width * scale_percent / 100)),
                _ceil4(round(height * scale_percent / 100)),
            )
        elif width % 4 or height % 4:
            targets[source] = (_ceil4(width), _ceil4(height))
    overrides: dict[str, str] = {}
    if not targets:
        return None, overrides
    input_dir = Path(tempfile.mkdtemp(prefix="tex-prescale-"))
    try:
        for source, target in targets.items():
            with Image.open(source) as image:
                image.resize(target, Image.Resampling.LANCZOS).save(input_dir / source.name, format="PNG")
            overrides[source.name] = str(input_dir / source.name)
    except Exception:
        shutil.rmtree(input_dir, ignore_errors=True)
        raise
    return input_dir, overrides


class _Job:
    """调度层的最小作业：一段可独立执行的工作。

    只描述「做什么」，不描述「怎么并发」——`run` 是编码器私有细节（预缩放 + 拼命令 + 起进程），
    调度器（`run_jobs`）只负责并发、取消、失败收敛与进度推进。
    """

    __slots__ = ("label", "run", "advance")

    def __init__(self, label: str, run: Callable[[], None], *, advance: int | None = None) -> None:
        self.label = label
        self.run = run
        # 完成后推进的进度条数；None = 进度由进程输出行（ProgressCounter.on_process_line）推进。
        self.advance = advance


def run_jobs(
    groups: list[tuple[str, int, list[_Job]]],
    *,
    cancel_event: threading.Event,
    progress: "_ProgressCounter | None" = None,
) -> None:
    """编码层调度器：把若干组作业交给各自的一组 worker 并发执行。

    分层约定：

    - **本函数只管调度**：一组一池、提交前查取消、收敛**首个失败**（其余作业照常跑完）、
      `TextureOptCancelled` 立即上抛、`finally` 保证池关闭、成功后按 `job.advance` 推进度。
    - **不关心作业是什么**：不碰路径、不拼命令、不识别格式——换调度策略（线程池 / 串行 /
      进程池 / 限流）只改这一处，两个编码器与既有测试都不动。
    - **失败语义**：异常原样上抛，由上层按 `ToolRunError` / `TextureOptCancelled` 分流，
      调度层不吞错、不包装。

    ``groups`` 为 ``(名字前缀, 并发数, 作业列表)``：texconv 传 gpu/cpu 两组（3 / 6），
    NVTT 传一组（分片数），互不阻塞地同时起跑。
    """
    failures: list[Exception] = []
    pools: list[ThreadPoolExecutor] = []
    futures = []
    try:
        for prefix, workers, jobs in groups:
            if not jobs:
                continue
            pool = ThreadPoolExecutor(max_workers=max(1, int(workers)), thread_name_prefix=prefix)
            pools.append(pool)
            for index, job in enumerate(jobs):
                futures.append(pool.submit(_execute_job, job, index, cancel_event, progress))
        for future in futures:
            try:
                future.result()
            except TextureOptCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - 收敛首个失败，其余分片照常跑完
                failures.append(exc)
    finally:
        for pool in pools:
            pool.shutdown(wait=True)
    if failures:
        raise failures[0]


def _execute_job(
    job: _Job,
    index: int,
    cancel_event: threading.Event,
    progress: "_ProgressCounter | None",
) -> None:
    if cancel_event.is_set():
        raise TextureOptCancelled("贴图优化任务已取消")
    job.run()
    if progress is not None and job.advance:
        progress.advance(int(job.advance))


class TexconvEncoder:
    """texconv 混合管线：alpha→BC7（DirectCompute GPU）、opaque→BC1（CPU）。

    分片并发数默认来自 ``resolve_worker_plan``（基准最优：gpu=3 / cpu=clamp(核−2,2,6)），
    可被 options 的 ``gpu_workers`` / ``cpu_workers``（``MIN_TEXTURE_WORKERS``..
    ``MAX_TEXTURE_WORKERS``）覆盖；GPU 可用性未显式
    给出时按 exe+mtime 缓存的 ``probe_gpu`` 实测决定（失败 → alpha 全走 CPU BC7）。
    GPU 分片运行期失败的处理是**熔断式**的：批内连续 GPU 单元失败达到
    ``GPU_UNIT_FAILURE_BREAKER`` 即熔断（首个失败先降级为 CPU BC7 重跑一次，容忍
    偶发 TDR / 坏图；GPU 单元成功即清零重新计数）——置位熔断事件后剩余 GPU 分片
    在入口快速短路、不再启动 texconv 进程，并上抛换档建议。纯 CPU 的 texconv BC7
    明显慢于 todds，静默降级会让用户在不知情时跌进性能悬崖，快速失效才是对的。
    熔断同时作废 GPU 探测缓存（故障是环境态，exe+mtime 未变但结论已失效）。
    """

    tool_name = "texconv"
    OPAQUE_FORMAT = "BC1_UNORM"
    ALPHA_FORMAT = "BC7_UNORM"
    DEFAULT_GPU_WORKERS = 3
    MIN_CPU_WORKERS = 2
    MAX_CPU_WORKERS = 6
    GPU_UNIT_FAILURE_BREAKER = 2

    def __init__(self, options: dict[str, Any]):
        self.options = options
        # 分片级 GPU 失败计数与熔断事件（以「批」为周期，见 _run_units 入口清零）：
        # unit 在线程池并发跑，锁只护计数
        self._gpu_unit_failures = 0
        self._gpu_failure_lock = threading.Lock()
        self._gpu_breaker = threading.Event()

    def resolve_executable(self) -> Path:
        if platform.system() != "Windows":
            raise TextureOptError("texconv 后端当前仅支持 Windows。")
        texture_tools_path = resolve_texture_tools_path(self.options)
        candidates = [
            str(self.options.get("texconv_path") or "").strip(),
            str(texture_tools_path / "texconv.exe"),
            str(TOOLS_DIR / "texture_tools" / "texconv.exe"),
            shutil.which("texconv.exe") or "",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists(): return Path(candidate)
        raise TextureOptError("未找到 texconv.exe。可用 download_texture_encoder(backend='texconv') 自动下载。")

    def encode_batch(
        self,
        cancel_event: threading.Event,
        *,
        entries: list[dict[str, Any]],
        overwrite_existing: bool,
        scale_percent: int | None,
        output_callback: Callable[[str], None] | None = None,
        on_unit_done: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        # 缩放不把 texconv 排除在外——非 todds 编码器由包装层先做 PIL LANCZOS 预缩放，
        # 编码器只负责编码，缩放由上游预处理完成。
        usable = [entry for entry in entries if str(entry.get("source_path") or "")]
        if not usable:
            return
        executable = self.resolve_executable()
        gpu_workers, cpu_workers = self._resolve_workers(executable)

        # 增量语义与 todds "-on" 对齐（_pending_entries）：输出存在且不旧于源 → 跳过；
        # 这些条目由管理器 verify 阶段按「输出已存在」计入成功。
        pending = _pending_entries(usable, overwrite_existing=overwrite_existing)
        done_base = len(usable) - len(pending)
        if not pending:
            return

        units = self._build_invocation_units(
            pending, gpu_workers=gpu_workers, scale_percent=scale_percent
        )
        self._run_units(
            executable,
            units,
            cancel_event,
            gpu_workers=gpu_workers,
            cpu_workers=cpu_workers,
            scale_percent=scale_percent,
            total=len(usable),
            done_base=done_base,
            output_callback=output_callback,
            on_unit_done=on_unit_done,
        )

    def _resolve_workers(self, executable: Path) -> tuple[int, int]:
        gpu_available: bool | None = None
        if _clamp_workers(self.options.get("gpu_workers")) == 0:
            gpu_available = probe_gpu(executable)
        plan = resolve_worker_plan(self.options, gpu_available=gpu_available)
        return int(plan["gpu_workers"]), int(plan["cpu_workers"])

    def _build_invocation_units(
        self, entries: list[dict[str, Any]], *, gpu_workers: int, scale_percent: int | None = None
    ) -> list[_TexconvUnit]:
        # 分组规则与 NVTT 共用（build_invocation_units），本方法只补上本后端的 mip 开关，
        # 免得两处各写一套分组而漂移。
        return build_invocation_units(
            entries,
            gpu_workers=gpu_workers,
            generate_mipmaps=bool(self.options.get("generate_mipmaps", True)),
            scale_percent=scale_percent,
        )

    def _run_units(
        self,
        executable: Path,
        units: list[_TexconvUnit],
        cancel_event: threading.Event,
        *,
        gpu_workers: int,
        cpu_workers: int,
        scale_percent: int | None,
        total: int,
        done_base: int,
        output_callback: Callable[[str], None] | None,
        on_unit_done: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        # 300s 下限防的是「配置写错把生产批提前砍掉」；基准等内部小负载调用
        # 可经 encode_batch_timeout_floor 显式放宽下限
        timeout_floor = max(1, int(self.options.get("encode_batch_timeout_floor", 300) or 300))
        timeout_seconds = max(timeout_floor, int(self.options.get("encode_batch_timeout_seconds", 480) or 480))
        progress = _ProgressCounter(total, done_base, output_callback)
        # 熔断状态以「批」为周期：批开始时清零计数与事件
        with self._gpu_failure_lock:
            self._gpu_unit_failures = 0
        self._gpu_breaker.clear()

        def make_job(unit: _TexconvUnit, *, use_gpu: bool) -> _Job:
            def run() -> None:
                if use_gpu and self._gpu_breaker.is_set():
                    # 熔断后的快速短路：不再为剩余 GPU 分片启动 texconv 进程
                    raise ToolRunError("GPU 熔断已触发，跳过剩余 GPU 分片", kind="failed")
                gpu_attempt_failed = False
                input_dir: Path | None = None
                try:
                    try:
                        input_dir, overrides = prepare_unit_inputs(unit, scale_percent=scale_percent)
                    except Exception as exc:  # noqa: BLE001 - 单分片输入准备失败 → 交逐条重试管线
                        raise ToolRunError(f"{self.tool_name} 输入准备失败: {exc}", kind="failed") from exc
                    unit.input_dir = input_dir
                    unit.input_overrides = overrides
                    try:
                        self._encode_one(
                            executable, unit, use_gpu=use_gpu,
                            cancel_event=cancel_event, timeout_seconds=timeout_seconds, progress=progress,
                        )
                    except ToolRunError as exc:
                        # GPU 分片失败处理（熔断式）：首个失败降级 CPU BC7 重跑一次
                        # （预缩放阶段的失败不进这里，它在 try 之外抛出后已由 finally 清理并上抛）；
                        # 批内连续失败达到阈值即熔断——置位事件让剩余 GPU 分片在入口短路，
                        # 上抛换档建议。纯 CPU 的 texconv BC7 慢于 todds，静默全转 CPU 是
                        # 用户不知情的性能悬崖。
                        if not use_gpu:
                            raise
                        gpu_attempt_failed = True
                        with self._gpu_failure_lock:
                            self._gpu_unit_failures += 1
                            gpu_failures = self._gpu_unit_failures
                        if gpu_failures >= self.GPU_UNIT_FAILURE_BREAKER:
                            self._gpu_breaker.set()
                            _invalidate_gpu_probe_cache(executable)
                            raise ToolRunError(
                                f"GPU 编码连续失败（本批已有 {gpu_failures} 个 GPU 分片失败），"
                                "已熔断并跳过剩余 GPU 分片：建议改用标准档（todds）或中等档（nvtt），"
                                "或检查显卡驱动后重试",
                                kind="failed",
                            ) from exc
                        logger.warning(
                            "texconv GPU 分片失败，降级 CPU BC7 重跑（本批连续第 %s 次，再次失败将熔断）：dir=%s files=%s error=%s",
                            gpu_failures,
                            unit.directory,
                            len(unit.entries),
                            exc,
                        )
                        self._encode_one(
                            executable, unit, use_gpu=False,
                            cancel_event=cancel_event, timeout_seconds=timeout_seconds, progress=progress,
                        )
                    if use_gpu and not gpu_attempt_failed:
                        # GPU 恢复健康即清零：熔断只针对「批内连续 GPU 单元失败」
                        with self._gpu_failure_lock:
                            self._gpu_unit_failures = 0
                    # 单元 DDS 全部落盘（含降级重跑成功），交给调用方的 ZSTD 管线压缩；
                    # 失败的单元不回调，其条目走调用方的失败/重试路径。
                    if on_unit_done is not None:
                        on_unit_done(list(unit.entries))
                finally:
                    unit.input_dir = None
                    unit.input_overrides = {}
                    if input_dir is not None:
                        shutil.rmtree(input_dir, ignore_errors=True)

            return _Job(f"{unit.route}:{Path(unit.directory).name}", run)

        gpu_jobs = [make_job(unit, use_gpu=True) for unit in units if unit.use_gpu]
        cpu_jobs = [make_job(unit, use_gpu=False) for unit in units if not unit.use_gpu]
        run_jobs(
            [
                ("TexconvGpu", gpu_workers, gpu_jobs),
                ("TexconvCpu", cpu_workers, cpu_jobs),
            ],
            cancel_event=cancel_event,
            progress=progress,
        )

    def _encode_one(
        self,
        executable: Path,
        unit: _TexconvUnit,
        *,
        use_gpu: bool,
        cancel_event: threading.Event,
        timeout_seconds: float,
        progress: _ProgressCounter,
    ) -> None:
        """文件操作数经 argv 直传（按命令行长度分块），不写 ``-flist``。

        texconv 2026.5.8.1 的 ``-flist`` 按非 UTF-8 方式解码文件列表，目录名或
        文件名里任何一个非 ASCII 字符都会乱码（80070003/80070002；生产日志与
        本机复现）；argv 走 CreateProcessW 的 UTF-16 通道无此问题
        （中英混合实测通过）。todds/NVTT 的 UTF-8 列表文件不受此影响。

        分块失败（kind=failed，rc != 0）只说明块里有文件失败——texconv 会为单个
        坏文件置非零退出码。此时逐文件隔离重跑：CPU 路由上单文件仍失败的只记
        日志、交给管理器 verify 按条目标失败；GPU 路由上单文件失败继续上抛，
        让 CPU 降级重跑处理（真 GPU 故障不至于被吞）。不做这层隔离时，
        1 个坏文件会把整批（10000 条）打回重试管线并连带清掉已生成产物
        （生产实测：3.5 小时 0 批完成）。隔离重跑会把已成功文件的
        ``writing`` 行再计一次，进度略虚高，仅影响 UI 展示。
        """
        base_command = self._build_command(executable, unit, use_gpu=use_gpu)
        paths = [_unit_input_path(unit, entry) for entry in unit.entries]
        for chunk in _argv_chunks(base_command, paths):
            try:
                self._run_command_once(base_command + chunk, cancel_event, timeout_seconds, progress)
            except ToolRunError as exc:
                if exc.kind != "failed":
                    raise
                for path in chunk:
                    try:
                        self._run_command_once(base_command + [path], cancel_event, timeout_seconds, progress)
                    except ToolRunError as file_exc:
                        if file_exc.kind != "failed":
                            raise
                        if use_gpu:
                            raise file_exc from exc
                        logger.warning(
                            "texconv 单文件编码失败（已隔离，交管理器按条目标失败）: path=%s error=%s",
                            path,
                            file_exc,
                        )

    def _run_command_once(
        self,
        command: list[str],
        cancel_event: threading.Event,
        timeout_seconds: float,
        progress: _ProgressCounter,
    ) -> None:
        _ToolProcessRunner.run_command(
            command,
            cancel_event,
            timeout_seconds=timeout_seconds,
            tool_name=self.tool_name,
            output_callback=progress.on_process_line,
        )

    def _build_command(self, executable: Path, unit: _TexconvUnit, *, use_gpu: bool) -> list[str]:
        command = [str(executable), "-nologo", "-y", "-vflip", "-m", str(unit.mip_levels)]
        if use_gpu:
            command.extend(["-f", self.ALPHA_FORMAT])
        else:
            cpu_format = self.ALPHA_FORMAT if unit.route == "alpha" else self.OPAQUE_FORMAT
            command.extend(["-nogpu", "-f", cpu_format])
        command.extend(["-o", unit.directory])
        return command


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    """按固定大小切块（NVTT 分块用：块间发进度 + 让空闲超时归零）。"""
    step = max(1, int(size))
    return [items[index:index + step] for index in range(0, len(items), step)]


def _pending_entries(
    usable: list[dict[str, Any]], *, overwrite_existing: bool
) -> list[dict[str, Any]]:
    """增量语义：与 todds ``-on`` 对齐，不只是「输出存在就跳过」。

    - ``overwrite_existing`` → 全部待编码；
    - 输出缺失 → 待编码；
    - 输出存在且 mtime **不旧于**源 → 跳过；源更新（mod 更新场景）→ 重编，否则
      会把陈旧 DDS 当有效产物留下。
    mtime 不可读（OSError）时按「需要编码」处理——宁可重编不可留旧。
    """
    pending: list[dict[str, Any]] = []
    for entry in usable:
        if overwrite_existing:
            pending.append(entry)
            continue
        output = Path(str(entry.get("output_path") or ""))
        source = Path(str(entry.get("source_path") or ""))
        try:
            if output.exists() and output.stat().st_mtime_ns >= source.stat().st_mtime_ns:
                continue
        except OSError:
            pass
        pending.append(entry)
    return pending


def _argv_chunks(
    base_command: list[str],
    args: list[str],
    *,
    limit: int = TEXCONV_MAX_COMMAND_CHARS,
) -> list[list[str]]:
    """把文件操作数按命令行总长度切块（texconv argv 直传用）。

    每参数按 ``len + 3`` 估（一对引号 + 一个分隔空格，转义放大时也够保守）；
    单参数超限（如超长路径）时独占一块，交给 texconv 自己报路径错误。
    """
    base_length = sum(len(part) + 3 for part in base_command)
    chunks: list[list[str]] = []
    current: list[str] = []
    current_length = base_length
    for arg in args:
        arg_length = len(arg) + 3
        if current and current_length + arg_length > limit:
            chunks.append(current)
            current = []
            current_length = base_length
        current.append(arg)
        current_length += arg_length
    if current:
        chunks.append(current)
    return chunks


def build_invocation_units(
    entries: list[dict[str, Any]],
    *,
    gpu_workers: int,
    generate_mipmaps: bool,
    scale_percent: int | None = None,
) -> list[_TexconvUnit]:
    """按 **(目录 × 路由 × mip 级数)** 分组——texconv 与 NVTT 共用的分组规则。

    独立成模块函数而非某编码器的方法，是为了让两个后端各自持有同样的分组真相、
    又不必互相继承（分组是「怎么切」，属于编码层共享语义，不属于任一后端）。

    ``scale_percent`` 非 None 时按**缩放后的目标尺寸**算 mip：分组键必须与真正喂给
    编码器的图像一致——预缩放把 256×256 变 128×128（最大 8 级）后，若仍按原图给
    ``-m 9``，texconv 会报 ``E_INVALIDARG 80070057``（实测复现）。
    mip 级数始终按**逻辑尺寸**算（ceil4 只是 BC 存储补齐，不改 mip 链；非 4 倍数源
    的编码输入取整由 prepare_unit_inputs 负责）。
    """
    groups: dict[tuple[str, str, int], _TexconvUnit] = {}
    for entry in entries:
        # has_alpha 缺失时保守按 alpha 处理：误入 BC7 只是体积代价，
        # 误入 BC1 会丢 alpha（数据损失方向必须保守）。
        has_alpha = bool(entry.get("has_alpha", True))
        route = "alpha" if has_alpha else "opaque"
        width = int(entry.get("width") or 0)
        height = int(entry.get("height") or 0)
        if scale_percent is not None and generate_mipmaps and width > 0 and height > 0:
            target_w = _ceil4(round(width * scale_percent / 100))
            target_h = _ceil4(round(height * scale_percent / 100))
            mip_levels = resolve_mip_levels(target_w, target_h, generate_mipmaps=True)
        else:
            mip_levels = resolve_mip_levels(width, height, generate_mipmaps=generate_mipmaps)
        directory = str(Path(str(entry.get("source_path"))).parent)
        key = (directory, route, mip_levels)
        unit = groups.get(key)
        if unit is None:
            unit = _TexconvUnit(
                directory=directory,
                route=route,
                mip_levels=mip_levels,
                use_gpu=route == "alpha" and gpu_workers > 0,
            )
            groups[key] = unit
        unit.entries.append(entry)
    return list(groups.values())


# ---------------------------------------------------------------------------
# NVTT 编码器（NVIDIA Texture Tools Exporter，中等档）
# ---------------------------------------------------------------------------


class NvttEncoder:
    """中等档：NVTT 混合管线（BC7/BC1 逐行选格式，CUDA `normal` 档）。

    接口约定（全部本机实测）：

    - **成功路径完全静默**（stdout/stderr 零字节）→ 进度由本类按**分块**自行推进，
      不依赖进程输出行；
    - 单次进程启动开销 ~317 ms → 必须 ``--batch`` 聚批，逐文件调用会吃光速度优势；
    - 分片上限 ~6（8 即驱动排队劣化）→ 缺省 6、可被 ``gpu_workers`` 覆盖；
      **不用** ``resolve_worker_plan`` 的 3，那是 texconv 的 GPU 饱和点；
    - 批内静默 + ``_ToolProcessRunner`` 用的是**空闲超时** → 按 ``CHUNK_SIZE`` 切块，
      每块跑完发一次进度，也把空闲计时器归零；
    - **不自建输出目录**（缺目录直接 rc=1）；``-o`` 逐行给出写盘目标——恒为源图旁的
      ``.dds``（见模块不变式），ZSTD 模式的 .dds.zstd 由管理器统一压缩；
    - 覆盖语义：已存在输出会被静默覆盖（实测 mtime 变化），增量仍由管理器 pending 过滤负责。
    """

    tool_name = "nvtt"
    OPAQUE_FORMAT = "bc1"
    ALPHA_FORMAT = "bc7"
    DEFAULT_SHARDS = 6
    # shard8 实测驱动排队劣化（88s vs 69s），用户覆盖也不越过实测上限
    MAX_SHARDS = 6
    CHUNK_SIZE = 64

    def __init__(self, options: dict[str, Any]):
        self.options = options

    def resolve_executable(self) -> Path:
        if platform.system() != "Windows":
            raise TextureOptError("NVTT 后端当前仅支持 Windows。")
        texture_tools_path = resolve_texture_tools_path(self.options)
        program_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        program_files_x86 = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
        candidates = [
            str(self.options.get("nvtt_path") or "").strip(),
            str(texture_tools_path / "nvtt_export.exe"),
            str(program_files / "NVIDIA Corporation" / "NVIDIA Texture Tools" / "nvtt_export.exe"),
            str(program_files_x86 / "NVIDIA Corporation" / "NVIDIA Texture Tools" / "nvtt_export.exe"),
            shutil.which("nvtt_export.exe") or "",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return Path(candidate)
        raise TextureOptError(
            "未找到 nvtt_export.exe。可用 download_texture_encoder(backend='nvtt') 获取安装指引。"
        )

    def resolve_shards(self) -> int:
        explicit = _clamp_workers(self.options.get("gpu_workers"))
        if explicit:
            return min(explicit, self.MAX_SHARDS)
        return self.DEFAULT_SHARDS

    def encode_batch(
        self,
        cancel_event: threading.Event,
        *,
        entries: list[dict[str, Any]],
        overwrite_existing: bool,
        scale_percent: int | None,
        output_callback: Callable[[str], None] | None = None,
        on_unit_done: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        usable = [entry for entry in entries if str(entry.get("source_path") or "")]
        if not usable:
            return
        executable = self.resolve_executable()
        pending = _pending_entries(usable, overwrite_existing=overwrite_existing)
        done_base = len(usable) - len(pending)
        if not pending:
            return

        generate_mipmaps = bool(self.options.get("generate_mipmaps", True))
        units = build_invocation_units(
            pending,
            gpu_workers=1,
            generate_mipmaps=generate_mipmaps,
            scale_percent=scale_percent,
        )
        timeout_seconds = max(300, int(self.options.get("encode_batch_timeout_seconds", 480) or 480))
        progress = _ProgressCounter(len(usable), done_base, output_callback)
        jobs = self._build_jobs(
            units,
            executable=executable,
            cancel_event=cancel_event,
            scale_percent=scale_percent,
            timeout_seconds=timeout_seconds,
            on_unit_done=on_unit_done,
        )
        run_jobs(
            [("NvttShard", self.resolve_shards(), jobs)],
            cancel_event=cancel_event,
            progress=progress,
        )

    def _build_jobs(
        self,
        units: list[_TexconvUnit],
        *,
        executable: Path,
        cancel_event: threading.Event,
        scale_percent: int | None,
        timeout_seconds: float,
        on_unit_done: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[_Job]:
        generate_mipmaps = bool(self.options.get("generate_mipmaps", True))
        jobs: list[_Job] = []
        for unit in units:
            for chunk in _chunks(list(unit.entries), self.CHUNK_SIZE):
                # 每个作业自建分片副本：并发分块之间不共享 input_dir，避免互相覆盖
                chunk_unit = _TexconvUnit(
                    directory=unit.directory,
                    route=unit.route,
                    mip_levels=unit.mip_levels,
                    use_gpu=unit.use_gpu,
                    entries=list(chunk),
                )

                def run(unit: _TexconvUnit = chunk_unit) -> None:
                    input_dir: Path | None = None
                    try:
                        input_dir, overrides = prepare_unit_inputs(unit, scale_percent=scale_percent)
                        unit.input_dir = input_dir
                        unit.input_overrides = overrides
                        self._run_batch(
                            executable, unit,
                            cancel_event=cancel_event,
                            generate_mipmaps=generate_mipmaps,
                            timeout_seconds=timeout_seconds,
                        )
                        # 分块 DDS 全部落盘，交给调用方的 ZSTD 管线压缩
                        if on_unit_done is not None:
                            on_unit_done(list(unit.entries))
                    finally:
                        unit.input_dir = None
                        unit.input_overrides = {}
                        if input_dir is not None:
                            shutil.rmtree(input_dir, ignore_errors=True)

                jobs.append(_Job(f"{unit.route}:{Path(unit.directory).name}", run, advance=len(chunk)))
        return jobs

    def _run_batch(
        self,
        executable: Path,
        unit: _TexconvUnit,
        *,
        cancel_event: threading.Event,
        generate_mipmaps: bool,
        timeout_seconds: float,
    ) -> None:
        """写一个 batch 文件并跑一次 ``nvtt_export --batch``（成功路径无任何输出）。"""
        mips = "--mips" if generate_mipmaps else "--no-mips"
        route_format = self.ALPHA_FORMAT if unit.route == "alpha" else self.OPAQUE_FORMAT
        lines = []
        for entry in unit.entries:
            # 写盘目标恒为源图旁的 .dds（与 todds/texconv 同一不变式）：output_path 在
            # ZSTD 模式下是 .dds.zstd 产物投影，直接当 -o 会把未压缩 DDS 写进 .zstd 文件、
            # 且管理器随后找不到临时 DDS 可压缩（整批必坏）。ZSTD 由管理器统一压缩。
            source = Path(str(entry.get("source_path") or ""))
            output = source.with_suffix(".dds")
            # NVTT 不自建输出目录 → 先确保每个输出的父目录存在，否则直接 rc=1
            output.parent.mkdir(parents=True, exist_ok=True)
            # 路径含空格不加引号会让整份 batch 解析失败（本机复现
            # "arguments were not expected: test.png dir/space ..."），带引号路径实测可用
            # batch 行里反斜杠是**转义符**——`E:\Games` 报 "unrecognized
            # escape sequence \G"、`dir\a.png` 把 \a 吃掉（本机三组对照复现）；
            # Windows 正斜杠绝对路径实测可读（含中文+空格目录）→ 行内统一 as_posix()。
            lines.append(
                f"--format {route_format} --quality normal {mips} --mip-filter kaiser "
                f'--save-flip-y -o "{PurePath(output).as_posix()}" '
                f'"{PurePath(_unit_input_path(unit, entry)).as_posix()}"'
            )
        batch_file: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", suffix=".nvdds", delete=False) as handle:
                batch_file = Path(handle.name)
                handle.write("\n".join(lines) + "\n")
            _ToolProcessRunner.run_command(
                [str(executable), "--batch", str(batch_file)],
                cancel_event,
                timeout_seconds=timeout_seconds,
                tool_name=self.tool_name,
                output_callback=None,  # 成功路径零输出，进度由 job.advance 推进
            )
        finally:
            if batch_file and batch_file.exists():
                try:
                    batch_file.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 工厂与按机器容量选择编码器、并发数
# ---------------------------------------------------------------------------

_GPU_PROBE_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
_GPU_PROBE_LOCK = threading.Lock()


def _gpu_probe_cache_key(executable: Path) -> tuple[str, int]:
    """GPU 探测缓存的键：exe 路径 + mtime。作废与查询必须共用同一构造，否则改键时
    一处更新另一处失配、作废会静默失效。"""
    try:
        return (str(executable).lower(), int(executable.stat().st_mtime_ns))
    except OSError:
        return (str(executable).lower(), -1)


def _invalidate_gpu_probe_cache(executable: Path) -> None:
    """作废该 exe 的 GPU 探测缓存。GPU 运行期故障（驱动重置 / TDR）是环境态：
    exe 与 mtime 都没变，但「DirectCompute 可用」的旧结论已经失效——不清理的话，
    下一次任务仍会按缓存乐观启用 GPU 分片、再次撞上同一堵墙。"""
    with _GPU_PROBE_LOCK:
        _GPU_PROBE_CACHE.pop(_gpu_probe_cache_key(executable), None)


def _clamp_workers(value: Any, *, minimum: int = MIN_TEXTURE_WORKERS, maximum: int = MAX_TEXTURE_WORKERS) -> int:
    try:
        number = int(value) if value is not None and str(value).strip() != "" else 0
    except (TypeError, ValueError):
        return 0
    return number if minimum <= number <= maximum else 0


def resolve_worker_plan(options: dict[str, Any], *, gpu_available: bool | None = None) -> dict[str, int]:
    """按机器容量给出的并发方案：cpu=clamp(核数−2, 2, 6)，gpu=3（探测失败 → 0）。

    ``gpu_available``: True/False = 已探测结果；None = 未探测（乐观取默认 3，
    编码前仍会实测）。显式 options 覆盖（``MIN_TEXTURE_WORKERS..MAX_TEXTURE_WORKERS``）
    优先于自动值。

    gpu=3 的梯度实测（196 张 alpha BC7 子集）：
    gpu=1 48.5s / 2 40.9s / 3 38.8s / 4 38.6s / 6 38.3s——3 即饱和，
    4~6 只再赚 0.5~1.2%（与 NVTT shard 8 反而劣化不同，这里不劣化但也不值得）。
    """
    cpu_logical = os.cpu_count() or 4
    cpu_workers = _clamp_workers(options.get("cpu_workers"))
    if not cpu_workers:
        cpu_workers = max(TexconvEncoder.MIN_CPU_WORKERS, min(TexconvEncoder.MAX_CPU_WORKERS, cpu_logical - 2))
    gpu_workers = _clamp_workers(options.get("gpu_workers"))
    if not gpu_workers:
        gpu_workers = 0 if gpu_available is False else TexconvEncoder.DEFAULT_GPU_WORKERS
    return {
        "cpu_logical": cpu_logical,
        "gpu_workers": gpu_workers,
        "cpu_workers": cpu_workers,
    }


def resolve_auto_backend(options: dict[str, Any]) -> str:
    """auto 的落点（质量恒定原则优先）：todds 可用 → todds（质量基准
    62.5~63.8 dB）；todds 不可用而 texconv 可用 → texconv（速度档 1.5~1.8×，但 BC7/BC1
    各 −2~−4 dB，GUI 必须标注质量代价）；两者皆不可用 → todds（由 resolve_executable
    给出带安装指引的因果错误）。"""
    try:
        ToddsEncoder(options).resolve_executable()
    except TextureOptError:
        pass
    else:
        return "todds"
    try:
        TexconvEncoder(options).resolve_executable()
    except TextureOptError:
        return "todds"
    return "texconv"


def create_encoder(options: dict[str, Any]) -> ToddsEncoder | TexconvEncoder | NvttEncoder:
    """按 options 的 ``encoder_backend`` 构造编码器；缺省 todds。

    不接收 ``needs_scaling``：缩放由包装层预缩放承接（仅 todds 走原生 `-sc`），
    三个编码档位都支持缩放，缩放任务不限定必须由 todds 执行。
    """
    backend = str(options.get("encoder_backend") or "todds").strip().lower()
    if backend == "auto":
        backend = resolve_auto_backend(options)
    if backend == "nvtt":
        return NvttEncoder(options)
    if backend == "texconv":
        return TexconvEncoder(options)
    return ToddsEncoder(options)


def texconv_info(executable: Path) -> dict[str, Any]:
    """texconv 机器适配信息：DirectCompute 可用性（4×4 PNG 实测）+ 版本号，按 exe+mtime 缓存。"""
    if platform.system() != "Windows":
        return {"gpu_directcompute": None, "version": ""}
    cache_key = _gpu_probe_cache_key(executable)
    with _GPU_PROBE_LOCK:
        cached = _GPU_PROBE_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    info = {"gpu_directcompute": _probe_gpu_uncached(executable), "version": _read_texconv_version(executable)}
    with _GPU_PROBE_LOCK:
        _GPU_PROBE_CACHE[cache_key] = dict(info)
    return info


def probe_gpu(executable: Path) -> bool | None:
    return texconv_info(executable).get("gpu_directcompute")


def _probe_gpu_uncached(executable: Path) -> bool:
    """用 4×4 RGBA PNG 走一次 BC7 GPU 编码（DirectCompute FL11.0 实测）。"""
    work_dir = Path(tempfile.mkdtemp(prefix="texconv-gpu-probe-"))
    try:
        from PIL import Image

        probe_png = work_dir / "probe.png"
        Image.new("RGBA", (4, 4), (255, 0, 0, 128)).save(probe_png)
        result = subprocess.run(
            [str(executable), "-nologo", "-y", "-m", "0", "-f", "BC7_UNORM", "-o", str(work_dir), str(probe_png)],
            capture_output=True,
            timeout=20,
            **_windows_silent_process_kwargs(),
        )
        return result.returncode == 0 and (work_dir / "probe.dds").exists()
    except Exception:  # noqa: BLE001 - 探测的任何异常都按「GPU 不可用」处理
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _read_texconv_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable)],
            capture_output=True,
            timeout=15,
            **_windows_silent_process_kwargs(),
        )
    except Exception:  # noqa: BLE001 - 版本号是展示信息，失败不阻断
        return ""
    banner = result.stdout.decode("utf-8", errors="replace").splitlines() if result.stdout else []
    for line in banner:
        match = _TEXCONV_VERSION_PATTERN.search(line)
        if match:
            return match.group(1)
    return ""
