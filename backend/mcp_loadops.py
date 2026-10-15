# backend/mcp_loadops.py
"""加载顺序增量 ops 的纯函数代数层（``apply_load_order_ops`` 的核心实现）。

本模块存在的意义：

- **交互形态**：采用「读全量 → 客户端改 → 传回全量」之外的增量方式。本模块把
  「校验 + 应用」压缩成一次小载荷调用：工具层读当前顺序 → 调
  :func:`apply_ops` → 单次落盘，响应只回 ``applied/summary/conflicts``，
  永不回传全表；
- **原子性**：「服务端在锁内读当前 → 校验全部 ops →
  应用 → 单次落盘；任一 op 失败 → 整体拒绝，逐条返回冲突原因」。因此这里
  顺序校验每条 op，失败只记 :class:`Conflict`、**不动该 op 的工作副本**，
  但**继续校验后续 op**——让调用方一次看到全部独立冲突（逐条返回），最后
  只要有任一冲突就整体拒绝；
- **零依赖**：仅标准库、不 import 任何 backend 模块 → 不拉起 DB/配置，
  工具层、单测、桥侧测试都能直接 import。

约定：

- packageId 身份比较一律 ``.casefold()``（大小写不敏感），但输出串保留
  ``current_ids`` / ops 里的原始大小写；
- ``ok=True`` 时 ``summary`` **恒含全部六个键**（无变化的计 0：inserted /
  removed / moved / activated / deactivated / skipped）；``ok=False`` 时
  ``summary={}``、``applied=[]``、``ordered`` 为入参的副本；
- ``ops`` 参数本身的结构性误用（非列表 / 空 / 超上限 / 非 dict 项）抛
  ``ValueError``（调用方转 MCP 错误）；单条 op 的语义失败**永不抛**，
  一律降级为 :class:`Conflict`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_OPS",
    "ApplyResult",
    "Conflict",
    "apply_ops",
]

# ops 数组条数上限（锚定的 ops 数组必须有条数上限）。
MAX_OPS = 100

# summary 的六个计数键；ok=True 时恒全部出现（0 也不省略，保持统计方式一致）。
_SUMMARY_KEYS = ("inserted", "removed", "moved", "activated", "deactivated", "skipped")

# insert/move 锚的合法形态：恰好一个键。
_ANCHOR_KEYS = ("after", "before", "index")


@dataclass(frozen=True, slots=True)
class Conflict:
    """单条冲突定位（逐条返回冲突原因）。

    ``index`` 是该 op 在 ``ops`` 数组中的下标；``-1`` 保留给整数组级别的
    结构性问题（本层对结构性误用直接抛 ``ValueError``，故正常路径不会
    产生 ``-1``）。
    """

    index: int
    op: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好的扁平形状（MCP 响应直接序列化用）。"""
        return {"index": self.index, "op": self.op, "reason": self.reason}


@dataclass(slots=True)
class ApplyResult:
    """``apply_ops`` 的结果结构（{ok, applied, summary, conflicts}）。

    - ``ok=False``：``ordered`` 是入参 ``current_ids`` 的副本（什么都没改），
      ``applied=[]``、``summary={}``，全部冲突见 ``conflicts``；
    - ``ok=True``：``ordered`` 是完全应用后的新顺序，``summary`` 恒含六个键。
    """

    ok: bool
    ordered: list[str]
    applied: list[str]
    summary: dict[str, int]
    conflicts: list[Conflict]


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Outcome:
    """单条 op 的处理结果：errors 非空 = 该 op 整体失败（工作副本未动）。"""

    errors: list[str]
    line: str | None
    counts: dict[str, int]


def _err(*reasons: str) -> _Outcome:
    return _Outcome(errors=list(reasons), line=None, counts={})


def _ok(line: str | None, **counts: int) -> _Outcome:
    return _Outcome(errors=[], line=line, counts=counts)


def _find(work: list[str], ref: str) -> int:
    """大小写不敏感地找首个下标；找不到返回 -1（身份比较一律 casefold）。"""
    key = ref.casefold()
    for i, item in enumerate(work):
        if item.casefold() == key:
            return i
    return -1


def _dedupe(ids: list[str]) -> list[str]:
    """按 casefold 去重、保序（remove 的重复 id 只应删一次）。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in ids:
        key = raw.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def _anchor_shape(anchor: Any, label: str) -> str | None:
    """锚形态校验：必须是「恰好一个 after/before/index 键」的 dict。

    缺键、多键（extra keys）、非 dict 都在这里拦掉——形态错了就没必要
    往下解析值。
    """
    if not isinstance(anchor, dict):
        return f"{label} 必须是对象，形如 {{after: id}} / {{before: id}} / {{index: N}}"
    if len(anchor) != 1 or not any(key in _ANCHOR_KEYS for key in anchor):
        return f"{label} 必须且只能含 after/before/index 之一"
    return None


def _resolve(
    key: str,
    value: Any,
    work: list[str],
    *,
    hi: int,
    label: str,
) -> tuple[int, str, str | None]:
    """把锚解析成插入位置。返回 ``(pos, resolved_ref, error)``。

    ``hi`` 是 ``index`` 键的合法上界（含）：insert 是 ``len(work)``（含末尾
    追加位），move 是 ``len(work) - 1``（按「先移除再插入」后的结果列表算）。
    ``resolved_ref`` 取工作副本里的原始大小写。
    """
    if key == "index":
        if isinstance(value, bool) or not isinstance(value, int):
            return 0, "", f"{label}.index 必须是整数"
        if value < 0 or value > hi:
            return 0, "", f"{label}.index {value} 超出范围 [0, {hi}]"
        return value, "", None
    # after / before：引用必须在工作副本里存在（锚优先于行号）
    if not isinstance(value, str) or not value:
        return 0, "", f"{label}.{key} 必须是非空字符串"
    idx = _find(work, value)
    if idx < 0:
        return 0, "", f"{label} 引用不存在：{value}"
    pos = idx + 1 if key == "after" else idx
    return pos, work[idx], None


# ---------------------------------------------------------------------------
# 五种 op 的处理器：先整体校验、后动工作副本（单条 op 内部也保持全有全无）
# ---------------------------------------------------------------------------


def _op_insert(op: dict[str, Any], work: list[str]) -> _Outcome:
    raw_id = op.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        return _err("insert.id 必须是非空字符串")
    if _find(work, raw_id) >= 0:
        return _err(f"packageId 已存在：{raw_id}")

    anchor = op.get("anchor")
    if anchor is None:  # 锚可选（含显式 null）→ 默认追加到末尾
        pos, line = len(work), f"insert {raw_id} at end"
    else:
        shape_err = _anchor_shape(anchor, "anchor")
        if shape_err:
            return _err(shape_err)
        key, value = next(iter(anchor.items()))
        pos, resolved, err = _resolve(key, value, work, hi=len(work), label="anchor")
        if err:
            return _err(err)
        if key == "index":
            line = f"insert {raw_id} -> index {pos}"
        else:
            line = f"insert {raw_id} {key} {resolved}"
    work.insert(pos, raw_id)
    return _ok(line, inserted=1)


def _op_remove(op: dict[str, Any], work: list[str]) -> _Outcome:
    ids = op.get("ids")
    if not isinstance(ids, list) or not ids:
        return _err("remove.ids 必须是非空列表")
    if any(not isinstance(raw, str) for raw in ids):
        return _err("remove.ids 必须全部为字符串")

    unique = _dedupe(ids)
    missing = [raw for raw in unique if _find(work, raw) < 0]
    if missing:
        # 任一 id 缺失 → 整条 op 失败，一个缺失 id 一条冲突（逐条返回）
        return _err(*(f"packageId 不存在：{raw}" for raw in missing))

    removed: list[str] = []
    for raw in unique:
        removed.append(work.pop(_find(work, raw)))
    return _ok("remove " + ", ".join(removed), removed=len(removed))


def _op_move(op: dict[str, Any], work: list[str]) -> _Outcome:
    raw_id = op.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        return _err("move.id 必须是非空字符串")
    id_idx = _find(work, raw_id)
    if id_idx < 0:
        return _err(f"packageId 不存在：{raw_id}")
    moved = work[id_idx]  # 输出保留工作副本里的原始大小写

    to = op.get("to")
    if not isinstance(to, dict):
        return _err("move 缺少 to，形如 {index: N} / {after: id} / {before: id}")
    shape_err = _anchor_shape(to, "to")
    if shape_err:
        return _err(shape_err)
    key, value = next(iter(to.items()))

    resolved = ""
    if key == "index":
        # 最终位置是「结果列表」里的位置 → 上界按移除后长度算 = len(work) - 1
        pos, _, err = _resolve("index", value, work, hi=len(work) - 1, label="to")
        if err:
            return _err(err)
    else:
        if isinstance(value, str) and value.casefold() == moved.casefold():
            return _err(f"不能以自身为锚：{moved}")  # 自锚会让位置语义退化
        # 引用在「移除被移动项之后」的副本里解析（防止 ref 恰是被移走的项）
        probe = work[:id_idx] + work[id_idx + 1 :]
        pos, resolved, err = _resolve(key, value, probe, hi=len(probe), label="to")
        if err:
            return _err(err)

    work.pop(id_idx)
    work.insert(pos, moved)
    if key == "index":
        return _ok(f"move {moved} -> index {pos}", moved=1)
    return _ok(f"move {moved} {key} {resolved}", moved=1)


def _op_activate(op: dict[str, Any], work: list[str]) -> _Outcome:
    ids = op.get("ids")
    if not isinstance(ids, list) or not ids:
        return _err("activate.ids 必须是非空列表")
    if any(not isinstance(raw, str) for raw in ids):
        return _err("activate.ids 必须全部为字符串")

    added: list[str] = []
    skipped = 0
    for raw in ids:
        if _find(work, raw) >= 0:
            skipped += 1  # 已在列表里 → 幂等 no-op，计 skipped（不是冲突）
        else:
            work.append(raw)  # 未启用的按给定顺序追加到末尾
            added.append(raw)
    line = "activate " + ", ".join(added) if added else None
    return _ok(line, activated=len(added), skipped=skipped)


def _op_deactivate(op: dict[str, Any], work: list[str]) -> _Outcome:
    ids = op.get("ids")
    if not isinstance(ids, list) or not ids:
        return _err("deactivate.ids 必须是非空列表")
    if any(not isinstance(raw, str) for raw in ids):
        return _err("deactivate.ids 必须全部为字符串")

    removed: list[str] = []
    skipped = 0
    for raw in ids:
        idx = _find(work, raw)
        if idx < 0:
            skipped += 1  # 本就未启用 → 幂等 no-op
        else:
            removed.append(work.pop(idx))
    line = "deactivate " + ", ".join(removed) if removed else None
    return _ok(line, deactivated=len(removed), skipped=skipped)


_HANDLERS = {
    "insert": _op_insert,
    "remove": _op_remove,
    "move": _op_move,
    "activate": _op_activate,
    "deactivate": _op_deactivate,
}


def _check_structure(ops: Any) -> None:
    """ops 参数级别的结构性误用 → ValueError（调用方转 MCP 错误）。"""
    if not isinstance(ops, list):
        raise ValueError("ops 必须是列表")
    if not ops:
        raise ValueError("ops 不能为空")
    if len(ops) > MAX_OPS:
        raise ValueError(f"ops 条数 {len(ops)} 超过上限 {MAX_OPS}")
    for i, entry in enumerate(ops):
        if not isinstance(entry, dict):
            raise ValueError(f"ops[{i}] 必须是对象")


def apply_ops(current_ids: list[str], ops: list[dict]) -> ApplyResult:
    """对加载顺序做增量 ops 的全有全无应用。

    逐条顺序校验、在工作副本上应用；任一 op 失败只记 :class:`Conflict`
    并**跳过该 op**（工作副本保持不变），随后继续校验后续 op 以便一次性
    返回全部独立冲突。最终：有冲突 → 整体拒绝（``ordered`` 为入参副本）；
    无冲突 → ``ok=True`` 与完整新顺序。

    结构性误用 ``ops``（非列表 / 空 / 超 ``MAX_OPS`` / 非 dict 项）抛
    ``ValueError``；单条 op 的语义失败永不抛。
    """
    _check_structure(ops)
    if not isinstance(current_ids, list):
        raise ValueError("current_ids 必须是列表")

    original = list(current_ids)  # 失败时原样返回的副本（不给调用方共享可变引用）
    work = list(current_ids)
    conflicts: list[Conflict] = []
    applied: list[str] = []
    summary = dict.fromkeys(_SUMMARY_KEYS, 0)

    for index, entry in enumerate(ops):
        name = entry.get("op")
        handler = _HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            reason = "缺少 op 字段" if "op" not in entry else f"未知 op：{name}"
            conflicts.append(Conflict(index=index, op="unknown", reason=reason))
            continue

        outcome = handler(entry, work)
        if outcome.errors:
            for reason in outcome.errors:
                conflicts.append(Conflict(index=index, op=name, reason=reason))
            continue  # 该 op 未动工作副本；继续校验后续 op（逐条返回冲突）
        if outcome.line:
            applied.append(outcome.line)
        for key, value in outcome.counts.items():
            summary[key] += value

    if conflicts:
        return ApplyResult(
            ok=False,
            ordered=original,
            applied=[],
            summary={},
            conflicts=conflicts,
        )
    return ApplyResult(ok=True, ordered=work, applied=applied, summary=summary, conflicts=[])
