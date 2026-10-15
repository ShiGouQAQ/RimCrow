# backend/mcp_guard.py
"""体积护栏语义层：结构化截断，产物必须是合法 JSON（双层上限的第一层）。

FastMCP 的 ``ResponseLimitingMiddleware`` 只保证“不打死管道”——其截断必然破坏
JSON 结构（UTF-8 边界安全的单 TextContent + suffix，源码行为如此）。语义层在**工具
返回值**上工作：按元素边界二分截断、保留响应结构、附 ``__truncated__`` 标记与
翻页 hint，让 agent 拿到的永远是可 ``json.loads`` 的结构化结果。

**两层上限（分档而非同值）**：

- **保险层** = :func:`max_output`（env ``RIMCROW_MCP_MAX_OUTPUT``，默认 262144 =
  256 KiB），只给 ``ResponseLimitingMiddleware(max_size=...)`` 用；它的度量基准是
  **整帧 ToolResult 序列化**（文本经 JSON 转义 + 包装字段），超限必然把文本从中间
  切开再补 suffix → 破损 JSON。它只保证体积不超限，不保证结构完整。
- **语义层** = :func:`semantic_max_output` ＝ 保险层 × ``SEMANTIC_RATIO``（0.75），
  是 :func:`guard_payload` 的默认 cap；产物恒为合法 JSON。两层**必须留出转义余量**：
  语义层若按保险层同值（＝262144）放行 261948B 文本，整帧将达到 275752B
  （1.05×）> 262144 → 保险层截断 → 客户端 ``JSONDecodeError: Extra data``。
  比值与降级机制见 ``SEMANTIC_RATIO`` 处的说明；极端高转义密度载荷由
  :func:`frame_cost` 重截。

约定：

- 截断 hint 与错误文案**不得拼入第三方工坊模组名称/描述**（防注入边界，仅用常量文案）；
- 只依赖标准库，工具层/单测/桥侧测试均可 import（:func:`frame_cost` 只用正则估计
  保险层字节数，不 import fastmcp/pydantic）。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_MAX_OUTPUT",
    "SEMANTIC_RATIO",
    "TRUNCATED_KEY",
    "build_error_text",
    "frame_cost",
    "guard_payload",
    "json_default",
    "max_output",
    "page_envelope",
    "semantic_max_output",
]

DEFAULT_MAX_OUTPUT = 262144  # 256 KiB
TRUNCATED_KEY = "__truncated__"

# 语义 cap ＝ 保险 cap × 0.75。
# 整帧字节 = 文本 UTF-8 字节数 + 文本内 '"' 数 + 文本内 '\' 数 + 126（ToolResult 包装）。
# 「整帧 / 文本」比值：中文模组名 1.075、ASCII 模组元数据 1.114、含引号与反斜杠的日志行 1.152、
# 极端全引号反斜杠载荷 1.846 → 0.75 × 1.152 = 0.864 < 1：典型内容留约 14% 余量；
# 极端密度内容不靠比值，由 frame_cost 按实际密度重截（guard_payload 内）。
SEMANTIC_RATIO = 0.75

# 保险层度量的是 pydantic_core.to_json(ToolResult) 的包装开销（常数 126；
# 中间件源码里只预留了 50，以实际序列化为准）。
_TOOL_RESULT_OVERHEAD = 126

# 语义 cap 放不下时的降级重截次数与安全系数（见 guard_payload 末段）。
_FRAME_RETRIES = 2
_FRAME_SAFETY = 0.97

# 常量文案：永不拼接模组名/用户输入（防注入边界）。
_TRUNCATION_HINT = "已按体积上限截断；请用 limit/offset/filter 缩小取数范围"
_PAGE_HINT = "用 offset 翻下一页"

# 走 \u00XX 而非短快捷转义的控制符（\b(08) \f(0C) \n(0A) \r(0D) \t(09) 走快捷，勿重复计）
_CTRL_OTHER_RE = re.compile(r"[\x00-\x07\x0b\x0e-\x1f]")


def max_output() -> int:
    """保险层体积上限（字节）。env 每次读取，便于测试与运行期调整。

    只描述 ``ResponseLimitingMiddleware`` 的度量基准；语义层请用
    :func:`semantic_max_output`。
    """
    import os

    raw = os.environ.get("RIMCROW_MCP_MAX_OUTPUT", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_MAX_OUTPUT


def semantic_max_output() -> int:
    """语义层默认上限（字节）＝ 保险层 :func:`max_output` × ``SEMANTIC_RATIO``。

    ``guard_payload`` 未显式传 ``max_bytes`` 时用它：既要尽量多给数据，又必须给
    「内层文本 → 整帧响应」的 JSON 转义留出余量，否则语义层放行的合法 JSON 会被
    保险层从中间切破。env 缩放保险层时按比例同步缩放。
    """
    return int(max_output() * SEMANTIC_RATIO)


def frame_cost(text: str) -> int:
    """保险层真正会量到的字节数（不 import fastmcp 的等价估计）。

    等价于 ``len(pydantic_core.to_json(ToolResult(content=[TextContent(text=...)])))``
    —— 保险层只在整帧序列化后超 ``max_output()`` 时才截断，所以语义层产物必须让
    这个数 ≤ :func:`max_output`。计算方式（逐字节等价）：

    ``UTF-8 字节数 + '"' 与 '\\' 各 +1 + 控制符（\\n\\r\\t\\b\\f +1、其余 \\u00XX +5）
    + 126（ToolResult 包装字段）``；非 ASCII 不转义（保证中文载荷不吃亏）。
    tests/test_mcp_payload_e2e.py 用 pydantic 序列化器钉死这个等式。
    """
    raw = len(text.encode("utf-8"))
    shortcuts = text.count("\n") + text.count("\r") + text.count("\t") + text.count("\b") + text.count("\f")
    other_ctrl = len(_CTRL_OTHER_RE.findall(text))
    return raw + text.count('"') + text.count("\\") + shortcuts + other_ctrl * 5 + _TOOL_RESULT_OVERHEAD


def json_default(value: Any) -> Any:
    """JSON 降级序列化（与 ApiResponse.serialize_data 同一计算方式的常用子集）。"""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _dumps(payload: Any, max_bytes: int | None = None) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=json_default, separators=(",", ":"))
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        return text  # 调用方按需再裁；这里只负责序列化
    return text


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


_UNSERIALIZABLE = 1 << 62  # 序列化失败的哨兵值：等价于「肯定放不下」


def _size_of(payload: Any) -> int:
    """载荷序列化后的字节数；序列化失败返回哨兵（等价于超限，交调用方截断）。"""
    try:
        return _byte_len(
            json.dumps(payload, ensure_ascii=False, default=json_default, separators=(",", ":"))
        )
    except (TypeError, ValueError, RecursionError):
        return _UNSERIALIZABLE


def _fit(payload: Any, max_bytes: int) -> bool:
    return _size_of(payload) <= max_bytes


def _largest_list_key(payload: dict[str, Any]) -> str | None:
    """选最大的顶层列表字段做截断对象（元素边界截断的前提）。"""
    best_key: str | None = None
    best_size = -1
    for key, value in payload.items():
        if isinstance(value, list) and value:
            size = _size_of(value)
            if size > best_size:
                best_key, best_size = key, size
    return best_key


# ---------------------------------------------------------------------------
# 有界深度的列表路径（真实载荷的列表在 data.* 下，不在顶层）
# ---------------------------------------------------------------------------

_MAX_PATH_DEPTH = 3  # 列表路径最多 3 段（data.all_mods = 2 段）
_MAX_LIST_PATHS = 24  # 候选路径条数上限
_MAX_VISIT_NODES = 512  # 遍历节点上限（防宽字典指数膨胀）


def _collect_list_paths(payload: dict[str, Any]) -> list[tuple[str, ...]]:
    """收集「有界深度、有界条数」的非空列表路径（顶层 key 即 1 段路径）。

    只沿 dict 容器下降（列表本身就是截断对象，不进入元素），因此任意两条路径
    互不为前缀——截断其中一条不会让另一条失效。路径段只认 str 键；遍历同时受
    深度 / 条数 / 节点数三重上限约束，防极端载荷把 probe 次数与内存拖爆。
    """
    paths: list[tuple[str, ...]] = []
    stack: list[tuple[tuple[str, ...], dict[str, Any]]] = [((), payload)]
    visited = 0
    while stack:
        path, node = stack.pop()
        visited += 1
        if visited > _MAX_VISIT_NODES or len(paths) >= _MAX_LIST_PATHS:
            break
        for key, value in node.items():
            if not isinstance(key, str) or not key:
                continue  # 非 str 键无法用路径字符串寻址，原样保留不截断
            child = path + (key,)
            if isinstance(value, list):
                if value and len(child) <= _MAX_PATH_DEPTH:
                    paths.append(child)
            elif isinstance(value, dict) and len(child) < _MAX_PATH_DEPTH:
                stack.append((child, value))
    return paths


def _path_str(path: tuple[str, ...]) -> str:
    """路径 → 响应字段键（``data.all_mods``）。段内含 ``.`` 的键会歧义，故只用于报告。"""
    return ".".join(path)


def _get_at(root: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = root
    for key in path:
        node = node[key]
    return node


def _set_at(root: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """沿路径写入并**只复制路径上的容器**（兄弟分支共享引用，不改原对象）。"""
    out = dict(root)
    if len(path) == 1:
        out[path[0]] = value
        return out
    cur_out = out
    cur_in = root
    for key in path[:-1]:
        child_in = cur_in[key]
        child_out = dict(child_in)
        cur_out[key] = child_out
        cur_out = child_out
        cur_in = child_in
    cur_out[path[-1]] = value
    return out


def _marker_block(totals: dict[str, int]) -> dict[str, Any]:
    """截断标记（多字段形态：``total_items`` 为 {路径: 原始条数}）。"""
    return {
        TRUNCATED_KEY: True,
        "total_items": dict(totals),
        "truncated_fields": sorted(totals),
        "hint": _TRUNCATION_HINT,
    }


def _binary_prefix(items: list[Any], base: dict[str, Any], key: str, total: int, max_bytes: int) -> int:
    """二分最大的可行前缀长度：谓词「截到 n 个元素仍 ≤ 上限」对 n 单调。"""

    def probe(n: int) -> bool:
        candidate = dict(base)
        candidate[key] = items[:n]
        candidate[TRUNCATED_KEY] = True
        candidate["total_items"] = total
        candidate["hint"] = _TRUNCATION_HINT
        return _fit(candidate, max_bytes)

    if probe(0):
        low, high = 0, len(items)
        while low < high:  # 标准二分：找满足谓词的最大 n
            mid = (low + high + 1) // 2
            if probe(mid):
                low = mid
            else:
                high = mid - 1
        return low
    # 连空壳都塞不下：只回最小响应结构，由调用方走 _minimal_fallback
    return -1


def _minimal_fallback(payload: Any, total: int | None = None) -> dict[str, Any]:
    fallback: dict[str, Any] = {TRUNCATED_KEY: True, "hint": _TRUNCATION_HINT}
    if total is not None:
        fallback["total_items"] = total
    if isinstance(payload, dict):
        fallback["truncated_fields"] = sorted(str(key) for key in payload)
    return fallback


def _realign_page_offset(candidate: dict[str, Any], key: str, *, original_len: int, kept: int) -> None:
    """分页响应（含 ``next_offset`` 键）的 items 被截后重算 next_offset，翻页不跳条目。

    分页字段约定 ``next_offset = offset + len(items)``；items 被截到 kept 条后，
    未展示的余量仍在 ``[offset+kept, total)`` 内，翻页起点必须落在那里。
    offset 本身响应不带，可由 next_offset/total 与原始长度反解：

    - ``next_offset`` 非 None（传了 limit）→ ``offset = next_offset - original_len``；
    - ``next_offset`` 为 None（未传 limit，items = 全量[offset:]）→ ``offset = total - original_len``。

    **尾读的字段例外**：``read_logs {tail}`` 的响应把 ``next_offset`` 策略性置
    None（尾读是一次性取尾，永不翻页），与「末页被截后 next_offset 恢复取值」的 None
    在载荷上无法区分——由工具层补 ``paging: "tail"`` 自描述字段消歧。看到该字段就
    保持 None 不动；字段本身是给 agent 的自描述信息，护栏不剥离。
    """
    if key != "items" or "next_offset" not in candidate:
        return
    if candidate.get("paging") == "tail":
        return  # 尾读：next_offset 恒 None，截断后同样不许复活
    total = candidate.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        return
    next_offset = candidate.get("next_offset")
    if next_offset is None:
        offset = total - original_len
    elif isinstance(next_offset, int) and not isinstance(next_offset, bool):
        offset = next_offset - original_len
    else:
        return
    if offset < 0:
        return
    candidate["next_offset"] = (offset + kept) if offset + kept < total else None


def _truncate_dict_multi(
    payload: dict[str, Any], limit: int, *, full_size: int | None = None
) -> dict[str, Any] | None:
    """多列表路径迭代截断：按字节占用从大到小逐路径分配预算，直到整体放得下。

    覆盖两种形态：

    - **顶层多列表**：``{all_mods:[…], disabled_mods:[…], groups:[…]}``，路径即顶层 key；
    - **嵌套载荷**：``ApiResponse.success()`` 的真实形状
      ``{status, message, data:{all_mods:[…], …}}``——顶层没有列表，只扫顶层会得到
      ``sized=[]`` 直接退化成 151B 空壳。沿 dict 有界下降（深度 ≤
      ``_MAX_PATH_DEPTH``）收集到 ``data.all_mods`` 这类路径，截断发生在元素边界、
      ``data`` 子树与其余标量原样保留。

    预算策略：按初始字节占用从大到小二分「最长可留前缀」，**未处理的
    候选路径按空计**，所以大字段先拿满预算、后处理字段拿剩余，主字段不会被其他
    字段的全量数据挤成空列表。

    **粗筛（probe 次数不随路径数失控）**：先把「整体放得下需要腾出多少字节」算成
    ``need = 载荷体积 − 上限 + 标记上界``，再按体积降序累加选出**凑够 need 的最少
    路径集**——只有这些路径进 O(log n) 二分，其余列表原样保留（小列表因此不会被
    白白置空）。``need`` 凑不够（= 所有列表清空仍放不下，标量超限）直接返回 None，
    一次 probe 都不发，交回调用方走 :func:`_minimal_fallback`。

    判定用**预留上界标记**：每次 probe 都按「被选中的路径全部被截」的终态标记计
    体积——最终标记（只含实际被截字段）只会更小，所以任一 probe 判过的 fit 对
    最终产物必然成立（标记体积随字段数增长，若 probe 各用当下标记，后处理字段
    的判定会因标记变大而失效，实测会把整体挤回超限）。

    返回 None = 放不下（标量/标记超限，或无任何可截列表），交调用方走
    :func:`_minimal_fallback`。
    """
    candidate = {key: value for key, value in payload.items() if key != TRUNCATED_KEY}
    if full_size is None:
        full_size = _size_of(candidate)
    if full_size <= limit:
        return None  # 防御：入口 _fit 已判超限，理论不可达
    paths = _collect_list_paths(candidate)
    if not paths:
        return None
    sized = [((path), _size_of(_get_at(candidate, path))) for path in paths]
    sized.sort(key=lambda kv: kv[1], reverse=True)
    totals_full = {path: len(_get_at(candidate, path)) for path, _size in sized}

    # 粗筛：need = 超出的字节 + 预留标记上界（按全部候选路径算，是终态标记的上界）
    marker_upper = _size_of(_marker_block({_path_str(p): totals_full[p] for p, _ in sized}))
    need = full_size - limit + marker_upper
    selected: list[tuple[tuple[str, ...], int]] = []
    acc = 0
    for path, size in sized:
        if acc >= need:
            break
        selected.append((path, size))
        acc += size
    if acc < need:
        return None  # 全部列表置空仍放不下 → 标量/已定前缀总量超限

    selected_paths = {path for path, _size in selected}
    reserved = {_path_str(path): totals_full[path] for path in selected_paths}
    processed: dict[tuple[str, ...], list[Any]] = {}

    def fits(state: dict[str, Any]) -> bool:
        probe = dict(state)
        probe.update(_marker_block(reserved))  # 预留上界，最终标记 ⊆ 此形状
        return _fit(probe, limit)

    for path, _size in selected:
        items = _get_at(candidate, path)

        def probe(n: int, path: tuple[str, ...] = path, items: list[Any] = items) -> bool:
            trial = candidate
            for other in selected_paths:
                if other == path:
                    value: list[Any] = items[:n]
                elif other in processed:
                    value = processed[other]  # 已定前缀
                else:
                    value = []  # 未处理的候选路径按「将被置空」计
                trial = _set_at(trial, other, value)
            return fits(trial)

        if not probe(0):
            return None  # 防御：粗筛已保证「全空」状态放得下
        low, high = 0, len(items)
        while low < high:
            mid = (low + high + 1) // 2
            if probe(mid):
                low = mid
            else:
                high = mid - 1
        candidate = _set_at(candidate, path, items[:low])
        processed[path] = items[:low]

    # 只报告实际被截的路径（保留数 < 原始数）；比 probe 的预留上界小，fit 仍成立。
    truncated_totals = {
        _path_str(path): original
        for path, original in totals_full.items()
        if path in selected_paths and len(_get_at(candidate, path)) < original
    }
    if not truncated_totals:
        return None  # 防御：没有任何路径实际被截却仍进到这里（入口已判超限，不应发生）
    candidate.update(_marker_block(truncated_totals))
    if "items" in truncated_totals:
        _realign_page_offset(
            candidate,
            "items",
            original_len=truncated_totals["items"],
            kept=len(candidate.get("items") or []),
        )
    return candidate


def guard_payload(payload: Any, *, max_bytes: int | None = None) -> str:
    """把工具返回值序列化成 JSON 字符串，超限则结构化截断。

    截断策略（产物恒为合法 JSON）：

    1. 字典 → 找**顶层**最大列表字段，按**元素边界**二分最长前缀，补
       ``{__truncated__, total_items, hint}`` 响应字段（其余字段保留）；
       该字段整段置空仍放不下（多个大字段同超）或顶层根本没有列表
       （``ApiResponse.success()`` 的嵌套形状 ``data.all_mods``）→
       沿有界深度的列表**路径**迭代截断（:func:`_truncate_dict_multi`）；
    2. 顶层列表 → 同样二分，包成 ``{items, __truncated__, total_items, hint}``；
    3. 无可截列表（纯标量/超深嵌套）→ 最小响应结构 + ``truncated_fields`` 清单。

    **``total_items`` 双态（消费方判别方式）**：

    - **单字段形态**（顶层最大列表被二分截断，或顶层列表本身）：``total_items``
      是 ``int``（被截列表的原始条数），载荷里**没有** ``truncated_fields``；
    - **多字段/路径形态**：``total_items`` 是 ``{路径: 原始条数}`` 的 dict，且**必有**
      ``truncated_fields``（同一批路径字符串的升序列表）。
      判别方式：``"truncated_fields" in payload`` 最稳，或
      ``isinstance(total_items, dict)``。路径用 ``.`` 连接（``data.all_mods``），
      顶层 key 就是一段路径，因此两种形态的键空间一致。

    含 ``next_offset`` 的分页响应在 items 被截后重算 next_offset
    （:func:`_realign_page_offset`），保证按分页字段翻页不跳过被截条目；
    带 ``paging: "tail"`` 的尾读响应例外——``next_offset`` 恒保持 None。
    超大单元素（一个元素就超限）会退化为 ``items=[]`` 的最小响应结构——依然合法 JSON。

    **与保险层的关系**：未传 ``max_bytes`` 时用 :func:`semantic_max_output`
    （＝保险层 × 0.75）收住，再用 :func:`frame_cost` 复核整帧成本 ≤
    :func:`max_output`；超了就按实际转义密度把预算折算回整帧基准重截
    （最多 :func:`_FRAME_RETRIES` 次）。这样「语义层放行的合法 JSON」永远不会被
    保险层从中间切破。显式传 ``max_bytes`` 时以它为语义预算，但保险层基准的复核
    同样生效（它本就是不可逾越的上限）。
    """
    if isinstance(payload, dict) and TRUNCATED_KEY in payload:
        # 重复护栏：陈旧标记作废重算，否则 _size_of 会把它算进体积。
        payload = {key: value for key, value in payload.items() if key != TRUNCATED_KEY}
    limit = max_bytes if max_bytes is not None else semantic_max_output()
    full_size = _size_of(payload)
    text = _guard(payload, limit, full_size)
    insurance = max_output()
    if frame_cost(text) > insurance:
        text = _refit_for_insurance(payload, limit, full_size, insurance)
    return text


def _guard(payload: Any, limit: int, full_size: int) -> str:
    """按语义预算 ``limit`` 截断并序列化（:func:`guard_payload` 的主体）。"""
    if full_size <= limit:
        return _dumps(payload)

    if isinstance(payload, dict):
        key = _largest_list_key(payload)
        if key is not None:
            items = payload[key]
            base = dict(payload)
            base.pop(TRUNCATED_KEY, None)
            kept = _binary_prefix(items, base, key, len(items), limit)
            if kept >= 0:
                base[key] = items[:kept]
                base[TRUNCATED_KEY] = True
                base["total_items"] = len(items)
                base["hint"] = _TRUNCATION_HINT
                _realign_page_offset(base, key, original_len=len(items), kept=kept)
                return _dumps(base)
        multi = _truncate_dict_multi(payload, limit, full_size=full_size)
        if multi is not None:
            return _dumps(multi)
        return _dumps(_minimal_fallback(payload))

    if isinstance(payload, list):
        kept = _binary_prefix(payload, {}, "items", len(payload), limit)
        if kept >= 0:
            envelope = {
                "items": payload[:kept],
                TRUNCATED_KEY: True,
                "total_items": len(payload),
                "hint": _TRUNCATION_HINT,
            }
            return _dumps(envelope)
        return _dumps(_minimal_fallback(payload, total=len(payload)))

    return _dumps(_minimal_fallback(payload))


def _refit_for_insurance(payload: Any, limit: int, full_size: int, insurance: int) -> str:
    """语义产物的整帧成本仍超保险层时，按实际转义密度折算预算重截。

    密度 ``ρ = frame_cost / 文本字节数``（典型 1.07~1.15，引号/反斜杠密集的
    极端载荷最高 1.85，理论上界 2.0）。把整帧预算 ``(insurance − 126)`` 除以 ρ 再
    留 3% 余量就是新的语义预算：新产物的整帧成本 ≈ 126 + ρ × 新预算 ≤ 保险层。
    两次仍收不住（密度随截断内容漂移的极端情形）→ 最小响应结构降级，宁可少给数据
    也不给破损 JSON。
    """
    budget = limit
    text = ""
    for _ in range(_FRAME_RETRIES):
        text = _guard(payload, budget, full_size)
        cost = frame_cost(text)
        if cost <= insurance:
            return text
        raw = max(len(text.encode("utf-8")), 1)
        density = (cost - _TOOL_RESULT_OVERHEAD) / raw
        budget = max(1, int((insurance - _TOOL_RESULT_OVERHEAD) * _FRAME_SAFETY / density))
    if frame_cost(text) > insurance:
        fallback = _dumps(_minimal_fallback(payload))
        if frame_cost(fallback) <= insurance:
            return fallback
    return text


def page_envelope(
    items: list[Any],
    *,
    total: int,
    offset: int = 0,
    limit: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """查询结果分页响应：结构即下一步指令。

    ``next_offset`` 非 None → 还有下一页；``hint`` 恒为翻页指引常量。
    ``extra`` 用于携带 token 等工具级元数据（如 version_token）。
    """
    offset = max(0, int(offset))
    consumed = offset + len(items)
    envelope: dict[str, Any] = {
        "total": int(total),
        "items": items,
        "next_offset": consumed if consumed < int(total) else None,
        "hint": _PAGE_HINT,
    }
    if extra:
        envelope.update(extra)
    return envelope


def build_error_text(
    message: str,
    next_actions: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """错误响应结构：因果文案 + nextActions[]，产物为单个合法 JSON 字符串。

    工具层以 ``ToolError`` 抛出 → MCP ``isError:true``；错误信息随本次调用返回，
    因此必须自带自纠指令，且同样遵守「不拼接第三方内容」的防注入边界
    （message 由调用方保证只含路径/计数/包名等受控信息）。``extra`` 用于携带
    结构化细节（如 apply_load_order_ops 的逐条 conflicts）。
    """
    payload: dict[str, Any] = {"status": "error", "message": str(message)}
    if next_actions:
        payload["nextActions"] = [str(action) for action in next_actions]
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
