"""webui 部署产物与前端定制源码的回归测试。

背景事故：去掉"连接服务器"设置页时，曾把 ``getBaseUrl()`` 改成返回空字符串，
``new URL(path, '')`` 在运行时抛 "Invalid base URL"，导致前端**全部 API 请求
失败**——历史会话、表单 schema、凭据列表统统加载不出，页面却显示"正常"的
空状态。该 bug 在 TS 编译期和后端 pytest 中均不可见。

前端源码已并入本仓库 ``webui-src/``（原 agentscope-src 定制版的正式归宿，
2026-09-03 起不再依赖独立 git clone + patch 重建）。本文件从两个可测面锁住
回归：
1. **部署产物完整性**（TestDeployedWebui）：webui/index.html 存在、引用的
   静态资源全部存在（防半截部署）、无 vite 开发模式残留；
2. **前端源码内容**（TestSrcSameOrigin / TestSrcNoSetupGate /
   TestSrcAgentVersion / TestSrcLeaderTeam）：定制功能的关键实现直接对
   ``webui-src/src`` 源文件断言——同源基址、401 跳登录、无 setup 门禁、
   版本封板、大A/小A 分组，源码一旦回退/漂移，测试立刻转红。

前端运行时逻辑仍需浏览器端到端验证（见 TESTS.md 前端验证清单），
此处只锁"源码/产物可静态断言"的部分。
"""

import json
import re
from pathlib import Path

import pytest

_BASE_DIR = Path(__file__).resolve().parent.parent
_WEBUI_DIR = _BASE_DIR / "webui"
_SRC_DIR = _BASE_DIR / "webui-src" / "src"


def _src(rel: str) -> str:
    """读取 webui-src 源文件；不存在时跳过（未初始化前端源码的环境）。"""
    p = _SRC_DIR / rel
    if not p.exists():
        pytest.skip(f"前端源码缺失: {rel}")
    return p.read_text(encoding="utf-8")


def _src_exists(rel: str) -> bool:
    return (_SRC_DIR / rel).exists()


class TestDeployedWebui:
    """部署产物 webui/ 的静态完整性。"""

    def test_index_html_exists(self):
        """入口文件必须存在，否则服务不会挂载静态目录（静默降级为纯 API）。"""
        assert (_WEBUI_DIR / "index.html").exists(), "webui/index.html 缺失——webui 未构建或部署失败"

    def test_referenced_assets_exist(self):
        """index.html 引用的本地资源必须全部存在（防 cp 中断的半截部署）。"""
        html = (_WEBUI_DIR / "index.html").read_text(encoding="utf-8")
        refs = re.findall(r'(?:src|href)="(/[^"]+)"', html)
        assert refs, "index.html 未引用任何资源，内容可疑"
        missing = [
            r for r in refs if not r.startswith("http") and not (_WEBUI_DIR / r.lstrip("/")).exists()
        ]
        assert not missing, f"部署产物缺资源文件: {missing}"

    def test_no_vite_dev_residue(self):
        """生产构建不得引用 vite 开发客户端（dev 构建误部署会导致运行时报错）。"""
        html = (_WEBUI_DIR / "index.html").read_text(encoding="utf-8")
        assert "/@vite/client" not in html, "index.html 引用了 /@vite/client——误部署了开发构建"


class TestSrcChatWidthAlignment:
    """对话列与顶栏同宽对齐（2026-09-07 宽度错位回归锁）。

    用户反馈："顶栏宽、对话窄，对不上；对话与右侧小组面板之间
    留空白"。根因：消息/输入列被固定 48rem 居中，而顶栏全宽。
    修复：--chat-content-w 改为 100%（填满中间面板），顶栏/
    消息/输入框同宽，与右栏驾驶舱仅余边距。
    """

    def test_chat_content_fills_panel(self):
        """对话列宽度变量必须是 100%（不再固定 48rem）。"""
        t = _src("pages/chat/ChatViewport.tsx")
        assert "--chat-content-w:100%" in t, "对话列应填满中间面板"
        assert "--chat-content-w:48rem" not in t, "48rem 定宽导致与顶栏错位"


class TestSrcNoDuplicateTeamPanel:
    """团队驾驶舱不得重复渲染（2026-09-07 顶部面板重复 bug 回归锁）。

    事故：批量脚本替换 ChatViewport 时一段缩进不匹配且未加 assert，
    静默失败——顶部 TeamFlowPanel 的 classic 门控没加上，专注布局
    下顶部+右栏渲染了两份。当时的 E2E 只断言"存在性"（驾驶舱在页
    面上存在）而没断言"唯一性+位置"，重复渲染漏网。规则：驾驶舱
    必须恰好一处——专注布局在右栏、经典布局在顶部；顶部渲染点必须
    被 layoutMode === 'classic' 门控。
    """

    def test_top_panel_gated_by_classic(self):
        """顶部 TeamFlowPanel 必须带 classic 门控条件。"""
        t = _src("pages/chat/ChatViewport.tsx")
        assert "layoutMode === 'classic' && isLeader && sessionId" in t, (
            "顶部团队面板缺 classic 门控（专注布局会重复渲染）"
        )

    def test_team_flow_panel_mount_count(self):
        """TeamFlowPanel 挂载点恰好 2 处（classic 顶部 + focused 右栏）。"""
        t = _src("pages/chat/ChatViewport.tsx")
        assert t.count("<TeamFlowPanel") == 2, (
            f"TeamFlowPanel 挂载点应恰好 2 处，实际 {t.count('<TeamFlowPanel')}"
        )


class TestSrcTeamDeleteGuard:
    """TeamDelete 双层防护 + 团队 hint 紧凑化（2026-09-07 培养资产保护）。

    用户诉求："大A+Team 是要完整绑定开放给别人用的产品资产，
    完全不能接受随便删除，删除一定要我确认。"
    """

    def test_team_delete_requires_confirm(self):
        """TeamDelete 必须 bypass-immune ASK（用户确认压不住）。"""
        t = _src("../app/team_preserve.py")
        assert "check_permissions" in t and "bypass_immune=True" in t
        assert "PermissionBehavior.ASK" in t

    def test_soft_delete_at_service_layer(self):
        """软解散必须在 SessionService 层（storage 层拦不住成员物理删除）。"""
        t = _src("../app/team_preserve.py")
        assert "SessionService.delete_team" in t
        assert "cancel_session_run" in t, "必须取消成员运行（防僵尸）"
        assert "set_session_team_id" in t

    def test_team_hint_compact(self):
        """主理会话团队 hint 紧凑单行（详情在右栏驾驶舱）。"""
        t = _src("components/chat/ASMessageBubble.tsx")
        assert "TeamHintCompactContext" in t
        assert "teamHintCompact" in t  # useContext 在组件顶层（Hooks 规则）
        c = _src("components/chat/ChatContent.tsx")
        assert "compactTeamHints" in c and "TeamHintCompactContext.Provider" in c
        v = _src("pages/chat/ChatViewport.tsx")
        assert "compactTeamHints={isLeader}" in v


class TestSrcMemberTeamSession:
    """成员团队会话跳转 + RouteError 自愈（2026-09-07 培养能力重构）。

    用户反馈：点成员"进入会话迭代"进入的是空白独立会话，看不到
    成员在团队任务中的聊天内容。修复：无活跃团队时经
    GET /team-sessions/{leaderSid} 补成员历史 session_id，跳转到
    成员的团队会话（可继续对话介入培养）。
    """

    def test_route_error_self_heal(self):
        """RouteError 必须识别动态 import 失败并自动整页刷新。

        React Router 捕获懒加载路由错误后不走 unhandledrejection
        （main.tsx 的监听接不住，2026-09-07 用户卡死在错误页）。
        """
        t = _src("components/error/RouteError.tsx")
        assert "Failed to fetch dynamically imported module" in t
        assert "sessionStorage" in t and "reload" in t

    def test_chat_viewport_uses_team_history(self):
        """ChatViewport 必须调 team-sessions 补成员历史会话。"""
        t = _src("pages/chat/ChatViewport.tsx")
        assert "teamSessions" in t, "缺少团队历史 API 调用"
        assert "TeamHistoryEntry" in t
        # fallback 名单的 sessionId 来自历史映射（不再是写死 null）
        assert "historyByAgent.get(a.id) ?? null" in t

    def test_layout_button_labeled(self):
        """布局切换按钮必须带文字标签（纯图标用户找不到）。"""
        t = _src("pages/chat/ChatViewport.tsx")
        assert "classicLayout" in t and "focusedLayout" in t


class TestSrcFocusedLayout:
    """专注布局与菜单收缩（2026-09-07 用户布局重构）。

    用户诉求：中间完整对话；右侧上=团队驾驶舱、下=资源面板
    （MCP/技能/知识库 Tab）；计划/权限/团队从面板菜单移除
    （被团队工作流/顶栏权限控件覆盖）；保留一键切回经典布局。
    """

    def test_resource_tabs_panel_exists(self):
        """资源面板组件存在且聚合三类资源 Tab。"""
        t = _src("components/panel/ResourceTabsPanel.tsx")
        for key in ("'mcp'", "'skill'", "'knowledge'"):
            assert key in t, f"资源面板丢失 Tab {key}"

    def test_panel_key_shrunk(self):
        """PanelKey 只剩 mcp/skill/knowledge——计划/权限/团队不再 dock。"""
        t = _src("components/panel/PanelDock.tsx")
        assert "'mcp' | 'skill' | 'knowledge'" in t
        for gone in ("'plan'", "'permission'", "'team'"):
            assert f"{gone} |" not in t and f"| {gone}" not in t, f"PanelKey 应移除 {gone}"

    def test_focused_layout_toggle(self):
        """ChatViewport 必须有布局模式切换（focused 默认/classic）且持久化。"""
        t = _src("pages/chat/ChatViewport.tsx")
        assert "chat_layout_mode" in t, "布局偏好未持久化"
        assert "'focused'" in t and "'classic'" in t
        assert "switchToClassic" in t and "switchToFocused" in t, "缺切换按钮文案键"
        # 专注布局右侧栏：团队驾驶舱 + 资源面板
        assert "ResourceTabsPanel" in t
        # 旧面板组件不再挂载
        for gone in ("TaskPanel", "PermissionPanel", "TeamPanel"):
            assert f"<{gone}" not in t, f"{gone} 不应再被挂载"

    def test_layout_i18n_keys(self):
        """布局切换文案中英齐全。"""
        for lang in ("zh", "en"):
            d = json.loads(
                (_BASE_DIR / f"webui-src/src/i18n/locales/{lang}.json").read_text(
                    encoding="utf-8"
                )
            )
            assert "switchToClassic" in d.get("chat", {}), f"{lang} 缺 switchToClassic"
            assert "switchToFocused" in d.get("chat", {}), f"{lang} 缺 switchToFocused"


class TestSrcAssetReloadGuard:
    """动态 import 失败自愈刷新（2026-09-04 事故）：部署新版后，已打开的
    旧页面懒加载旧 hash chunk 404 → "Failed to fetch dynamically imported
    module" 页面崩溃。main.tsx 全局捕获后自动 reload（sessionStorage
    防死循环），build 脚本 assets 增量保留旧 chunk 双保险。"""

    def test_main_has_asset_reload_guard(self):
        """main.tsx 必须含自愈刷新：错误识别 + 防死循环标记 + load 清除。"""
        t = _src("main.tsx")
        assert "Failed to fetch dynamically imported module" in t
        assert "sessionStorage" in t and "reload" in t
        # 新页面加载成功后清除标记：下次部署失效还能再自愈一次
        assert 'addEventListener("load"' in t or "addEventListener('load'" in t

    def test_build_script_keeps_old_assets(self):
        """build_webui.sh 不得 rm -rf 产物目录（旧 chunk 需保留给已打开
        的旧页面），必须增量覆盖部署。"""
        script = (_BASE_DIR / "scripts" / "build_webui.sh").read_text(encoding="utf-8")
        assert "rm -rf" not in script, "禁止整目录删除：旧 hash chunk 被删会让已打开页面崩溃"
        assert "mkdir -p" in script and "cp -r dist/*" in script


class TestSrcSameOrigin:
    """前端源码必须保留同源 API 基址的正确实现（事故回归锁）。"""

    def test_base_url_uses_location_origin(self):
        """getBaseUrl 必须返回 location.origin——空字符串会让 new URL() 抛异常。"""
        client = _src("api/client.ts")
        assert (
            "export const getBaseUrl = () => window.location.origin;" in client
        ), "丢失同源基址实现（window.location.origin）——将复现全部 API 失败的事故"

    def test_base_url_not_empty_string(self):
        """空字符串基址是已确认的事故根因，出现即失败。"""
        client = _src("api/client.ts")
        assert (
            "export const getBaseUrl = () => '';" not in client
        ), "getBaseUrl 返回空字符串——new URL(path, '') 运行时抛 Invalid URL，前端所有请求会失败"

    def test_401_redirects_to_login(self):
        """API 401 必须跳 /login（会话过期自动回登录页，而非停留报错）。"""
        client = _src("api/client.ts")
        assert "window.location.assign('/login');" in client, "丢失 401 → /login 跳转"


class TestSrcNoSetupGate:
    """前端源码不得再有"连接服务器"引导页（同源部署不该出现）。"""

    def test_setup_gate_removed(self):
        """App.tsx 不得存在 setupComplete 门禁。"""
        app = _src("App.tsx")
        assert "setupComplete" not in app, "setup 门禁回来了——'连接到服务器' 设置页会再出现"

    def test_setup_route_redirects(self):
        """/setup 路由必须重定向到 /chat 而非渲染设置页。"""
        app = _src("App.tsx")
        assert "path: '/setup', element: <Navigate to=\"/chat\" replace />" in app, (
            "/setup 未重定向——访问旧链接会看到设置页"
        )


class TestSrcAgentVersion:
    """前端源码必须包含 agent 版本封板实现（功能回归锁）。

    版本封板（freeze/unfreeze/save-version/restore）的后端拦截在
    pytest（test_agent_version.py）已覆盖；但前端若丢失版本区
    （源码漂移/误回退），用户将无法冻结/恢复——tsc 与后端测试均
    不可见，只能靠源码断言 + 浏览器 E2E。
    """

    def test_version_api_module_present(self):
        """agentVersion API 模块必须存在且四个端点齐全。"""
        api = _src("api/agentVersion.ts")
        for endpoint in (
            "/agent/${agentId}/freeze",
            "/agent/${agentId}/unfreeze",
            "/agent/${agentId}/save-version",
            "/agent/${agentId}/versions/${version}/restore",
        ):
            assert endpoint in api, f"丢失版本封板端点 {endpoint}"

    def test_edit_dialog_has_version_section(self):
        """编辑对话框必须有版本封板区：冻结/解冻按钮 + 冻结时禁用保存。"""
        dialog = _src("components/dialog/EditAgentDialog.tsx")
        assert "dialog-agent-edit.version.freeze" in dialog, "丢失冻结封板按钮"
        assert "dialog-agent-edit.version.unfreeze" in dialog, "丢失解冻按钮"
        # 冻结中主保存按钮必须禁用（自我迭代停止的前端表现）
        assert "submitting || !schema || !values || frozen" in dialog, (
            "丢失冻结时禁用保存逻辑——冻结的 agent 仍可提交修改"
        )

    def test_version_i18n_keys_present(self):
        """zh.json 必须有版本封板文案（用户界面全中文要求）。"""
        zh = json.loads(_src("i18n/locales/zh.json"))
        v = zh.get("dialog-agent-edit", {}).get("version", {})
        assert v.get("section") == "版本封板", "丢失版本封板区块标题"
        assert v.get("frozenBadge") == "已冻结 v{{version}}", "丢失冻结徽章文案"
        assert v.get("openBadge") == "开放模式", "丢失开放模式文案"
        assert (
            zh.get("chat", {}).get("agent", {}).get("frozenTooltip")
            == "已冻结封板 v{{version}}：配置固定，自我迭代停止"
        ), "丢失选择器冻结徽章 tooltip"

    def test_agent_select_frozen_badge(self):
        """agent 选择器必须有冻结徽章（锁图标 + 版本号）。"""
        select = _src("components/select/AgentSelect.tsx")
        assert "agent.version?.frozen && (" in select, (
            "丢失选择器冻结徽章——用户无法分辨正在对话的 agent 是否已封板"
        )


class TestSrcLeaderTeam:
    """前端源码必须保留大A/小A（leader/member）定制（功能回归锁）。"""

    def test_leader_team_api_present(self):
        """成员推荐 API 模块必须存在。"""
        assert _src_exists("api/leaderTeam.ts"), "丢失 leaderTeam API 模块"

    def test_agent_select_groups_by_type(self):
        """agent 选择器必须按大A/小A 分组并显示徽章。"""
        select = _src("components/select/AgentSelect.tsx")
        assert "chat.agent.groupLeader" in select, "丢失大A 分组"
        assert "chat.agent.groupMember" in select, "丢失小A 分组"
        assert "chat.agent.leaderBadge" in select, "丢失大A 徽章"

    def test_team_panels_present(self):
        """团队互动流程图与团队面板组件必须存在。"""
        assert _src_exists("components/panel/TeamFlowPanel.tsx"), "丢失团队互动流程图组件"
        assert _src_exists("components/panel/TeamPanel.tsx"), "丢失团队面板组件"

    def test_team_flow_is_workflow_cockpit(self):
        """团队面板 = 工作流驾驶舱（2026-09-07 用户原型图重构）。

        核心诉求回归锁定：
        - 成员完整汇报必须解析展示（hint <team-message from=…> 全文，
          不再只显示"汇报"两字摘要）
        - 团队名（TeamCreate name）与任务描述（description）上头部卡片
        - 四 Tab：团队动态（时间轴）/ 工作流 / 成员 / 产物
        - 点成员卡片 = 本页看互动（不跳转）；跳转是成员 Tab 的次要入口
        - 时间轴渲染块级 created_at 时间戳
        """
        t = _src("components/panel/TeamFlowPanel.tsx")
        # 汇报全文解析：team-message 提取 from + 剥壳内容入 content
        assert 'team-message' in t and 'member_report' in t
        # 团队名/任务描述：TeamCreate 的 name + description
        assert 'team_created' in t and 'input.description' in t
        # 四 Tab
        for key in ("'activity'", "'workflow'", "'members'", "'artifacts'"):
            assert key in t, f"丢失 Tab {key}"
        # 点成员卡片本页看互动：focusMember 切 Tab 不调 onOpenMember
        assert 'focusMember' in t
        # 成员中断状态展示（system-reminder was interrupted）
        assert 'member_interrupted' in t and "was interrupted" in t
        # 时间轴时间戳
        assert 'created_at' in t and 'toLocaleTimeString' in t
        # i18n 键齐备
        zh = json.loads((_BASE_DIR / "webui-src/src/i18n/locales/zh.json").read_text(encoding="utf-8"))
        tf = zh["panel"]["teamFlow"]
        for key in (
            "statusRunning",
            "tabActivity",
            "tabWorkflow",
            "tabMembers",
            "tabArtifacts",
            "stReported",
            "stInterrupted",
            "evFinal",
        ):
            assert key in tf, f"zh.json panel.teamFlow 缺 {key}"


class TestSrcMCPToolsDrawer:
    """「我的 MCP」点开看工具清单（2026-09-03 用户反馈：注册的 MCP 无法点击）。"""

    def test_drawer_component_present(self):
        """工具清单抽屉组件必须存在。"""
        assert _src_exists("components/drawer/MCPToolsDrawer.tsx"), \
            "丢失 MCP 工具清单抽屉组件"

    def test_mcp_api_has_tools_endpoint(self):
        """前端 API 层必须调用 /mcp-tools/{id}。"""
        api = _src("api/mcp.ts")
        assert "/mcp-tools/" in api, "丢失 MCP 工具清单 API"

    def test_mine_panel_rows_clickable(self):
        """「我的 MCP」列表项必须可点击打开抽屉（含悬停样式与提示）。"""
        page = _src("pages/mcp/index.tsx")
        assert "setInspecting" in page, "丢失 MCP 行点击打开抽屉逻辑"
        assert "cursor-pointer" in page, "丢失 MCP 行可点击样式"
        # 编辑/删除按钮必须阻止冒泡，避免点击它们同时打开抽屉
        assert "stopPropagation" in page, "丢失按钮事件冒泡隔离"

    def test_drawer_i18n_keys_present(self):
        """抽屉文案（中文）必须存在于 zh.json。"""
        zh = json.loads(
            (_SRC_DIR / "i18n" / "locales" / "zh.json").read_text(
                encoding="utf-8",
            ),
        )
        assert "mcp-tools" in zh, "丢失 mcp-tools 文案节点"
        for key in ("itemTooltip", "toolCount", "parametersLabel"):
            assert key in zh["mcp-tools"], f"丢失 mcp-tools.{key} 文案"


class TestSrcUsagePage:
    """用量统计页（消费计量 v1，2026-09-07）回归锁。

    用户需求："我要知道我这个账号的消耗情况——模型、大A/小A、
    输入、输出"。后端 GET /usage/summary 四维度聚合，前端必须有：
    路由入口（App.tsx）+ 侧边栏导航（AppSidebar.tsx）+ 页面四区块
    （总计卡片/每日趋势/按智能体/按模型）+ API 客户端 + i18n 文案。
    任何一环被误删，页面入口消失或维度缺失，测试立刻转红。
    """

    def test_route_registered(self):
        """/usage 路由与页面组件必须注册（入口消失 = 功能不可达）。"""
        app = _src("App.tsx")
        assert "path: '/usage'" in app, "丢失 /usage 路由"
        assert "UsagePage" in app, "丢失 UsagePage 组件引用"
        assert "from '@/pages/usage'" in app, "丢失 UsagePage 导入"

    def test_sidebar_nav_entry(self):
        """侧边栏必须有"用量"导航（ChartPie 图标，点进 /usage）。"""
        sidebar = _src("components/layout/AppSidebar.tsx")
        assert "ChartPie" in sidebar, "丢失用量导航图标 ChartPie"
        assert "navigate('/usage')" in sidebar, "丢失 /usage 导航跳转"
        assert "isActive={location.pathname === '/usage'}" in sidebar, (
            "丢失 /usage 激活态高亮"
        )

    def test_page_covers_all_four_dimensions(self):
        """页面必须覆盖用户要求的全部维度：总计/日期/大A小A/模型。"""
        page = _src("pages/usage/index.tsx")
        assert "usageApi.summary" in page, "丢失 /usage/summary API 调用"
        for marker in (
            "TotalCards",       # 总计：输入/输出/缓存/调用次数
            "DailyTrendCard",   # 每日消耗趋势
            "AgentTableCard",   # 按智能体（大A/小A）
            "ModelTableCard",   # 按模型
        ):
            assert marker in page, f"用量页丢失维度组件 {marker}"
        # 时间窗口切换（7/30/90 天）
        assert "RANGE_OPTIONS" in page and "90" in page, "丢失时间窗口切换"

    def test_page_empty_and_error_states(self):
        """空状态/错误态/加载骨架必须齐备（不能白屏或裸 spinner）。"""
        page = _src("pages/usage/index.tsx")
        assert "UsageSkeleton" in page, "丢失加载骨架"
        assert "usage.empty-title" in page, "丢失空状态文案键"
        assert "usage.load-failed" in page, "丢失错误态文案键"

    def test_api_client_wired(self):
        """API 层必须存在并从 index 导出（缺导出页面 import 报错）。"""
        api = _src("api/usage.ts")
        assert "/usage/summary" in api, "usage API 缺少端点路径"
        index = _src("api/index.ts")
        assert "from './usage'" in index, "api/index.ts 缺 usageApi 导出"
        types = _src("api/types.ts")
        for t in ("UsageSummary", "UsageTotals", "UsageByAgent", "UsageByModel", "UsageByDate"):
            assert f"interface {t}" in types, f"丢失 {t} 类型定义"

    def test_i18n_keys_present(self):
        """中英文文案节点必须齐备（缺键页面渲染裸 key）。"""
        for locale, title in (("zh", "用量统计"), ("en", "Usage")):
            data = json.loads(
                (_SRC_DIR / "i18n" / "locales" / f"{locale}.json").read_text(
                    encoding="utf-8",
                ),
            )
            assert "usage" in data, f"{locale}.json 丢失 usage 文案节点"
            keys = ("title", "subtitle", "total-input", "total-output",
                    "total-cache", "total-calls", "daily-trend",
                    "by-agent", "by-model", "empty-title")
            for k in keys:
                assert k in data["usage"], f"{locale}.json 丢失 usage.{k}"
            assert data["usage"]["title"] == title
            assert "usage" in data["common"], f"{locale}.json 丢失 common.usage 导航词条"

    def test_backend_metering_intact(self):
        """后端计量模块与路由挂载必须在（前端页面对着它取数）。"""
        svc = (_BASE_DIR / "agent_service_app.py").read_text(encoding="utf-8")
        assert "patch_usage_metering" in svc, "服务缺实时计量钩子挂载"
        assert "backfill_usage" in svc, "服务缺存量回填启动任务"
        assert "usage_router" in svc, "服务缺 usage 路由注册"
