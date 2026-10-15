# tests/test_mcp_event_queue.py
"""事件队列并发语义测试：wait_event × search_mod_text fan-out。

背景：``wait_event`` / ``search_mod_text`` 同属 ``_NO_LOCK_TOOLS``（无全局调用锁）。
事件采用 fan-out 分发：每个活跃等待者各持私有缓冲，tap 复制投递，取事件互不影响
（wait_event 只拿自己等的事件，搜索只拿 ``file-search-results``）。

覆盖：
1. wait_event × search_mod_text 并发，双方都不丢事件（含「后到等待者的清理不动
   先到等待者已入队的事件」）；
2. 两个并发 wait_event 交叉注入，各取所需互不干扰；
3. 等待开始前入队的陈旧事件仍被丢弃；
4. wait_event / search_mod_text 超时路径仍返回 timeout；
5. 等待者缓冲随 handler 返回注销干净。

setUp/tearDown 范式照抄 test_mcp_server.TestWaitEventBridge 与
test_mcp_capabilities_ext.TestSearchModText。
"""

from __future__ import annotations

import queue
import threading
import time
import unittest
from types import SimpleNamespace

from backend.mcp_server import (
    _EVENT_QUEUE,
    _SUBSCRIBERS,
    _handle_search_mod_text,
    _handle_wait_event,
    install_event_tap,
    restore_event_tap,
)


class FakeApi:
    """最小后端替身：wait_event 不读 api，search 只读 file_search_mgr。"""

    def __init__(self) -> None:
        self.search_payload: dict = {}


def _hit(file_path: str, line_number: int = 1) -> dict:
    return {"file_path": file_path, "line_number": line_number, "matched_line": "<def>"}


class TestEventQueueConcurrency(unittest.TestCase):
    """fan-out 的并发语义：等待者互不吃事件，陈旧事件丢弃与超时路径行为不变。"""

    def setUp(self):
        from backend.utils.event_bus import EventBus

        restore_event_tap()  # 防御：清掉任何遗留 tap
        self.EventBus = EventBus
        self._saved_emit = EventBus.__dict__.get("emit")
        EventBus.emit = lambda name, data=None: None  # type: ignore[method-assign]
        self._pending_backup = list(EventBus._pending_events)
        EventBus._pending_events.clear()
        self._timers: list[threading.Timer] = []
        self._drain_shared_queue()
        install_event_tap()  # 包住上面的 no-op：事件才进共享队列与 fan-out 缓冲
        self.assertEqual(list(_SUBSCRIBERS), [])  # 防御：上个用例不得泄漏订阅

    def tearDown(self):
        for timer in self._timers:
            timer.cancel()
            timer.join(timeout=2)
        restore_event_tap()
        if self._saved_emit is not None:
            self.EventBus.emit = self._saved_emit  # type: ignore[method-assign]
        self.EventBus._pending_events[:] = self._pending_backup
        self._drain_shared_queue()
        self.assertEqual(list(_SUBSCRIBERS), [])  # handler 必须注销干净（防泄漏）

    # ---- 工具方法 --------------------------------------------------------

    @staticmethod
    def _drain_shared_queue() -> None:
        while True:
            try:
                _EVENT_QUEUE.get_nowait()
            except queue.Empty:
                return

    def _emit_later(self, delay: float, event: str, data: dict) -> None:
        timer = threading.Timer(delay, self.EventBus.emit, args=(event, data))
        timer.start()
        self._timers.append(timer)

    def _search_api(self, task_id: str, plan) -> FakeApi:
        """plan: [(delay, data), ...] —— start_search 返回后按延迟依次 emit。"""
        api = FakeApi()

        def start_search(payload):
            api.search_payload = dict(payload or {})
            for delay, data in plan:
                timer = threading.Timer(
                    delay,
                    self.EventBus.emit,
                    args=("file-search-results", {"task_id": task_id, **data}),
                )
                timer.start()
                self._timers.append(timer)
            return task_id

        api.file_search_mgr = SimpleNamespace(start_search=start_search)
        return api

    @staticmethod
    def _run_in_thread(fn, name: str):
        """在守护线程里跑 fn，返回 (thread, box)；box 收 value 或 error。"""
        box: dict = {}

        def runner() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - 测试内转交断言
                box["error"] = exc

        thread = threading.Thread(target=runner, daemon=True, name=name)
        thread.start()
        return thread, box

    # ---- 1. wait_event × search_mod_text 并发不丢事件 ---------------------

    def test_concurrent_wait_event_and_search_lose_no_events(self):
        # 构造：搜索 t0 启动（0.10s 入队 partial、0.50s 入队 done），wait_event
        # t0+0.25 才起等，scan-complete 在 0.35s 到达。起等会先清理陈旧事件，且等待
        # 循环只认自己等的事件——共享队列下单方清理/取走会让另一方拿不到结果。
        # fan-out 下双方各读各的缓冲，partial / done / scan-complete 各自保留。
        api = self._search_api(
            "t-conc",
            [
                (
                    0.10,
                    {
                        "results": [_hit("a.xml")],
                        "done": False,
                        "status": "running",
                        "matched_count": 2,
                    },
                ),
                (
                    0.50,
                    {
                        "results": [_hit("b.xml", 9)],
                        "done": True,
                        "status": "success",
                        "matched_count": 2,
                    },
                ),
            ],
        )
        search_thread, search_box = self._run_in_thread(
            lambda: _handle_search_mod_text(api, query="x", timeout_seconds=5.0),
            name="search",
        )
        time.sleep(0.25)  # partial 已入队（起等的陈旧清理不得动它）
        self._emit_later(0.10, "scan-complete", {"status": "success"})
        wait_payload = _handle_wait_event(FakeApi(), event_name="scan-complete", timeout=3.0)

        search_thread.join(timeout=6)
        self.assertFalse(search_thread.is_alive(), "search_mod_text 未在超时前返回")
        self.assertNotIn("error", search_box, search_box.get("error"))

        # wait_event：既没被搜索的 partial/done 干扰，也没丢自己的 scan-complete
        self.assertEqual(wait_payload["status"], "success")
        self.assertEqual(wait_payload["data"]["event"], "scan-complete")

        # search：聚合完整（partial + done），未被 wait_event 的事件干扰
        payload = search_box["value"]
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["matched_count"], 2)
        self.assertEqual(payload["returned"], 2)
        self.assertFalse(payload["truncated"])

    # ---- 2. 两个并发 wait_event 各取所需 ----------------------------------

    def test_concurrent_waiters_each_receive_their_own_event(self):
        # 构造：交叉注入——先发 B 等的 game-status-changed，后发 A 等的 scan-complete。
        # 单一共享队列下 A 取走前者、B 取走后者，均因不匹配而丢弃，双双超时；
        # fan-out 下各自缓冲各取所需。
        thread_a, box_a = self._run_in_thread(
            lambda: _handle_wait_event(FakeApi(), event_name="scan-complete", timeout=3.0),
            name="wait-scan",
        )
        thread_b, box_b = self._run_in_thread(
            lambda: _handle_wait_event(FakeApi(), event_name="game-status-changed", timeout=3.0),
            name="wait-game",
        )
        time.sleep(0.1)  # 两个等待者都已订阅
        self._emit_later(0.05, "game-status-changed", {"status": "running"})
        self._emit_later(0.15, "scan-complete", {"status": "success"})
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)
        self.assertFalse(thread_a.is_alive() or thread_b.is_alive(), "wait_event 未在超时前返回")
        self.assertNotIn("error", box_a, box_a.get("error"))
        self.assertNotIn("error", box_b, box_b.get("error"))
        self.assertEqual(box_a["value"]["status"], "success")
        self.assertEqual(box_a["value"]["data"]["event"], "scan-complete")
        self.assertEqual(box_b["value"]["status"], "success")
        self.assertEqual(box_b["value"]["data"]["event"], "game-status-changed")

    # ---- 3. 等待前入队的陈旧事件仍被丢弃 ---------------------------------

    def test_stale_event_queued_before_wait_is_discarded(self):
        # 调用方全程未触发扫描时，历史 scan-complete 不能被当成交付命中
        self.EventBus.emit("scan-complete", {"status": "success", "seq": "stale"})
        payload = _handle_wait_event(FakeApi(), event_name="scan-complete", timeout=0.3)
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(payload["data"], {"event": "scan-complete"})

    # ---- 4. 超时路径不变 --------------------------------------------------

    def test_wait_event_timeout_returns_timeout_payload(self):
        payload = _handle_wait_event(FakeApi(), event_name="never-emitted", timeout=0.2)
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(payload["data"], {"event": "never-emitted"})

    def test_search_timeout_returns_timeout_with_partial(self):
        api = self._search_api(
            "t-timeout",
            [
                (
                    0.05,
                    {
                        "results": [_hit("f.xml")],
                        "done": False,
                        "status": "running",
                        "matched_count": 1,
                    },
                )
            ],
        )
        payload = _handle_search_mod_text(api, query="x", timeout_seconds=1.0)
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(payload["returned"], 1)
        self.assertIn("超时", payload["message"])

    # ---- 5. 订阅生命周期 --------------------------------------------------

    def test_subscribers_unsubscribed_after_handler_returns(self):
        _handle_wait_event(FakeApi(), event_name="never-emitted", timeout=0.1)
        self.assertEqual(list(_SUBSCRIBERS), [])
