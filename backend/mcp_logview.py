# backend/mcp_logview.py
"""MCP ``read_logs`` 的纯过滤 / 翻页逻辑（无 I/O、无状态，stdlib + python-dateutil）。

上游读取链路的已核实事实（改动本文件前先复核对应源码）：

1. **分页顺序**：``BaseLogReader.get_paged_data``（backend/utils/logger.py）
   是「倒序分页」——``page=1`` 是**最新页**（切片 ``blocks[total-page*ps :
   total-(page-1)*ps]``），页号越大越旧；越界页返回空 ``blocks``。
2. **页内顺序**：内部 ``blocks`` 数组按文件行序追加（``_parse_file_base`` /
   ``_parse_file_to_blocks`` 均顺序读取）＝**最旧在前**；分页切片保持数组升序，
   因此**每页内部是最旧→最新**。要拼出「全局最新优先」流必须：按 page=1,2,… 逐页
   取，**把每页 reverse 后再依次追加**（``scan_pages`` 即此，顺序不要反转）。
3. **等级集合**：``BaseLogReader._normalize_log_level``（backend/utils/logger.py）
   只做 strip、缺失给 ``"UNKNOWN"``——它**不收敛**到固定集合。按写入方核实的实际取值：
   ``DEBUG/INFO/WARNING/ERROR/CRITICAL``（``JSONFormatter`` 写 ``record.levelname``）、
   ``EXCEPTION``（游戏日志诊断用，mgr_game_logs.py 的 ``LogAnalyzer._is_diagnostic_level``
   与 ``LogCondenser.condense_for_ai``）、``UNKNOWN``；
   Player.log 纯文本块只产出 ``INFO/ERROR/WARNING``
   （``GameLogManager._parse_file_to_blocks``）。
4. **时间戳格式**：app JSONL 为 ``'%Y-%m-%d %H:%M:%S.%f'[:-3]`` ＝
   ``"YYYY-MM-DD HH:MM:SS.mmm"``（空格分隔、毫秒精度、本地时区，
   ``JSONFormatter.format`` 写入）；游戏 JSONL 原样透传；
   **Player.log 纯文本块 ``timestamp=''``**（``GameLogManager._parse_file_to_blocks``）——
   激活时间过滤时这类块必须被排除。
5. **行号**：``raw_lines`` 是 **1 基**源文件行号列表（linecache 从 1 开始）；
   文本块的续行（缩进 / 栈帧）会 append 进同一块，故行范围用 min..max 重叠语义。
6. **API 形状**：``api.read_log_page(log_type, filename, page=1, page_size=1000,
   profile_scope="active")`` 返回
   ``ApiResponse.success({"status","blocks","has_more","total_pages","current_page"})``；
   ``scan_pages`` 的 ``fetch_page`` 适配器收到的就是这个**内层 payload**。

依据文件：backend/utils/logger.py 的 ``BaseLogReader.get_paged_data``、
``BaseLogReader._parse_file_base``（JSON 解析）与 ``JSONFormatter``；
backend/managers/mgr_game_logs.py 的 ``GameLogManager._parse_file_to_blocks``
（文本块）与 ``LogAnalyzer._is_diagnostic_level``（诊断等级）；
backend/api.py 的 ``read_log_page``。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

__all__ = [
    "CANONICAL_LEVELS",
    "DEFAULT_MAX_SCAN_PAGES",
    "block_matches",
    "block_time",
    "filter_blocks",
    "normalize_levels",
    "parse_time",
    "scan_pages",
]

# 上游写入方核实的等级全集（模块 docstring 事实 3）。仅供参考/校验：
# normalize_levels 不裁剪集合外的值，理由见其 docstring（level 在上游是透传的）。
CANONICAL_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "EXCEPTION", "UNKNOWN"}
)

# 扫描页数上限：MCP 工具预算纪律（尾读优先，默认最多读 20 页 × page_size 条）。
DEFAULT_MAX_SCAN_PAGES = 20

# 用户口语别名 → 上游规范名（levels 里常见 warn/err 写法）。
_LEVEL_ALIASES = {"warn": "WARNING", "err": "ERROR"}

# 相对时间：90s / 30m / 1h / 2d / 1w（允许小数，如 1.5h）。
_RELATIVE_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$", re.IGNORECASE)
_RELATIVE_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


def _parse_iso_timestamp(text: str) -> float | None:
    """ISO8601 → epoch 秒；stdlib ``fromisoformat`` 优先，失败退 ``dateutil.isoparse``。

    两者都拿不准时返回 None（由调用方决定是抛错还是静默跳过）。
    无时区的时间按本地时间解释——与 ``JSONFormatter`` 写入的本地时间戳一致。
    """
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    try:
        from dateutil.parser import isoparse

        return isoparse(text).timestamp()
    except Exception:
        return None


def parse_time(value: str, *, now: datetime | None = None) -> float:
    """把用户输入解析为 epoch 秒。

    支持相对量（``90s``/``30m``/``1h``/``2d``/``1w``，相对 ``now`` 往回算）
    与 ISO8601 绝对时间（``2026-09-27T03:00:00`` / ``2026-09-27 03:00:00``）。
    解析失败抛 ``ValueError``（中文短消息，直接给工具层回给调用方）。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间格式无效：请输入如 90s/30m/1h 或 2026-09-27T03:00:00")
    text = value.strip()
    matched = _RELATIVE_RE.match(text)
    if matched:
        base = now if now is not None else datetime.now()
        return base.timestamp() - float(matched.group(1)) * _RELATIVE_SECONDS[matched.group(2).lower()]
    timestamp = _parse_iso_timestamp(text)
    if timestamp is None:
        raise ValueError(f"时间格式无效：{text}")
    return timestamp


def normalize_levels(levels: list[str] | None) -> set[str] | None:
    """None/空 → None（不过滤）；否则大写规范化 + 别名映射（warn→WARNING、err→ERROR）。

    未知等级**原样大写保留**，不丢弃也不报错：上游 ``_normalize_log_level`` 对
    level 是透传的（游戏 JSONL 可能带集合外取值），从过滤集合里裁掉会让用户
    显式点名的等级静默失效（甚至全空变成「不过滤」）。
    """
    if not levels:
        return None
    result: set[str] = set()
    for item in levels:
        name = str(item).strip()
        if not name:
            continue
        result.add(_LEVEL_ALIASES.get(name.lower(), name.upper()))
    return result or None


def block_time(block: dict) -> float | None:
    """取块时间戳的 epoch 秒；空串 / 缺失 / 解析不了 → None。

    Player.log 纯文本块 ``timestamp`` 恒为 ``''``（事实 4），必然落到 None，
    由 ``block_matches`` 决定「激活时间过滤时排除」。
    """
    raw = block.get("timestamp") if isinstance(block, dict) else None
    if not raw or not isinstance(raw, str):
        return None
    return _parse_iso_timestamp(raw.strip())


def _line_bounds(block: dict) -> tuple[int, int] | None:
    """raw_lines 的 (min, max)；缺失 / 空 / 全部不可转 int → None。"""
    raw_lines = block.get("raw_lines")
    if not raw_lines:
        return None
    numbers: list[int] = []
    for item in raw_lines if isinstance(raw_lines, (list, tuple)) else [raw_lines]:
        try:
            numbers.append(int(item))
        except (TypeError, ValueError):
            continue
    if not numbers:
        return None
    return min(numbers), max(numbers)


def block_matches(
    block: dict,
    *,
    levels: set[str] | None,
    since_ts: float | None,
    until_ts: float | None,
    line_from: int | None,
    line_to: int | None,
    keyword: str | None,
) -> bool:
    """单块过滤：所有**已提供**的维度按 AND 生效（任一不过滤的维度传 None）。

    - levels：块等级（upper）必须在集合内（块等级也是透传的，故这里同样不裁剪）。
    - 时间：``since_ts <= t <= until_ts``（闭区间）；**无时间戳的块在时间过滤
      激活时被排除**——Player.log 纯文本块没有时间，混进时间窗只会误导。
    - 行范围：块 ``raw_lines`` 的 min..max 与 ``[line_from, line_to]`` 做**重叠**
      判断（块跨 [100..105]，line_from=105 命中、106 不命中）；无 raw_lines 的块
      在行过滤激活时被排除（事实 5：文本块续行都记在同一块里）。
    - keyword：对 ``message + details`` 做大小写不敏感子串匹配（日志内容可能
      来自任何 Mod，大小写与本地化不定，子串是唯一稳的匹配面）。
    """
    if levels is not None:
        level = str(block.get("level") or "").strip().upper()
        if level not in levels:
            return False

    if since_ts is not None or until_ts is not None:
        timestamp = block_time(block)
        if timestamp is None:
            return False
        if since_ts is not None and timestamp < since_ts:
            return False
        if until_ts is not None and timestamp > until_ts:
            return False

    if line_from is not None or line_to is not None:
        bounds = _line_bounds(block)
        if bounds is None:
            return False
        lo, hi = bounds
        if line_to is not None and lo > line_to:
            return False
        if line_from is not None and hi < line_from:
            return False

    if keyword:
        haystack = f"{block.get('message') or ''}\n{block.get('details') or ''}".lower()
        if keyword.lower() not in haystack:
            return False

    return True


def filter_blocks(
    blocks: list[dict],
    *,
    levels: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    line_from: int | None = None,
    line_to: int | None = None,
    keyword: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """对一个已就绪的块列表做一次性过滤，**保持输入顺序**。

    时间字符串只解析一次；``since``/``until`` 非法时 ``ValueError`` 直接上抛
    （工具层负责转成错误结构，不在这里吞）。
    """
    norm_levels = normalize_levels(levels)
    since_ts = parse_time(since, now=now) if since else None
    until_ts = parse_time(until, now=now) if until else None
    return [
        block
        for block in blocks
        if block_matches(
            block,
            levels=norm_levels,
            since_ts=since_ts,
            until_ts=until_ts,
            line_from=line_from,
            line_to=line_to,
            keyword=keyword,
        )
    ]


def scan_pages(
    fetch_page: Callable[[int, int], dict],
    *,
    levels: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    line_from: int | None = None,
    line_to: int | None = None,
    keyword: str | None = None,
    tail: int | None = None,
    limit: int = 100,
    offset: int = 0,
    page_size: int = 1000,
    max_pages: int = DEFAULT_MAX_SCAN_PAGES,
) -> dict[str, Any]:
    """从 ``page=1``（最新页）向上逐页扫描，拼出**全局最新优先**的匹配流并取窗。

    ``fetch_page(page, page_size)`` 是 ``api.read_log_page`` 内层 payload 的适配器
    （事实 6）；返回 ``{"blocks": []}`` 视为读到底，立即结束扫描。

    关键语义（尾读优先 / 预算纪律）：

    - **页内顺序归一**：真实 reader 每页内部是「最旧→最新」（事实 2），本函数把
      每页 reverse 后追加，保证 ``items`` 与 ``matches`` 恒为最新优先。
    - ``tail>=1``：强制 ``offset=0``，凑满 ``tail`` 条**即停**（页粒度提前停止，
      少读页数＝省预算）；``tail<1`` 视为未设置。
    - 非 tail：凑满 ``offset+limit`` 条即停；``items = matches[offset:offset+limit]``。
    - 判停粒度为**整页**：一页内的匹配全收完再判停——这样读者声明 ``has_more=False``
      时 ``total`` 一定是精确值（提前半页断掉会让 exhausted 时的 total 少算）。
    - ``exhausted``：仅当**读者明确读到底**（空页或 ``has_more=False``）才为 True，
      此时 ``total`` 精确；被 tail/offset+limit 或 ``max_pages`` 截停时为 False，
      ``total`` 是「已找到的下界」，``has_more_pages = not exhausted``。
    """
    norm_levels = normalize_levels(levels)
    since_ts = parse_time(since) if since else None
    until_ts = parse_time(until) if until else None
    offset = max(0, int(offset))
    limit = max(0, int(limit))
    use_tail = tail is not None and int(tail) >= 1
    if use_tail:
        offset = 0  # tail 语义：只从最新开始，忽略调用方的 offset
        want = int(tail)
    else:
        want = offset + limit

    matches: list[dict] = []
    scanned_pages = 0
    exhausted = False

    if want > 0:
        for page in range(1, max(0, int(max_pages)) + 1):
            data = fetch_page(page, page_size) or {}
            scanned_pages += 1
            page_blocks = list(data.get("blocks") or [])
            if not page_blocks:
                exhausted = True  # 空页 = 读到底
                break
            # 页内最旧→最新（已核实），反转后拼进全局最新优先流。
            for block in reversed(page_blocks):
                if block_matches(
                    block,
                    levels=norm_levels,
                    since_ts=since_ts,
                    until_ts=until_ts,
                    line_from=line_from,
                    line_to=line_to,
                    keyword=keyword,
                ):
                    matches.append(block)
            has_more = data.get("has_more")
            if len(matches) >= want or has_more is False:
                # 凑够了（提前收手）或读者声明到底；只有后者才认定 exhausted。
                exhausted = has_more is False
                break

    items = matches[: int(tail)] if use_tail else matches[offset : offset + limit]
    return {
        "items": items,
        "total": len(matches),
        "exhausted": exhausted,
        "scanned_pages": scanned_pages,
        "has_more_pages": not exhausted,
    }
