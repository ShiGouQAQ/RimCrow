# backend/mcp_deps.py
"""``get_mod_dependencies`` 的实现体：依赖/顺序关系图的遍历、环检测与输出格式化。

职责分三层：

1. **图核心**（``walk`` / ``build_tree`` / 两个 dataclass）是纯函数，只依赖标准库
   —— 图核心函数不调用任何 backend 模块（本模块顶层 import 的 dao/managers
   仅适配层使用），纯函数单测不需要数据库与规则引擎；
2. **适配层** ``load_edge_index`` 是唯一接触 ``ModDAO`` / ``RuleManager`` 的地方：
   把「当前档案可见模组 + 生效规则」压平成 casefold 索引 + 启用集合；
3. **工具入口** ``query_dependencies`` 做参数校验、起点解析与响应结构组装，
   输出形状对齐 ``backend.mcp_tool_specs.get_mod_dependencies`` 的 docstring
   （edges 平铺表 ``{from,to,relation,depth}``、tree 仅 depth<=2、
   截断带常量 hint）。

边的规范化方向（canonical orientation）：``from`` 永远是声明方、``to`` 永远是被引用方。
模组 M 声明「依赖 X / loadAfter X」→ ``M --dependencies|load_after--> X``。
方向（up/down/both）只是**遍历侧**语义：down=顺着自己的声明走，up=谁指向我
（删模组前的影响分析）。同一条边在任何 direction 下形态一致，
去重、环检测、树形化都不用关心查询方向——与工具 desc 逐字一致。

为什么图里不放 ``incompatible``：不兼容是**互斥关系**，不是加载顺序边——
它既不代表「谁在谁之前」，也不构成依赖；入图会把冲突误报成可遍历的顺序关系，
把「这两个模组不能共存」渲染成一条看起来像依赖的边。不兼容明细归
``get_mod_rules``（两个工具的职责边界已写进各自 desc）。

截断 ``hint`` 必须是常量（``TRUNCATED_HINT``）：MCP 响应会进入模型上下文，
动态拼接模组名/路径等于开放注入面，这里只允许给固定的操作指引。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from backend.database.dao import ModDAO
from backend.load_order.package_tokens import parse_package_token
from backend.managers.mgr_load_order import LoadOrderManager
from backend.managers.mgr_rules import RuleManager, resolve_mod_rules

__all__ = [
    "MAX_DEPTH",
    "MAX_EDGES",
    "MAX_CYCLES",
    "TRUNCATED_HINT",
    "RuleEdge",
    "WalkResult",
    "walk",
    "build_tree",
    "load_edge_index",
    "query_dependencies",
]

# ==== 常量 ====================================================================

MAX_DEPTH = 5
MAX_EDGES = 500
MAX_CYCLES = 20

# 注入边界：hint 是常量，禁止拼接模组名/路径（见模块 docstring）。
TRUNCATED_HINT = "结果已截断（单次返回达到上限）：请降低 depth 或缩小 direction 范围后重试。"

# 图里的三类顺序/依赖关系；incompatible 刻意排除（互斥不是顺序边）。
_GRAPH_RELATIONS = ("dependencies", "load_after", "load_before")

_DIRECTIONS = frozenset({"down", "up", "both"})

# 嵌套 tree 的层数上限（限制返回体积；深树用 edges 分步查）。
_TREE_MAX_DEPTH = 2


# ==== 数据结构 ================================================================


@dataclass(frozen=True, slots=True)
class RuleEdge:
    """一条规范化方向的规则边。

    ``source`` 是声明方 packageId（保留原始大小写），``target`` 是被引用方；
    ``relation`` ∈ ``_GRAPH_RELATIONS``；``force`` 来自规则的 ``is_force``。
    """

    source: str
    target: str
    relation: str
    force: bool = False


@dataclass(slots=True)
class WalkResult:
    """``walk`` 的产物：平铺边表（BFS 发现序）+ 环报告 + 截断/访问统计。

    ``edges`` 每项为 ``{"from","to","relation","depth"}``（规范化方向，
    ``depth`` 是发现该边的 BFS 层级，1..depth）；``cycles`` 为 casefold 后的
    排序节点列表，无环时为空；``start`` 为 casefold 后的起点 id（与索引键一致，
    原始大小写由 ``query_dependencies`` 另行还原）；``visited`` 含起点
    （起点不存在时为 0）。
    """

    start: str
    missing_start: bool
    edges: list[dict[str, Any]]
    cycles: list[list[str]]
    truncated: bool
    visited: int


# ==== 图核心（纯函数：零 backend 依赖，单测无需数据库）========================


def _edge_key(edge: RuleEdge) -> tuple[str, str, str]:
    """去重键 = 大小写折叠的 (from, to, relation)。

    force 不参与：规则引擎按 (类别, target) 合并，同一条生效规则只有一条，
    不存在同键不同 force 的情况；重复只可能来自索引的双键登记或调用方手搭的重复项。
    """
    return (edge.source.casefold(), edge.target.casefold(), edge.relation)


def _build_adjacency(
    edge_index: dict[str, list[RuleEdge]],
) -> tuple[dict[str, list[RuleEdge]], dict[str, list[RuleEdge]]]:
    """把 edge_index 拉平成「出边 / 入边」邻接表，按边本身去重。

    适配层会把同一条边登记在 source 与 target 两个键下（让 up 遍历 O(1) 命中），
    手搭索引可能只登记 source——这里统一按边去重，使 up/down 遍历都不依赖
    登记方式，同时保证同一条边不会被收集两次。
    """
    out_adj: dict[str, list[RuleEdge]] = {}
    in_adj: dict[str, list[RuleEdge]] = {}
    seen: set[tuple[str, str, str]] = set()
    for bucket in edge_index.values():
        for edge in bucket or ():
            key = _edge_key(edge)
            if key in seen:
                continue
            seen.add(key)
            out_adj.setdefault(edge.source.casefold(), []).append(edge)
            in_adj.setdefault(edge.target.casefold(), []).append(edge)
    return out_adj, in_adj


def _tarjan_scc(adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan 强连通分量（迭代版，无第三方库）。

    迭代实现而非递归：环可能出现在深链上，迭代不占 Python 递归栈。
    节点按字典序从 ``sorted(adjacency)`` 起步，保证分量产出顺序确定。
    """
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in sorted(adjacency):
        if root in index:
            continue
        # work 栈：(节点, 下一个待访问的孩子下标)；cursor==0 时初始化该节点。
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, cursor = work[-1]
            if cursor == 0:
                index[node] = lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = adjacency.get(node, ())
            if cursor < len(children):
                work[-1] = (node, cursor + 1)
                child = children[cursor]
                if child not in index:
                    work.append((child, 0))
                elif child in on_stack:
                    # 回边/指向仍在栈上的节点：用它的发现序压低本节点 lowlink
                    lowlink[node] = min(lowlink[node], index[child])
            else:
                work.pop()
                if lowlink[node] == index[node]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    components.append(component)
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
    return components


def _detect_cycles(edges: list[dict[str, Any]]) -> list[list[str]]:
    """在**已收集的子图**上做环检测：SCC 大小 >1 的互指环 + 自环（from==to）。

    节点一律 casefold 后比较：声明方与被引用方的大小写可能不同
    （如 ModA 声明 loadBefore moda 的旧数据），用原始字符串会让本应互指的环漏检。
    结果排序、截断到 ``MAX_CYCLES``，保证输出确定且有界。
    """
    adjacency: dict[str, list[str]] = {}
    self_loops: list[str] = []
    seen_self: set[str] = set()
    for edge in edges:
        source_cf = str(edge.get("from") or "").casefold()
        target_cf = str(edge.get("to") or "").casefold()
        if not source_cf or not target_cf:
            continue
        adjacency.setdefault(source_cf, []).append(target_cf)
        adjacency.setdefault(target_cf, [])
        if source_cf == target_cf and source_cf not in seen_self:
            seen_self.add(source_cf)
            self_loops.append(source_cf)

    cycles = [sorted(component) for component in _tarjan_scc(adjacency) if len(component) > 1]
    # Tarjan 对自环给出的是 size==1 的分量（会被上面的 len>1 过滤），单独补报
    cycles.extend([node] for node in self_loops)
    cycles.sort(key=lambda component: tuple(component))
    return cycles[:MAX_CYCLES]


def walk(
    edge_index: dict[str, list[RuleEdge]],
    start: str,
    *,
    direction: str,
    depth: int,
    max_edges: int = MAX_EDGES,
) -> WalkResult:
    """按 direction/depth 在边索引上做 BFS 收集，返回平铺边表 + 环报告。

    - ``direction``：down=出边（source==current → target）、
      up=入边（target==current → source）、both=双向；
    - ``depth``：边的 BFS 层级——起点外扩的边记 1，最远记 depth（1..MAX_DEPTH）；
    - ``max_edges``：硬上限，收集条数绝不超过；因上限停手且还有边没收集时
      ``truncated=True``（正好收满且无剩余 → False）；
    - 环：对收集到的子图跑 Tarjan（``_detect_cycles``），自环单列，不死循环；
    - 输出序 = BFS 发现序，同层内按 (from, to, relation) 排序 ⇒
      与索引登记顺序无关，结果确定（去重见 ``_build_adjacency``）。
    """
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction 无效: {direction!r}，仅支持 down/up/both")
    if not isinstance(depth, int) or isinstance(depth, bool) or not (1 <= depth <= MAX_DEPTH):
        raise ValueError(f"depth 必须是 1..{MAX_DEPTH} 的整数: {depth!r}")

    start_cf = str(start or "").casefold()
    out_adj, in_adj = _build_adjacency(edge_index)
    known_nodes = {key.casefold() for key in edge_index} | set(out_adj) | set(in_adj)
    missing_start = start_cf not in known_nodes

    collected: dict[tuple[str, str, str], dict[str, Any]] = {}
    visited: set[str] = set()
    if not missing_start:
        visited.add(start_cf)
    queue: deque[tuple[str, int]] = deque([(start_cf, 0)])
    truncated = False

    while queue:
        node_cf, level = queue.popleft()
        if level >= depth:
            continue  # 再外扩一层就超 depth，本节点的边不属于本次查询范围

        candidates: list[RuleEdge] = []
        if direction in ("down", "both"):
            candidates.extend(out_adj.get(node_cf, ()))
        if direction in ("up", "both"):
            candidates.extend(in_adj.get(node_cf, ()))
        # 自环在出边与入边里各出现一次；先按边键去重，再排序定序
        uniq: dict[tuple[str, str, str], RuleEdge] = {}
        for edge in candidates:
            uniq.setdefault(_edge_key(edge), edge)

        for edge in sorted(uniq.values(), key=_edge_key):
            key = _edge_key(edge)
            if key in collected:
                # 已收集过：它的另一端在首次收集时必然已入队，无需重复处理
                continue
            if len(collected) >= max_edges:
                truncated = True  # 上限挡住了一条真实存在的边 → 前沿还有边没收
                queue.clear()
                break
            edge_level = level + 1
            collected[key] = {
                "from": edge.source,
                "to": edge.target,
                "relation": edge.relation,
                "depth": edge_level,
            }
            other = edge.target.casefold() if edge.source.casefold() == node_cf else edge.source.casefold()
            if other not in visited:
                visited.add(other)
                queue.append((other, edge_level))

    edges = list(collected.values())
    return WalkResult(
        start=start_cf,
        missing_start=missing_start,
        edges=edges,
        cycles=_detect_cycles(edges),
        truncated=truncated,
        visited=len(visited),
    )


def build_tree(start: str, edges: list[dict[str, Any]], *, depth: int) -> dict[str, Any]:
    """把平铺边表折成嵌套树：``{"id", "children": [{"id","relation","depth","children":...}]}``。

    子节点方向**背离起点**：相对父节点看边向——``edge["from"] == parent`` 则
    子节点是 ``edge["to"]``，否则（``edge["to"] == parent``）子节点是 ``edge["from"]``。
    只展开 ``edge["depth"] == 树层级`` 的边（walk 的 BFS 层级与树层级对齐），
    因此 depth 参数与 walk 一致时树天然有界。

    防死循环：子节点已出现在当前路径上时，发一个 ``__cycle__`` 标记节点且不展开
    其子树——环在树里以叶子形式可见，避免无限递归。
    """
    start_id = str(start or "")

    def expand(node_id: str, level: int, path: frozenset[str]) -> list[dict[str, Any]]:
        if level >= depth:
            return []
        node_cf = node_id.casefold()
        children: list[dict[str, Any]] = []
        seen_children: set[tuple[str, str]] = set()
        for edge in edges:
            if edge.get("depth") != level + 1:
                continue
            from_id = str(edge.get("from") or "")
            to_id = str(edge.get("to") or "")
            relation = str(edge.get("relation") or "")
            if from_id.casefold() == node_cf:
                child_id = to_id
            elif to_id.casefold() == node_cf:
                child_id = from_id
            else:
                continue  # 不与当前父节点相邻，或层级对不上
            child_cf = child_id.casefold()
            dedupe_key = (child_cf, relation)
            if dedupe_key in seen_children:
                continue  # 同一对节点的多条同关系边（如 up/down 双向各收集一次）只挂一个
            seen_children.add(dedupe_key)
            if child_cf in path:
                # 路径上已出现 → 环标记叶子，不展开
                children.append({
                    "id": child_id,
                    "relation": relation,
                    "depth": edge.get("depth"),
                    "__cycle__": True,
                    "children": [],
                })
                continue
            children.append({
                "id": child_id,
                "relation": relation,
                "depth": edge.get("depth"),
                "children": expand(child_id, level + 1, path | {child_cf}),
            })
        return children

    return {"id": start_id, "children": expand(start_id, 0, frozenset({start_id.casefold()}))}


# ==== 后端适配层（唯一接触 DB / 规则引擎的地方）===============================


def load_edge_index(api: Any) -> tuple[dict[str, list[RuleEdge]], set[str]]:
    """把当前档案的「可见模组 + 生效规则」压成图索引 + 启用 id 集合。

    - 可见模组来自 ``ModDAO.get_profile_mods(api.active_context)``（档案可见性事实来源）；
    - 规则走 ``resolve_mod_rules`` 统一入口：优先复用 ``api.sorter.rule_mgr``
      （与主列表同一份规则缓存），没有才构造一次 ``RuleManager``——构造会读规则
      文件，绝不能每个模组来一次；
    - 只收 ``effective is not False`` 的规则：被高优先级来源遮蔽的规则不参与排序，
      也不该进依赖图（明细由 get_mod_rules 的 ``shadowed_rules`` 解释）；
    - ``incompatible`` 不入图（互斥关系不是顺序边，见模块 docstring）；
    - target 即使不在可见模组里也保留（未安装/外部引用 = 悬空节点），且把边同时
      登记在 source 与 target 两个键下：up 遍历可 O(1) 找到入边，查询起点也能
      命中这些只被引用过的 id；没有任何规则的可见模组也占一个空键（孤立已知节点）；
    - 启用集合镜像 ``AIToolExecutor._get_active_mod_id_set``：
      ``read_active_mods().active_mods`` 是包 token，用 ``parse_package_token``
      归一到 canonical id（剥 ``_steam`` / ``_local`` 后缀）。
    """
    context = getattr(api, "active_context", None)
    rule_mgr = getattr(getattr(api, "sorter", None), "rule_mgr", None)
    if rule_mgr is None:
        rule_mgr = RuleManager(context)

    edge_index: dict[str, list[RuleEdge]] = {}
    for mod in (ModDAO.get_profile_mods(context) or []):
        if not isinstance(mod, dict):
            continue
        source = str(mod.get("package_id") or "").strip()
        if not source:
            continue
        source_cf = source.casefold()
        edge_index.setdefault(source_cf, [])
        _, rules = resolve_mod_rules(rule_mgr, source, mod)
        for relation in _GRAPH_RELATIONS:
            for rule in (rules.get(relation) or []):
                if not isinstance(rule, dict):
                    continue
                if rule.get("effective") is False:
                    continue  # 被更高优先级来源遮蔽的规则不构成生效边
                target = str(rule.get("target_id") or "").strip()
                if not target:
                    continue
                edge = RuleEdge(
                    source=source,
                    target=target,
                    relation=relation,
                    force=bool(rule.get("is_force")),
                )
                edge_index.setdefault(source_cf, []).append(edge)
                target_cf = target.casefold()
                if target_cf != source_cf:
                    edge_index.setdefault(target_cf, []).append(edge)

    load_order_mgr = getattr(api, "load_order_mgr", None)
    if load_order_mgr is None:
        load_order_mgr = LoadOrderManager(context)
    active_ids = {
        token_info.canonical_package_id
        for pid in (load_order_mgr.read_active_mods().get("active_mods") or [])
        for token_info in (parse_package_token(pid),)
        if token_info.canonical_package_id
    }
    return edge_index, active_ids


def _resolve_start_id(edge_index: dict[str, list[RuleEdge]], raw_id: str) -> str:
    """把查询起点还原成索引里登记的原始大小写；纯孤立键（无边）时原样返回。"""
    target_cf = raw_id.casefold()
    for bucket in edge_index.values():
        for edge in bucket or ():
            if edge.source.casefold() == target_cf:
                return edge.source
            if edge.target.casefold() == target_cf:
                return edge.target
    return raw_id


# ==== 工具入口 =================================================================


def query_dependencies(
    api: Any,
    package_id: str,
    direction: str = "both",
    depth: int = 1,
    fmt: str = "edges",
) -> dict[str, Any]:
    """``get_mod_dependencies`` 的实现：校验 → 起点解析 → 遍历 → 响应结构组装。

    返回结构 ``{start, direction, depth, total_edges, truncated, cycles, ...}``，
    ``fmt=="edges"`` 时附平铺边表 ``{from,to,relation,depth}``（固定形状），
    ``fmt=="tree"`` 时附嵌套树；截断时附常量 ``TRUNCATED_HINT``。起点未知 /
    参数越界抛 ``ValueError``（中文文案，由工具层转成 isError + nextActions）。
    """
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction 无效: {direction!r}，仅支持 both/up/down")
    if not isinstance(depth, int) or isinstance(depth, bool) or not (1 <= depth <= MAX_DEPTH):
        raise ValueError(f"depth 必须是 1..{MAX_DEPTH} 的整数: {depth!r}")
    if fmt not in ("edges", "tree"):
        raise ValueError(f"format 仅支持 edges/tree: {fmt!r}")
    if fmt == "tree" and depth > _TREE_MAX_DEPTH:
        raise ValueError("format=tree 仅支持 depth<=2，多级展开请改用 format=edges")

    edge_index, active_ids = load_edge_index(api)
    raw_id = str(package_id or "").strip()
    start_cf = raw_id.casefold()
    known_nodes = set(edge_index) | {active_id.casefold() for active_id in active_ids}
    if start_cf not in known_nodes:
        raise ValueError(f"未找到此模组: {package_id}")

    walked = walk(edge_index, raw_id, direction=direction, depth=depth)
    start_id = _resolve_start_id(edge_index, raw_id)

    envelope: dict[str, Any] = {
        "start": start_id,
        "direction": direction,
        "depth": depth,
        "total_edges": len(walked.edges),
        "truncated": walked.truncated,
        "cycles": walked.cycles,
    }
    if fmt == "edges":
        # 平铺边表形状固定为 {from,to,relation,depth}（减少返回体积、可截断）；
        # force/单规则明细归 get_mod_rules。
        envelope["edges"] = walked.edges
    else:
        envelope["tree"] = build_tree(start_id, walked.edges, depth=depth)

    if walked.truncated:
        envelope["hint"] = TRUNCATED_HINT  # 常量，注入边界（见模块 docstring）
    return envelope
