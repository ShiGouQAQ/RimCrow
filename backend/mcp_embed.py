# backend/mcp_embed.py
"""进程内 Streamable HTTP 端点（唯一的运行数据面）。

在 API 就绪即挂载（main.py api_ready 之后、launch-mode 分支之前），与 pywebview
窗口完全解耦：endpoint 写出即 ready，任何等待都不指向 window_created /
page_loaded。

安全边界：只绑 ``127.0.0.1``（永不 0.0.0.0）+ 动态端口 + 每启动随机
256-bit bearer token（只写进 endpoint 文件）+ HTTP 401 零工具执行。本地信任模型：
同机进程读 endpoint 文件即可取得 token，与 GABS ``bridge.json`` 采用同一形态。

生命周期：uvicorn 以 daemon 线程运行，随进程退出；``api.cleanup()`` 被包装一层，
窗口关闭触发 cleanup 时先删 endpoint、停服务，再执行原清理逻辑。
"""

from __future__ import annotations

import atexit
import hmac
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.mcp_endpoint import EndpointInfo, remove_endpoint, write_endpoint
from backend.utils.logger import logger

# uvicorn 进入 serving 状态的等待上限（秒）；超时 = 本进程不暴露端点（诚实降级）。
_SERVE_TIMEOUT = 10.0

_ACTIVE_LOCK = threading.Lock()
_ACTIVE: "McpEmbed | None" = None


def _bearer_guard(app: Any, token: str) -> Any:
    """纯 ASGI 包装：无 token / 错 token → 401，零工具执行。"""
    from starlette.responses import JSONResponse

    expected = f"Bearer {token}".encode("latin-1")

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = scope.get("headers") or []
            supplied = next((v for k, v in headers if k == b"authorization"), b"")
            if not hmac.compare_digest(supplied, expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await app(scope, receive, send)

    return guarded


def _serve_in_thread(asgi_app: Any) -> tuple[Any, threading.Thread, int]:
    """在 daemon 线程里编程式跑 uvicorn（动态端口），返回 (server, thread, port)。

    - ``log_config=None``：不让 uvicorn 改写全局日志配置（stdout 纪律）；
    - ``port=0``：内核分配动态端口，双实例天然无冲突；
    - uvicorn 的信号注册在非主线程自动跳过（capture_signals 行为如此）。
    """
    import uvicorn

    config = uvicorn.Config(
        asgi_app, host="127.0.0.1", port=0, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mcp-embed-http", daemon=True)
    thread.start()
    deadline = time.monotonic() + _SERVE_TIMEOUT
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        raise RuntimeError(f"uvicorn 未能在 {_SERVE_TIMEOUT}s 内进入 serving 状态")
    port = int(server.servers[0].sockets[0].getsockname()[1])
    return server, thread, port


class McpEmbed:
    """单个 GUI 进程内的 MCP 端点宿主（start/stop 幂等）。"""

    def __init__(
        self,
        api: Any,
        *,
        mode: str = "desktop",
        on_stage: Callable[..., None] | None = None,
        endpoint_path: Path | None = None,
    ) -> None:
        self.api = api
        self.mode = mode
        self.on_stage = on_stage
        self.endpoint_path = endpoint_path
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._info: EndpointInfo | None = None
        self._started = False
        self._lock = threading.Lock()

    @property
    def info(self) -> EndpointInfo | None:
        return self._info

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> EndpointInfo | None:
        """挂载端点并写出 endpoint 文件；任何失败只降级为“无 MCP”，不打断 GUI。"""
        with self._lock:
            if self._started:
                return self._info
            try:
                # 导入工具层会顺带完成 stdout 纪律（logger → stderr）。
                from backend.mcp_server import create_mcp_app

                mcp = create_mcp_app(self.api, with_event_bridge=True)
                asgi_app = mcp.http_app(path="/mcp")
                token = secrets.token_urlsafe(32)  # 256-bit，每次启动随机
                server, thread, port = _serve_in_thread(_bearer_guard(asgi_app, token))
                info = write_endpoint(
                    url=f"http://127.0.0.1:{port}/mcp",
                    pid=os.getpid(),
                    token=token,
                    mode=self.mode,
                    path=self.endpoint_path,
                )
            except Exception:  # noqa: BLE001 - 端点失败不得影响主程序
                logger.error(
                    "MCP embed failed to start; GUI continues without MCP endpoint",
                    exc_info=True,
                    extra={"error_code": "MCP.EMBED.START_FAILED"},
                )
                return None
            self._server, self._thread, self._info = server, thread, info
            self._started = True
            atexit.register(self.stop)
            self._install_cleanup_hook()
            if self.on_stage is not None:
                self.on_stage("mcp_endpoint_ready", port=port)
            logger.info("MCP endpoint ready: %s (mode=%s)", info.url, self.mode)
            return info

    def stop(self) -> None:
        """删 endpoint → 停 uvicorn → 还原事件桥（幂等，atexit/cleanup 双挂）。"""
        with self._lock:
            if not self._started:
                return
            self._started = False
            remove_endpoint(self.endpoint_path)
            if self._server is not None:
                self._server.should_exit = True
            if self._thread is not None:
                self._thread.join(timeout=5.0)
            from backend.mcp_server import restore_event_tap

            restore_event_tap()
            logger.info("MCP endpoint stopped")

    def _install_cleanup_hook(self) -> None:
        """把 stop 挂进 api.cleanup（main.py 对上游唯一侵入只有一行钩子）。"""
        api = self.api
        original = getattr(api, "cleanup", None)
        if not callable(original) or getattr(api, "_mcp_cleanup_wrapped", False):
            return

        def cleanup() -> None:
            try:
                self.stop()
            finally:
                original()

        api.cleanup = cleanup  # 实例属性遮蔽类方法：main.py 注册 events.closed 时取到包装版
        api._mcp_cleanup_wrapped = True  # type: ignore[attr-defined]


def start_mcp_embed(
    api: Any,
    mode: str = "desktop",
    *,
    on_stage: Callable[..., None] | None = None,
    endpoint_path: Path | None = None,
) -> McpEmbed | None:
    """main.py 的钩子入口：进程内单例，失败返回 None（GUI 不受影响）。"""
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is not None and _ACTIVE.started:
            return _ACTIVE
        embed = McpEmbed(
            api, mode=mode, on_stage=on_stage, endpoint_path=endpoint_path
        )
        if embed.start() is None:
            return None
        _ACTIVE = embed
        return embed


def stop_mcp_embed() -> None:
    """显式停端点（测试/退出用；幂等）。"""
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is not None:
            _ACTIVE.stop()
            _ACTIVE = None
