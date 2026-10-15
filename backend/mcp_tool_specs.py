# backend/mcp_tool_specs.py
"""工具定义元数据：MCP 工具面的唯一定义位置（纯定义，零 backend 初始化副作用）。

约束：本模块的**函数签名即 inputSchema**、
**docstring 即 description**、**前缀即风险分级**。清单消费点都从这里取——

- ``backend.mcp_server``（工具层，stdio 测试 / mcp-embed 托管）：
  ``functools.wraps`` 绑定真实处理器后注册，schema 由本模块签名派生；
- ``scripts/gen_tool_cache.py``（构建期）：导出 ``tools-cache.json`` 供生产入口
  ``rimcrow-mcp.exe``（Rust 薄封装）``include_str!`` 嵌入——GUI 未开时提供清单。

禁止本模块 import ``backend.api`` / ``backend.ai`` / 任何会连接数据库的模块
（stdio 入口 attach-only）；只允许标准库。与 ``backend.ai`` 内置工具的字段级
一致性由 ``tests/test_mcp_server.py`` 动态断言（不一致即失败）。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "RISK_READ",
    "RISK_WRITE",
    "RISK_HIGH",
    "SERVER_INSTRUCTIONS",
    "TOOL_FUNCTIONS",
    "TOOL_SPECS",
    "risk_of",
]

# ---- 风险分级（desc 前缀，与 fork 逐字对齐）--------------------------------

RISK_READ = "[只读]"
RISK_WRITE = "[写]"
RISK_HIGH = "[高风险]"

_RISK_PREFIXES = (RISK_READ, RISK_WRITE, RISK_HIGH)

# ---- server instructions：与工具层同一来源生成，flag/embed 透传

SERVER_INSTRUCTIONS = (
    "RimCrow（RimWorld 模组管理器）的 MCP 接口。工具按风险标注只读/写入/高风险"
    f"（对应 MCP ToolAnnotations 的 readOnlyHint / destructiveHint）。使用约定：\n"
    "1. 分页取数——list_mods / get_load_order / read_logs 用 limit+offset（或 filter/keyword）"
    "限定范围，避免一次取回全部；响应里 next_offset 非 null 表示还有下一页。\n"
    "2. 增量修改——加载顺序一律用 apply_load_order_ops 提交 ops，先 dry_run:true 校验再正式"
    "写入；不要「读全量→改→回传全量」。\n"
    "3. 并发控制——写入必须带上最近一次响应的 version_token / new_version_token（乐观锁）；"
    "不一致会返回冲突，此时重新 get_load_order 取新 token 后重试。\n"
    "4. 返回摘要——日志按 list_log_files → read_logs（levels 过滤、tail 取最新）→ "
    "get_log_context 逐层缩小范围，不要回传整份文件。\n"
    "5. 按固定流程组合——获取依赖：resolve_missing_dependencies → download_workshop_mods → "
    "wait_event(\"scan-complete\") → list_mods；Git 仓库：github_fetch_repo_info → "
    "github_subscribe_repo → github_get_timeline 跟踪部署；排障：search_mod_text 检索模组文件，"
    "launch_game 后用 get_game_status / wait_event(\"game-status-changed\") 跟踪再 read_logs "
    "验证；导入排序：parse_load_order 解析 → apply_load_order_ops 写入；贴图转换："
    "get_texture_encoder_status → download_texture_encoder（缺 bin 时）→ "
    "start_texture_convert（先 dry_run:true 预演）→ get_texture_convert_tasks 轮询。\n"
    "scan_mods、下载等异步操作完成后用 wait_event 等待，或轮询 list_mods / "
    "get_download_tasks 确认。"
)


# ---- 工具清单：签名 = inputSchema，docstring = description ------------------
# 说明：docstring 与 fork TOOL_SPECS 逐字对齐；带「（MCP 扩展）」标记的句子是
# MCP 侧新增的说明（分页参数、大载荷告警、日志源选择），差异清单见
# tests/test_mcp_server.py 模块 docstring。


def get_initial_data() -> str:
    """[只读] 获取全量启动数据(设置/模组列表/分组/加载顺序等)。
    输出体积较大,首次了解项目状态时使用;之后请用 list_mods / get_load_order 等定向查询。
    超出单次输出上限时按字段从大到小逐个截断:每个大列表字段保留各自能容纳的最长前缀,
    total_items 给出各被截字段的原始条数,truncated_fields 列出被截字段;仅当标量本身
    超限才退化为只含字段名的截断壳。
    因此更推荐按需取数:list_mods / get_load_order / get_groups / get_settings_overview;
    本工具只适合数据量小的档案做一次性全量快照。"""
    raise NotImplementedError


def list_mods(
    filter: str = "",
    fields: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    scope: Literal["all", "active", "inactive"] = "all",
) -> str:
    """[只读] 模组主表(all_mods)定向查询；扫描或状态变更后用它刷新。
    （MCP 扩展）filter=名称+packageId+workshopId 子串匹配(忽略大小写)；fields=字段投影(未知名被忽略)；
    limit/offset 分页，响应字段 total/items/next_offset。不传 limit 返回全量，超出响应体积
    上限时服务端按元素边界截断，并附 hint 提示改用 limit/offset/filter 缩小范围。
    scope: all=全部模组(默认)｜active=仅已启用｜inactive=仅未启用；is_active 随行返回。
    加载顺序用 get_load_order，分组用 get_groups。"""
    raise NotImplementedError


def get_load_order(
    filter: str = "",
    fields: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> str:
    """[只读] 当前档案的加载顺序（该数据的权威来源）；写入时须原样回传响应中的
    version_token 做冲突检测。
    （MCP 扩展）items 按顺序给出 {package_id,name,workshop_id}，支持 filter/limit/offset
    分页；fields 只在这三个字段内投影，传其它字段会被静默忽略，不会报错。"""
    raise NotImplementedError


def list_backups(profile_id: str | None = None) -> str:
    """[只读] 列出所有备份文件(可按 profile_id 过滤)。"""
    raise NotImplementedError


def list_profiles() -> str:
    """[只读] 全部游戏档案(profile)。
    （MCP 扩展）默认不含 inactive_mods_order/temp_mods_order 两个大数组(控制响应体积)；
    激活的加载顺序用 get_load_order。"""
    raise NotImplementedError


def read_logs(
    log_type: str = "game",
    filename: str = "",
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
    profile_scope: str = "active",
) -> str:
    """[只读] 分页读系统日志(JSONL)；默认从最新一条开始读，第 1 页即最新。
    log_type/filename 取自 list_log_files(缺省=active 档案的最新文件)。
    （MCP 扩展）levels=["error","warning"] 按日志级别过滤；since/until 支持 "1h"/"30m"
    相对时间或 ISO 时间；line_from/line_to 行号区间；keyword 子串匹配；tail=N 只保留
    最新 N 条并提前结束扫描(返回 paging="tail"、next_offset 恒为 null，不能翻页)。
    返回窗口由 limit/offset/tail 决定；page_size 只是内部单次请求的页大小，无需传。
    每条返回 raw_lines(日志源行号，从 1 开始)——get_log_context.target_line 只接受
    raw_lines[0]；编辑器/grep 统计的行号属于另一坐标系，不可混用。"""
    raise NotImplementedError


def get_settings_overview(package_id: str = "") -> str:
    """[只读] 获取模组设置概览。
    单次返回当前档案的全量概览,体量很大(实测单次约 124KB),只在需要全貌时调用,
    取到后不要整包回传。
    只想看一个模组时传 package_id:与概览里的 mod_groups[].package_id 精确匹配
    (忽略大小写),只返回 config_path + 命中的 mod_groups,扫描级统计量不随行;
    无命中时 mod_groups 为空并附 hint。不传 package_id 保持全量返回(向后兼容)。"""
    raise NotImplementedError


def get_groups(limit: int | None = None, offset: int = 0) -> str:
    """[只读] 获取全部分组及其模组归属(含 group_id/name/color/sort_index/mod_ids)。
    limit/offset 分页,响应字段 total/items/next_offset;不传 limit 返回全量。"""
    raise NotImplementedError


def scan_mods(
    specific_paths: list[str] | None = None,
    forced_update: bool = False,
    size_check_override: bool | None = None,
    size_check_paths: list[str] | None = None,
) -> str:
    """[写] 触发后台模组扫描,立即返回已启动状态;
    完成后可用 wait_event("scan-complete") 等待,或轮询 list_mods 确认结果。
    specific_paths 为空时使用设置中的默认路径。
    成本/副作用:这是重 IO 的全库扫描(默认扫 DLC / 本地 Mods / 自身 Mods /
    工坊 Mods / 工具模组目录),扫描收尾还会同步 _Link_ 部署(增删运行时联接);
    forced_update=true 会强制刷新所有模组数据。请按需调用,不要连续触发。"""
    raise NotImplementedError


def sort_mods(active_ids: list[str]) -> str:
    """[写] 对传入的激活模组 ID 列表执行自动排序,返回 sorted_ids / warnings。
    响应还可能含 auto_activated(排序时被自动激活的联锁项)。
    只计算顺序,不写 ModsConfig.xml;确认结果后用 apply_load_order_ops / save_load_order 落盘。"""
    raise NotImplementedError


def save_load_order(
    active_ids: list[str],
    is_dirty: bool = True,
    base_version_token: dict | None = None,
) -> str:
    """[写] 把激活列表全量写入 ModsConfig.xml，带 version_token 冲突检测。
    active_ids 必须是完整目标列表；base_version_token 在 schema 中可选但必须传，
    否则跳过冲突检测，可能覆盖其它程序对该文件的改动。
    （MCP 扩展）⚠ 全量返回、体积大，仅作兼容保留——顺序调整改用
    apply_load_order_ops(增量操作、原子提交、支持 dry_run)。"""
    raise NotImplementedError


def update_mod_data(package_id: str, data_dict: dict) -> str:
    """[写] 更新某个模组的用户自定义数据(别名/标签/备注/颜色等),data_dict 为要合并写入的字段。
    合并语义:只覆盖 data_dict 里出现的字段,未出现的字段保持原值;字段名必须是用户数据
    模型字段(如 alias_name/notes/tags/sign_color),未知字段会被静默丢弃。
    可写字段可参照 get_mod_user_data 的返回确认现状。"""
    raise NotImplementedError


def create_group(name: str, color: str) -> str:
    """[写] 创建模组分组,name 为分组名,color 为颜色(十六进制如 #FF0000)。
    color 必须是 #RRGGBB 形式的十六进制;非法值会被静默置为 #ffffff(不报错也不警告)。"""
    raise NotImplementedError


def update_group(group_id: str, updates: dict) -> str:
    """[写] 更新分组属性(重命名/改色/折叠),updates 可含 name/color/is_expanded 字段。"""
    raise NotImplementedError


def add_mods_to_group(group_id: str, mod_ids: list[str]) -> str:
    """[写] 把模组 ID 列表加入指定分组(mod_ids 用 package_id)。"""
    raise NotImplementedError


def remove_mods_from_group(group_id: str, mod_ids: list[str]) -> str:
    """[写] 从指定分组移除模组 ID 列表(mod_ids 用 package_id)。"""
    raise NotImplementedError


def reorder_group_content(group_id: str, mod_id_list: list[str]) -> str:
    """[写] 重排分组内模组的显示顺序,mod_id_list 为该分组完整的目标顺序。
    实际语义是「设置分组成员顺序」而非单纯重排:列表中的组外模组会被并入该分组
    (要求模组资产存在),列表必须非空且不得重复;未出现在列表里的原有成员不会被移除,
    会按原顺序续到列表末尾,传入列表的顺序成为结果的前缀。"""
    raise NotImplementedError


def reorder_groups(group_id_list: list[str]) -> str:
    """[写] 重排分组列表的显示顺序,group_id_list 为完整的目标顺序。"""
    raise NotImplementedError


def create_backup(path: str, target_dir: str, profile_id: str | None = None) -> str:
    """[高风险] 把加载顺序备份为文件(ModsConfig.xml 风格)到指定目录。
    会写磁盘文件,请确认 target_dir 正确后再调用。
    实际是把既有备份文件复制到 target_dir:target_dir 必须是已存在的目录(否则报错),
    同名文件不会被覆盖(会自动加序号改名,如 name-1.xml),成功响应含新文件 path。"""
    raise NotImplementedError


def activate_profile(pid: str) -> str:
    """[高风险] 切换当前游戏档案(profile),会影响后续所有操作的作用域,
    切换后请用 list_mods / get_load_order 重新读取状态。
    pid 取 list_profiles 返回条目的 id;切换是全局的,会改变此后所有调用读写的数据源。
    切换失败时后端会尝试回退到 default 档案,因此即使返回 error 当前档案也可能已经改变,
    请用 list_profiles 复核实际生效的档案。"""
    raise NotImplementedError


def delete_group(group_id: str) -> str:
    """[高风险] 删除指定分组(含其模组归属关系),不可恢复,请先 get_groups 确认 group_id。"""
    raise NotImplementedError


def wait_event(event_name: str, timeout: float = 60.0) -> str:
    """[只读] 等待后端事件(如 \"scan-complete\"、\"game-status-changed\")，超时返回
    timeout 状态。用于确认 scan_mods / launch_game 等异步操作已完成。
    （MCP 扩展）每个并发等待者各持独立事件缓冲：并发等待互不取走对方的事件
    (wait_event 与 search_mod_text 可同时使用，各自收到自己的事件)；只命中等待
    开始之后新到达的事件，服务端就绪前发出的事件捕获不到——启动早期请改为轮询
    list_mods。事件不含发生时间，取到后先核对 data 再使用。"""
    raise NotImplementedError


# ---- 既有内置 AI 工具的原样导出（只读复用）


def get_log_context(
    target_line: int,
    stack_excerpt_lines: int = 15,
    log_type: str = "game",
    filename: str = "",
) -> str:
    """[只读] 读取某条错误摘要对应的详细日志上下文，包含原始报错内容与截断堆栈。仅当摘要不足以定位问题时再调用。
    （MCP 扩展）log_type: "game"|"app"；filename 缺省用日志源中最新文件(见 list_log_files)。
    stack_excerpt_lines 取值 6-80（越界上游参数模型会拒绝）。
    target_line 必须取 read_logs 返回条目的 raw_lines[0]（本工具自己的行号坐标系）；
    不要用 grep / 编辑器 / IDE 数出来的行号——日志可能含单独 CR 换行，外部工具的行号
    坐标系与本工具不一致（实测差 3 行会静默定位到无关内容）；它也不是 read_logs
    返回列表的序号。"""
    raise NotImplementedError


def search_mods(keyword: str, limit: int = 10) -> str:
    """[只读] 按名称、别名、作者或 package_id 片段搜索可能对应的模组，用于确认准确 package_id。
    limit 取值范围 1..50(默认 10，0 或 51 这类越界值会被参数模型拒绝)。"""
    raise NotImplementedError


def get_active_mod_list(include_names: bool = False) -> str:
    """[只读] 获取当前环境中的启用模组列表，用于整体排序和冲突排查。"""
    raise NotImplementedError


def get_mod_info(
    package_id: str,
    scope: Literal["simple", "all", "specific"] = "simple",
    fields: list[str] | None = None,
) -> str:
    """[只读] 读取已安装模组的元数据。simple 返回基本信息：('package_id', 'name', 'workshop_id', 'supported_versions', 'is_active', 'path')；all 返回全部信息；specific 返回指定字段：('package_id', 'name', 'author', 'workshop_id', 'version', 'description', 'path', 'url', 'source', 'store', 'supported_versions', 'supported_languages', 'mod_type', 'is_active', 'dependencies_mods', 'load_after_mods', 'load_before_mods', 'incompatible_mods', 'save_breaking', 'last_active_time', 'last_moved_time')。
    scope=specific 必须提供非空且全部合法的 fields(合法值即上面 specific 列出的字段),
    fields 为空或含非法字段时整个调用会被拒绝,不会部分返回。"""
    raise NotImplementedError


def get_mod_rules(package_id: str, native_only: bool = False) -> str:
    """[只读] 读取模组依赖、前置、后置和不兼容规则，用于判断显性排序和兼容关系。
    单模组一跳明细；多级依赖图/环路/影响面请用 get_mod_dependencies。"""
    raise NotImplementedError


def get_mod_user_data(package_id: str) -> str:
    """[只读] 读取管理器中的模组别名、标签、备注、颜色、分组等用户态信息，仅用于辅助理解玩家归类。"""
    raise NotImplementedError


def get_group_mods(group_name: str, limit: int = 40) -> str:
    """[只读] 按分组名读取分组下的模组列表，仅用于辅助理解玩家自己的分组归类。
    group_name 是子串模糊匹配(大小写不敏感)，可能同时命中多个分组，结果逐组放在
    matched_groups 里；limit 为每组最多返回的模组数，取值 1..100(默认 40，越界会被
    参数模型拒绝)，截断时该组带 truncated 标记。"""
    raise NotImplementedError


# ---- 缺口补全 ----


def get_mod_dependencies(
    packageId: str,
    direction: Literal["both", "up", "down"] = "both",
    depth: int = 1,
    format: Literal["edges", "tree"] = "edges",
) -> str:
    """[只读] 模组依赖与加载顺序关系图（参数名是驼峰 packageId，与其它工具的 package_id 不同）。
    direction: up=指向我的边(谁依赖我/要求排在我之后，用于删除前的影响分析)｜down=我指出的
    边(我依赖谁/我要求谁排在后面)｜both=双向；depth 1..5。
    format=edges 返回平铺边表 {from,to,relation,depth}(体积小)｜tree 为嵌套结构，仅 depth<=2。
    环路(loadBefore/After 互指)单独列在 cycles，不会无限递归。edges 最多 500 条，
    truncated:true 表示未取全——本工具没有翻页参数，请缩小 direction/depth 后重查。
    直接关系(1-hop)的规则明细用 get_mod_rules。"""
    raise NotImplementedError


def list_log_files(log_type: str = "game", profile_scope: str = "active") -> str:
    """[只读] 可读日志文件列表(name/path/size/mtime 按修改倒序)，是 read_logs /
    get_log_context 的 filename 来源。log_type 仅 game|app 有效(schema 是 string)，
    其它值(含空串)静默按 game 处理。"""
    raise NotImplementedError


def apply_load_order_ops(
    base_version_token: dict,
    ops: list[dict],
    dry_run: bool = False,
) -> str:
    """[写] 用增量操作原子地修改加载顺序（顺序变更的推荐入口）。
    ops 整批全有全无、最多 100 条：insert{id,anchor:{after|before|index}}｜remove{ids}｜
    move{id,to:{index|after|before}}｜activate{ids}｜deactivate{ids}。
    base_version_token 取自 get_load_order(用于冲突检测)；dry_run:true 只校验、不写入，
    也不会返回 new_version_token。写入成功返回 summary/conflicts/new_version_token，
    不返回完整列表。"""
    raise NotImplementedError


def set_mod_rule(
    package_id: str,
    relation: Literal["loadAfter", "loadBefore", "incompatibleWith"],
    target_id: str,
    comment: str = "",
    remove: bool = False,
) -> str:
    """[写] 新增、修改或删除本模组的一条用户排序/冲突规则。
    relation: loadAfter=必须排在 target 之后(target 为前置)｜loadBefore=必须排在 target
    之前｜incompatibleWith=两者不应同时启用。remove:true 只删除这一条，其余规则保留
    (合并写入，不是整体替换)；内容无变化时返回 action=noop，不写入磁盘。
    规则立即写入并参与后续自动排序——建议先用 get_mod_rules 查看现状；
    comment 是界面上显示的可选备注。"""
    raise NotImplementedError


# ---- 排障 / 整理 / 获取类工具 ----


def get_game_status() -> str:
    """[只读] 游戏运行状态：running、steam_running、runtime_session(最近启动会话的
    id/状态/档案)。launch_game 后用本工具或 wait_event("game-status-changed") 跟踪。"""
    raise NotImplementedError


def launch_game(profile_id: str = "", confirm_action: str = "") -> str:
    """[写] 启动游戏(默认当前档案)。会创建 RimWorld 进程，并可能同步模组链接(_Link_)，
    确认需要后再调用。检测到启动前风险(如 Steam 正在运行)时不启动，改以 isError 返回
    warning，并在 nextActions 中给出后续步骤：用 confirm_action 重新调用
    (continue=继续启动｜wait_steam_exit=等待 Steam 退出｜retry_steam_client /
    retry_steam_url=换 Steam 方式重试｜cancel=取消)。启动后用 get_game_status 跟踪。"""
    raise NotImplementedError


def search_mod_text(
    query: str,
    scope: Literal["current-effective", "current-active", "workshop", "local", "self"] = "current-active",
    use_regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 50,
    timeout_seconds: float = 120.0,
    file_types: list[str] | None = None,
    filenames_only: bool = False,
) -> str:
    """[只读] 在模组文件中做子串/正则搜索(ripgrep)，用于定位问题：报错文本出自哪个模组、
    某个 Def 由谁定义。
    scope: current-active=已启用模组(默认，排障首选)｜current-effective=当前环境可见的
    全部模组(含未启用)，并按 LoadFolders 只保留实际会加载的文件——版本目录取不高于当前
    游戏版本的最高者，IfModActive/DLC 条件按当前已启用集合判定｜workshop/local/self=
    按各自的模组库全量搜索(较慢)。
    file_types 限定扩展名，默认 [".xml"]；要搜全部类型(含二进制)传 ["."]。
    filenames_only=true 改为按文件名或模组内相对路径匹配(查「哪个模组带这张贴图/dll」)：
    不读文件内容，结果 line_number=0、matched_line 为命中的文件名或相对路径；此模式下
    不应用 Textures/Source/Languages 目录排除，未给 file_types 则不限扩展名。
    返回 matched_count 与 results[{mod_name,file_path,line_number,matched_line}]，
    条数超过 max_results 时 truncated=true。本工具耗时较长(默认 120 秒)；事件缓冲
    按等待者隔离，期间并发调用 wait_event 不会互相取走事件。"""
    raise NotImplementedError


def parse_load_order(path: str = "", share_code: str = "") -> str:
    """[只读] 解析排序文件或分享码，只读——不修改当前加载顺序。path / share_code 二选一
    (都传时以 path 为准)。支持 ModsConfig.xml、ModList.xml、RML、RimSort 导出与分享码。
    返回 format/total/active_ids/warnings/errors/import_check(缺失模组清单)；
    active_ids 可直接作为 apply_load_order_ops 的入参。"""
    raise NotImplementedError


def export_load_order(
    export_format: Literal["modsconfig", "modlist", "rml", "share_code"],
    path: str = "",
    list_name: str = "",
) -> str:
    """[高风险] 导出当前激活的加载顺序。export_format=share_code 时直接返回分享码字符串
    (path 留空)；modsconfig/modlist/rml 必须给完整 path(含扩展名)，会创建文件，调用前确认
    路径正确。激活列表由服务端读取，不需要传。"""
    raise NotImplementedError


def smart_insert_mods(package_ids: list[str], base_version_token: dict, dry_run: bool = False) -> str:
    """[写] 用规则引擎把模组插入激活列表并写入 ModsConfig.xml（新模组加入列表的推荐入口）。
    插入位置由依赖与规则决定，只需传 package_ids(最多 100 个)；base_version_token 取自
    get_load_order(过期会被拒绝)。dry_run:true 只返回插入位置与摘要、不写入；成功返回
    applied[{id,index,after}]、auto_activated(随之自动启用的联锁项)、new_version_token，
    不返回完整列表。"""
    raise NotImplementedError


def set_mod_absolute_position(
    package_id: str,
    position: Literal["top", "bottom", "none"],
    comment: str = "",
) -> str:
    """[写] 设置/清除模组的用户固定位置规则(top=置顶｜bottom=置底｜none=清除)，
    影响后续自动排序；写前用 get_mod_rules 看现状，comment 为可选备注。"""
    raise NotImplementedError


def get_residue_overview() -> str:
    """[只读] 当前扫描范围内的卸载残留目录与关联设置文件总览(只读，不删除)。
    供清理前评估；删除文件属批量磁盘变更，本工具不执行。"""
    raise NotImplementedError


def trace_install_sources(package_ids: list[str]) -> str:
    """[只读] 按 package_id 批量查询模组的安装来源(外置数据库本地查询)，最多 100 个 id。
    返回 {package_id:{original:[],replacement:[]}}，条目含 workshop_id/url/name/kind/
    supported_versions；查不到的 id 不在返回中。拿到 workshop_id 后可作为
    download_workshop_mods 的入参补下载。"""
    raise NotImplementedError


def resolve_missing_dependencies(package_ids: list[str] | None = None) -> str:
    """[只读] 找出缺失的前置模组并给出可下载的 workshop ID（创意工坊物品 ID）。不传
    package_ids 时由服务端读取当前激活列表。返回 missing:[{workshop_id,name,preview_url}]，
    workshop_id 可直接作为 download_workshop_mods 的入参；补充名称时可能联网，耗时数秒。"""
    raise NotImplementedError


def download_workshop_mods(workshop_ids: list[str]) -> str:
    """[写] 用 SteamCMD 下载/更新创意工坊物品，立即返回 task_id（联网并写入创意工坊目录，
    完成后自动触发一次全量扫描）。进度用 get_download_tasks 轮询；要确认已下载并完成扫描，
    需等 wait_event("scan-complete") 后调用 list_mods。SteamCMD 未就绪时返回 isError 错误、
    正文含 nextActions——按其指引先在 GUI 的工具环境检查中修复。"""
    raise NotImplementedError


def get_download_tasks() -> str:
    """[只读] 获取所有后台任务状态(SteamCMD 下载等),用于轮询下载进度与结果。"""
    raise NotImplementedError


def check_mod_updates() -> str:
    """[只读] 检查已安装模组是否有可用更新：创意工坊模组用 Steamworks 本机状态比对，
    自身(self)模组用 Steam Web API 比对更新时间。返回 updates:
    [{workshop_id,title,source,local_time,online_time}]。需要联网、耗时十几秒；Steam 未
    运行时创意工坊部分会被跳过(响应中注明)，结果按实际覆盖范围采信。"""
    raise NotImplementedError


# ---- 获取通路补全：Steam 订阅 / WebAPI 订阅 / Git 仓库订阅 / 本地模组包 ------


def subscribe_workshop_items(workshop_ids: list[str]) -> str:
    """[写] 向 Steam 客户端提交创意工坊订阅（Steamworks ISteamUGC），订阅后由 Steam
    自动下载到工坊目录。前置：Steam 正在运行且已登录——未就绪会返回因果错误，无账号
    的获取走 download_workshop_mods（SteamCMD 匿名下载）。与后者的分工：本工具会把
    模组挂进账号云端订阅列表并随 Steam 自动更新。返回 task_id（task_type=
    steam-subscribe，可 cancel_task 取消）。下载完成不等于入库：需
    wait_event("scan-complete")（或 scan_mods）→ list_mods 确认。"""
    raise NotImplementedError


def unsubscribe_workshop_items(workshop_ids: list[str]) -> str:
    """[写] 向 Steam 客户端提交取消订阅（Steamworks ISteamUGC），解除账号云端订阅
    关系并返回 task_id（task_type=steam-unsubscribe，可 cancel_task 取消）。只退订：
    本地文件去留由 Steam 后续校验决定，重要模组退订前先确认；本地残留面用
    get_residue_overview。前置同 subscribe_workshop_items（Steam 运行且已登录）。"""
    raise NotImplementedError


def webapi_subscribe_workshop(workshop_id: str) -> str:
    """[写] 备用 WebAPI 订阅（IPublishedFileService/Subscribe），不经 Steam 客户端。
    前置：RimCrow 设置已配置 steam_web_api_key（未配置返回因果错误）；普通 Key 能否
    订阅成功由 Steam 判定——主力通路是 subscribe_workshop_items（Steam 客户端），
    无账号的获取走 download_workshop_mods。不自动带依赖，依赖补齐走
    resolve_missing_dependencies。联网操作。"""
    raise NotImplementedError


def webapi_unsubscribe_workshop(workshop_id: str) -> str:
    """[写] 备用 WebAPI 取消订阅（IPublishedFileService/Unsubscribe），不经 Steam
    客户端。前置：RimCrow 设置已配置 steam_web_api_key；成败同样由 Steam 判定，
    主力通路是 unsubscribe_workshop_items。联网操作。"""
    raise NotImplementedError


def github_fetch_repo_info(url: str, source_branch: str = "") -> str:
    """[只读] 解析 Git 仓库信息（provider/host/owner/repo/default_branch/has_release/
    latest_release_tag/源码分支版本），支持 GitHub 与 GitLab 系（含 gitgud.io）。
    GitHub API 限流或不可用时自动降级网页/本地缓存，结果可能带 fetch_warning（注明
    降级原因）。联网操作，耗时数秒。订阅前用它决定 install_type 与 version：
    无 Release 的仓库用 source（按分支装源码），有 Release 用 release（按 tag 装），
    清单型仓库用 zip。"""
    raise NotImplementedError


def github_subscribe_repo(
    url: str,
    install_type: Literal["source", "zip", "release"] = "source",
    source_branch: str = "",
    version: str = "",
) -> str:
    """[写] 订阅 Git 仓库并立即触发下载部署（订阅＝落监听记录 + 部署，一次完成）。
    先 github_fetch_repo_info 取仓库信息（失败即因果错误，不落订阅），再落库并启动
    部署任务。install_type：source=按分支装源码（source_branch 留空跟随仓库默认
    分支，version 忽略）｜zip=装清单压缩包｜release=装 Release（version 给 tag，
    留空用最新 tag）。模组装进管理器自身 Mod 目录 _GH_<repo>，同名目录会被覆盖。
    返回当前全部订阅记录；上游不透出 task_id，部署进度用 github_get_timeline(url)
    跟踪、进行中任务用 get_download_tasks（取消走 cancel_task(task_type="download")）。
    部署完成≠入库：需 wait_event("scan-complete")（或 scan_mods）→ list_mods 确认。"""
    raise NotImplementedError


def github_get_subscribed() -> str:
    """[只读] 列出全部 Git 仓库订阅记录（provider/分支/install_type/installed_version/
    last_sync 与最新时间线摘要）；missing_urls 标出订阅仍在但本地目录已缺失的仓库。
    是 Git 仓库模组的现状总览，更新决策结合 github_fetch_repo_info 的最新版本比对。"""
    raise NotImplementedError


def github_trigger_download(
    url: str,
    install_type: Literal["source", "zip", "release"],
    version: str = "",
) -> str:
    """[写] 对已订阅的 Git 仓库重新触发下载与部署（更新版本/换分支/重装），部署
    语义同 github_subscribe_repo 的部署半段，但不改订阅记录。返回 task_id（走
    download_mgr，可 cancel_task(task_type="download") 取消）；进度用
    github_get_timeline(url) / get_download_tasks 跟踪，装完需扫描入库。"""
    raise NotImplementedError


def github_get_timeline(url: str) -> str:
    """[只读] 单个 Git 仓库的本地操作时间线（订阅/下载/解压/部署成功/部署失败/移除/
    本地缺失），是「部署到底成没成」的事实源——get_download_tasks 只反映进行中
    任务，成功与失败的结论以这里为准。"""
    raise NotImplementedError


def github_remove_subscription(url: str) -> str:
    """[写] 移除 Git 仓库订阅记录（数据库记录 + 时间线标记）。**不删除已安装的模组
    文件**——本地文件删除属批量磁盘变更，不在工具面内；文件是否还在用
    get_residue_overview / list_mods 确认。"""
    raise NotImplementedError


def mod_package_prepare_import(bundle_path: str) -> str:
    """[只读] 预检 RimCrow 模组包（.rimcrowmods.zip）的导入冲突（版本/覆盖/缺失），
    零写入。bundle_path 为模组包绝对路径。预检通过后用 mod_package_import 落地。"""
    raise NotImplementedError


def mod_package_import(bundle_path: str) -> str:
    """[写] 启动 RimCrow 模组包（.rimcrowmods.zip）导入任务（解包 → 装入本地 Mods →
    收敛当前档案），返回 task_id（task_type=mod-import，可 cancel_task 取消）；目标
    档案与本地模组目录由上游按当前环境自动补，无需也无法显式指定。建议先跑
    mod_package_prepare_import 预检冲突。完成≠入库：需 wait_event("scan-complete")
    （或 scan_mods）→ list_mods 确认。"""
    raise NotImplementedError


# ---- 补全批：MCP 缺失能力（取消任务 / 读模组文件 / 工坊详情查询）--------------


def cancel_task(
    task_type: Literal[
        "scan",
        "download",
        "update",
        "localize",
        "mod-import",
        "mod-export",
        "steamcmd-download",
        "steamcmd-workshop-repair",
        "steamcmd-init",
        "steam-subscribe",
        "steam-unsubscribe",
        "steam-workshop-download",
        "texture-opt",
        "texture-opt-analyze",
        "ai-task",
        "file-search",
    ],
    task_id: str = "",
) -> str:
    """[写] 取消一个进行中的后台任务——发起 search_mod_text / 下载 / 扫描这类长任务后
    不必干等。task_type 只接受上游统一取消入口实际路由的 16 种取值（cancel_progress_task 逐类型
    分派，与前端 cancellableTaskTypes 同集合）：scan｜download｜update｜localize｜
    mod-import｜mod-export｜steamcmd-download｜steamcmd-workshop-repair｜steamcmd-init｜
    steam-subscribe｜steam-unsubscribe｜steam-workshop-download｜texture-opt｜
    texture-opt-analyze｜ai-task｜file-search。
    task_id 取自发起任务的响应（scan_mods 的 data.details.task_id、search_mod_text 与
    download_workshop_mods 的 task_id）；仅 task_type=scan 可留空＝取消当前扫描
    （cancel_progress_task 的 scan 分支传 None）。取消是**请求**语义：返回成功只代表已发出请求，不代表任务
    已终止——用 get_download_tasks / wait_event("scan-complete") 复核实际结果。"""
    raise NotImplementedError


def read_mod_file(path: str) -> str:
    """[只读] 读取单个模组文件的文本内容（search_mod_text 只回匹配行，要看上下文用本
    工具读全文）。path 必须落在当前环境**已知模组根目录内**：DLC / 本地 Mods / 自身
    Mods / 工坊 Mods / 工具模组目录，或已扫描到的模组自身路径；含 `..` 的路径与越界
    路径一律拒绝——这不是任意文件读取工具。
    单次最多读 64 KiB：上游 path_read_text_file 的默认上限是 2 MB
    （上限定义在 mgr_files.read_text_file 的 max_bytes 默认值），本工具按工具层输出预算收窄；文件更大时 truncated=true，
    file_size 仍是完整字节数、content 只到文件开头，超限部分不返回。
    返回 file_path / content / truncated / file_size / encoding / max_bytes；encoding
    为实际使用的解码编码（按 BOM 与内容依次探测）。"""
    raise NotImplementedError


def get_workshop_mod_details(workshop_ids: list[str]) -> str:
    """[只读] 按创意工坊物品 ID 批量查详情（标题/作者/预览图/更新时间），一次最多
    100 个（同上游 mgr_steam_api._request_published_file_details 的单批量级），更多请分批。ID 取自
    get_residue_overview / trace_install_sources / resolve_missing_dependencies 等返回的
    workshop_id；上游先规范化（纯数字 6..20 位，非法与重复的直接跳过，
    见 get_workshop_details）。返回 data = {workshop_id: {workshop_id, package_id, title,
    name, author, preview_url, url, time_updated, detail_source, available}}；
    detail_source: online=在线命中｜database=仅本地库缓存｜unavailable=两处都没有，
    available=false 即查不到该 ID。可能联网（Steam GetPublishedFileDetails），在线拿不到
    时自动降级为本地库数据；所有 ID 都无效时 data 为空对象。"""
    raise NotImplementedError


# ---- 贴图转换工具（启动 / 查询 / 取消）--------------------------------------


def get_texture_encoder_status() -> str:
    """[只读] 检测贴图转换编码器(todds/texconv/nvtt)可用性、机器容量与自动推荐方案。
    返回 encoders(逐个 available/resolved_path)、machine(cpu_logical 与 GPU
    DirectCompute 实测探测)、plan(auto 推荐的 backend 与 gpu/cpu 并发数、理由与
    质量注记、selected/effective 档位解析)。贴图转换前的第一步:按 plan 选
    start_texture_convert 的 backend;缺 bin 时用 download_texture_encoder 补齐。
    首次调用会做一次 GPU 探测(约 1 秒,结果按可执行文件缓存)。"""
    raise NotImplementedError


def download_texture_encoder(backend: Literal["todds", "texconv", "nvtt"]) -> str:
    """[写] 准备缺失的贴图编码器。todds → release zip 解包;texconv → microsoft/DirectXTex
    单文件直落工具目录;nvtt → **不自动下载**(闭源安装包无稳定直链),返回官网地址由界面
    打开,安装后重新调用即可识别。已就绪返回 already_ready:true 不重复下载。
    进度用 get_download_tasks 轮询(todds/texconv),完成后用 get_texture_encoder_status 复核。"""
    raise NotImplementedError


def start_texture_convert(
    mod_paths: list[str],
    backend: Literal["auto", "todds", "nvtt", "texconv"] = "auto",
    output_format: Literal["dds", "zstd"] = "dds",
    generate_mipmaps: bool = True,
    scale_factor: float = 1.0,
    overwrite: bool = False,
    gpu_workers: int | None = None,
    cpu_workers: int | None = None,
    dry_run: bool = False,
) -> str:
    """[高风险] 把模组 Textures 目录下的 PNG 批量转为 DDS(原地写入,源 PNG 永不删除)。
    mod_paths 为模组根目录绝对路径(list_mods 条目的 path 字段,上限 50 个)。
    backend 四选一(缺省 auto = 有 texconv 就用 texconv,否则 todds):
      todds  = 标准档,画质最高(BC7 62.56 / BC1 42.50 dB),最慢;
      nvtt   = 中等档,画质贴近标准(62.22 / 41.35,BC1 曾反超标准),原尺寸下速度居中;
      texconv= 快速档,最快(1.51~1.82 倍),画质低 2~4 dB(肉眼难分)。
    **三档均支持缩放**:todds 用原生 -sc;nvtt/texconv 由包装层先做 PIL LANCZOS 预缩放
    再编码。缩放会改变速度排名(原尺寸下中等快于标准,开启预缩放后标准反超中等;
    临界点随硬件与版本变化,以本机 dry_run + 实测为准)。
    **nvtt 需本机已装 NVIDIA Texture Tools Exporter**,不自动下载——缺失时用
    download_texture_encoder(backend="nvtt") 取官网地址,装好后重新调用即识别。
    output_format=zstd 生成 Image Opt Continued 可读的 .dds.zstd。
    scale_factor 只接受 1.0(原尺寸,默认)或 GUI 缩放档位(20/25/40/50/60/75/80%),
    其它值(如 0.9)报因果错误——非档位值会被后端静默按原尺寸转换,所以不代为取整。
    overwrite=false 只补缺失输出(增量,推荐),true 全量重编;缩放档 overwrite=false
    沿用 GUI 档位语义。gpu_workers/cpu_workers 覆盖自动并发(1..12):对 texconv 决定
    GPU/CPU 分片(gpu 缺省 3、实测探测失败→0;cpu=clamp(逻辑核-2,2,6));对 nvtt 只决定
    分片数(缺省 6,8 即驱动排队劣化);todds 走内置线程池、不接受这两个参数。
    dry_run:true 只扫描统计不写盘,返回 summary(沿用扫描层既有键:generate_required_count
    =待转换条数、source_total_count=源文件数、pending_source_bytes=待转写字节量)、
    逐模组 mods 与将选用的 backend。
    任务异步执行立即返回 task_id,用 get_texture_convert_tasks 轮询,
    cancel_texture_convert 可取消。
    成本/副作用:批量写入模组目录的 .dds/.dds.zstd(含工坊模组目录);请先 dry_run。"""
    raise NotImplementedError


def get_texture_convert_tasks() -> str:
    """[只读] 获取进行中的贴图转换任务快照(status/progress/阶段计数/失败清单)。
    start_texture_convert 之后用本工具轮询;任务结束(success/failed/cancelled)
    后保留 120 秒可查,最终统计在 summary(optimized/skipped/failed 与结果文件
    路径)。响应已剥离逐模组明细(final_mods)等大载荷。"""
    raise NotImplementedError


def cancel_texture_convert(task_id: str) -> str:
    """[写] 取消一个贴图转换任务(task_id 来自 start_texture_convert)。
    取消是协作式的:进行中的编码子进程会在数秒内退出,已完成部分保留;
    任务不存在或已结束时返回因果错误。"""
    raise NotImplementedError


# ---- 注册表：名称 -> backend.api.API 方法（None = 工具层自实现）-------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """单个工具的绑定描述（清单由函数对象派生，不复制字面量）。"""

    func: Callable[..., str]
    name: str
    method: str | None  # backend.api.API 方法名；None = 工具层自实现

    @property
    def desc(self) -> str:
        return inspect.getdoc(self.func) or ""

    @property
    def risk(self) -> str:
        return risk_of(self.desc)


def risk_of(desc: str) -> str:
    """按 desc 前缀取风险分级；未标注视为 [只读]。"""
    for prefix in _RISK_PREFIXES:
        if desc.startswith(prefix):
            return prefix
    return RISK_READ


_API_METHODS: dict[str, str] = {
    "get_initial_data": "get_initial_data",
    "list_backups": "backups_get_all",
    "scan_mods": "scan_mods",
    "sort_mods": "auto_sort_mods",
    "save_load_order": "load_order_save_with_token",
    "update_mod_data": "mod_user_data_update",
    "create_group": "group_create",
    "update_group": "group_update",
    "add_mods_to_group": "group_add_mods",
    "remove_mods_from_group": "group_remove_mods",
    "reorder_group_content": "group_content_reorder",
    "reorder_groups": "group_reorder",
    "create_backup": "backup_file_save_as",
    "activate_profile": "profile_activate",
    "delete_group": "group_delete",
    # 单步直接委托（参数即 API 参数）
    "set_mod_absolute_position": "rule_set_user_mod_absolute_position",
    "get_residue_overview": "mod_residue_get_overview",
    "trace_install_sources": "get_install_sources_by_package_ids",
    "download_workshop_mods": "steamcmd_download",
    "get_download_tasks": "get_active_downloads",
    "check_mod_updates": "lifecycle_check_updates",
    # 订阅与部署类动作。参数名必须与 API 方法签名逐字一致——_delegate_method 按
    # kwargs 原样转发，名字对不上就是调用期 TypeError；
    # github_subscribe_repo 要先取仓库信息组装 payload，逻辑在 handler 里，不在此登记。
    "subscribe_workshop_items": "steam_subscribe",
    "unsubscribe_workshop_items": "steam_unsubscribe",
    "webapi_subscribe_workshop": "workshop_webapi_subscribe",
    "webapi_unsubscribe_workshop": "workshop_webapi_unsubscribe",
    "github_fetch_repo_info": "github_fetch_info",
    "github_get_subscribed": "github_get_subscribed",
    "github_trigger_download": "github_trigger_download",
    "github_get_timeline": "github_get_timeline",
    "github_remove_subscription": "github_remove_subscription",
    "mod_package_prepare_import": "mod_package_prepare_import",
    "mod_package_import": "mod_package_import",
    # 直接委托（api 方法即最终形状，无需投影/校验层）
    "get_texture_encoder_status": "texture_get_encoder_status",
    "download_texture_encoder": "texture_prepare_encoder_download",
    # 适配层自实现（一个工具只允许一条绑定路径）：list_mods / get_load_order /
    # list_profiles / get_groups / read_logs / list_log_files / get_settings_overview 由
    # mcp_server._HANDLER_OVERRIDES 适配，其 ``api.<method>`` 目标只写在 handler 体内、
    # 这里不登记（双写漏改其一不会报错，互斥由 validate_bindings 校验）；其余自实现
    # （wait_event / get_mod_dependencies / apply_load_order_ops / AI 桥接 8 个工具）method 置 None。
}

# 顺序即 tools/list 展示顺序：查询 → 写 → 高风险 → AI 工具桥接 → 缺口补全 → 单步委托。
TOOL_FUNCTIONS: tuple[Callable[..., str], ...] = (
    get_initial_data,
    list_mods,
    get_load_order,
    list_backups,
    list_profiles,
    read_logs,
    get_settings_overview,
    get_groups,
    wait_event,
    scan_mods,
    sort_mods,
    save_load_order,
    update_mod_data,
    create_group,
    update_group,
    add_mods_to_group,
    remove_mods_from_group,
    reorder_group_content,
    reorder_groups,
    create_backup,
    activate_profile,
    delete_group,
    get_log_context,
    search_mods,
    get_active_mod_list,
    get_mod_info,
    get_mod_rules,
    get_mod_user_data,
    get_group_mods,
    get_mod_dependencies,
    list_log_files,
    apply_load_order_ops,
    set_mod_rule,
    # 排障 → 整理 → 获取
    get_game_status,
    launch_game,
    search_mod_text,
    parse_load_order,
    export_load_order,
    smart_insert_mods,
    set_mod_absolute_position,
    get_residue_overview,
    trace_install_sources,
    resolve_missing_dependencies,
    download_workshop_mods,
    get_download_tasks,
    check_mod_updates,
    # 获取通路补全：Steam 订阅 → WebAPI 订阅 → Git 仓库订阅 → 本地模组包
    subscribe_workshop_items,
    unsubscribe_workshop_items,
    webapi_subscribe_workshop,
    webapi_unsubscribe_workshop,
    github_fetch_repo_info,
    github_subscribe_repo,
    github_get_subscribed,
    github_trigger_download,
    github_get_timeline,
    github_remove_subscription,
    mod_package_prepare_import,
    mod_package_import,
    # 补全批（MCP 缺失能力）：任务控制 → 文件读取 → 工坊详情
    cancel_task,
    read_mod_file,
    get_workshop_mod_details,
    # 贴图转换：启动 / 查询 / 取消
    get_texture_encoder_status,
    download_texture_encoder,
    start_texture_convert,
    get_texture_convert_tasks,
    cancel_texture_convert,
)


def _build_specs() -> tuple[ToolSpec, ...]:
    specs: list[ToolSpec] = []
    for func in TOOL_FUNCTIONS:
        name = func.__name__
        specs.append(
            ToolSpec(
                func=func,
                name=name,
                method=_API_METHODS.get(name),
            )
        )
    return tuple(specs)


TOOL_SPECS: tuple[ToolSpec, ...] = _build_specs()


def annotations_for(spec: ToolSpec) -> dict[str, Any]:
    """风险分级 → MCP ToolAnnotations（readOnly/destructive 提示，两端同源）。"""
    if spec.risk == RISK_READ:
        return {"readOnlyHint": True, "destructiveHint": False}
    if spec.risk == RISK_HIGH:
        return {"readOnlyHint": False, "destructiveHint": True}
    return {"readOnlyHint": False, "destructiveHint": False}


def validate_registry() -> None:
    """自检：清单与方法映射一致（开发期断言，单测会调用）。

    只校验「映射表 → 清单」的包含关系与重名；方法名是否真的落在 API 上由
    ``backend.mcp_server.validate_bindings`` 在建 app 时对真实 api 校验
    （``spec.method`` 恒由 ``_API_METHODS.get(name)`` 派生，第三条检查在此恒为假，
    方法名校验只在 validate_bindings 做）。
    """
    names = [spec.name for spec in TOOL_SPECS]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate tool names: {names}")
    unknown = sorted(set(_API_METHODS) - set(names))
    if unknown:
        raise ValueError(f"_API_METHODS has unknown tool names: {unknown}")


validate_registry()
