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


class TestSrcAccountPage:
    """账户中心（消费概览 + 发布管理，2026-09-08 v2）回归锁。

    用户需求闭环："一个账户界面，直观看到我的消费情况 + 管理我发布的
    大小A 的可见性"。消费按**产品维度**统计——平台产品 = 大A及team
    （整体消耗）或独立小A（自身消耗）：
        类型        名字       模型         输入   输出
        大A及团队   高考主理人  doubao-pro  (大A+成员合计)
        独立小A     政策研究员  glm-4.7     (自身)
    旧 /usage、/share 独立页并入 /account 双 Tab，路由保留重定向。
    """

    def test_route_registered(self):
        """/account 路由注册 + 旧 /usage、/share 重定向（入口收敛）。"""
        app = _src("App.tsx")
        assert "path: '/account'" in app, "丢失 /account 路由"
        assert "from '@/pages/account'" in app, "丢失 AccountPage 导入"
        assert "<Navigate to=\"/account\" replace />" in app, (
            "丢失旧 /usage、/share → /account 重定向"
        )

    def test_sidebar_nav_entry(self):
        """侧边栏账户入口（CircleUserRound 图标，点进 /account）。"""
        sidebar = _src("components/layout/AppSidebar.tsx")
        assert "CircleUserRound" in sidebar, "丢失账户导航图标 CircleUserRound"
        assert "navigate('/account')" in sidebar, "丢失 /account 导航跳转"
        assert "isActive={location.pathname === '/account'}" in sidebar, (
            "丢失 /account 激活态高亮"
        )
        # 旧双图标必须收编（防三入口并存）
        assert "ChartPie" not in sidebar, "旧用量图标应并入账户入口"
        assert "Share2" not in sidebar, "旧共享图标应并入账户入口"

    def test_account_page_tabs(self):
        """账户中心双 Tab：消费概览 + 智能体管理。"""
        page = _src("pages/account/index.tsx")
        for marker in (
            "UsageOverview",     # 消费概览 Tab
            "AgentManagement",   # 智能体管理 Tab（增删改查+版本+复制+发布）
        ):
            assert marker in page, f"账户中心丢失 {marker}"
        # 页头显示当前账号（authApi 链式换行，分开断言）
        assert "authApi" in page and ".me()" in page, "丢失当前账号获取"

    def test_usage_overview_product_dimension(self):
        """消费概览必须覆盖产品维度（v2 核心）：类型/名字/模型/输入/输出。"""
        page = _src("pages/account/UsageOverview.tsx")
        assert "usageApi.summary" in page, "丢失 /usage/summary API 调用"
        for marker in (
            "TotalCards",          # 总计：输入/输出/缓存/调用次数
            "DailyTrendCard",     # 每日消耗趋势
            "ProductUsageCard",   # 产品用量表（v2 核心）
            "ProductTypeBadge",   # 大A及团队 / 独立小A 徽标
            "product.members",    # team 成员构成（成本明细）
            "by_model",           # 产品行按模型拆分
        ):
            assert marker in page, f"消费概览丢失 {marker}"
        # 时间窗口切换（7/30/90 天）
        assert "RANGE_OPTIONS" in page and "90" in page, "丢失时间窗口切换"

    def test_usage_overview_states(self):
        """空状态/错误态/加载骨架必须齐备（不能白屏或裸 spinner）。"""
        page = _src("pages/account/UsageOverview.tsx")
        assert "UsageSkeleton" in page, "丢失加载骨架"
        assert "usage.empty-title" in page, "丢失空状态文案键"
        assert "usage.load-failed" in page, "丢失错误态文案键"

    def test_share_management_migrated(self):
        """智能体管理页：共享迁移完整 + 增删改查 + 版本 + 复制 + 发布（2026-09-08 v2）。"""
        page = _src("pages/account/AgentManagement.tsx")
        # 共享 v1 完整迁入（对话框 + 三种可见性 + 只读检测）
        for marker in (
            "ShareSettingDialog",
            "'private'",
            "'users'",
            "'public'",
            "agentShareApi.set",
            "sharedToMe",
            "!a.editable",   # 共享给我的只读检测（官方合并链路）
        ):
            assert marker in page, f"智能体管理丢失关键实现 {marker}"
        # v2 增删改查（用户诉求：管理界面不是只有发布）
        for marker in (
            "AgentDialog",          # 新建
            "EditAgentDialog",      # 编辑
            "DeleteDialog",         # 删除
            "agentApi.list",        # 查（列表）
            "agentApi.delete",      # 删除 API
        ):
            assert marker in page, f"智能体管理缺少增删改查组件 {marker}"
        # v2 版本管理 + 复制 + 版本化发布（三条产品化链路）
        for marker in (
            "AgentVersionDialog",      # 版本管理入口（历史版本/发版/冻结）
            "DuplicateDialog",         # 复制：当前配置或任意版本快照分叉
            "agentVersionApi.duplicate",
            "PublishDialog",           # 发布：版本快照 → 独立产品（重命名）
            "agentShareApi.publish",
            "agentShareApi.publications",  # 我的发布物列表（溯源）
        ):
            assert marker in page, f"智能体管理缺少版本化能力 {marker}"
        # 旧页面目录必须已删除（防双入口并存）
        assert not (_SRC_DIR / "pages" / "share").exists(), "旧 share 页应删除"
        assert not (_SRC_DIR / "pages" / "usage").exists(), "旧 usage 页应删除"
        assert not (_SRC_DIR / "pages" / "account" / "ShareManagement.tsx").exists(), (
            "旧 ShareManagement.tsx 应被 AgentManagement.tsx 取代"
        )

    def test_api_client_wired(self):
        """API 层必须存在并从 index 导出（缺导出页面 import 报错）。"""
        api = _src("api/usage.ts")
        assert "/usage/summary" in api, "usage API 缺少端点路径"
        index = _src("api/index.ts")
        assert "from './usage'" in index, "api/index.ts 缺 usageApi 导出"
        assert "from './agentShare'" in index, "api/index.ts 缺 agentShareApi 导出"
        assert "from './agentVersion'" in index, "api/index.ts 缺 agentVersionApi 导出（2026-09-08）"
        # 版本化发布 + 复制的端点路径（v2 新链路）
        share_api = _src("api/agentShare.ts")
        assert "'/agent-share/publish'" in share_api, "缺版本化发布端点"
        assert "'/agent-share/pubs'" in share_api, "缺发布物列表端点"
        ver_api = _src("api/agentVersion.ts")
        assert "`/agent/${agentId}/duplicate`" in ver_api, "缺复制智能体端点"
        client = _src("api/client.ts")
        assert "put: <T>" in client, "client 缺 PUT 方法（发布设置用）"
        types = _src("api/types.ts")
        for t in ("UsageSummary", "UsageTotals", "UsageByAgent", "UsageByModel",
                  "UsageByDate", "UsageProduct", "ProductMember"):
            assert f"interface {t}" in types, f"丢失 {t} 类型定义"
        types_src = types
        assert "products: UsageProduct[]" in types_src, "UsageSummary 缺 products 字段"
        assert "interface PublicationInfo" in types_src, "缺发布物类型定义（2026-09-08）"

    def test_backend_metering_and_product_aggregation(self):
        """后端计量 + 产品维度聚合 + 路由挂载必须在（前端对着它取数）。"""
        svc = (_BASE_DIR / "agent_service_app.py").read_text(encoding="utf-8")
        assert "patch_usage_metering" in svc, "服务缺实时计量钩子挂载"
        assert "backfill_usage" in svc, "服务缺存量回填启动任务"
        assert "usage_router" in svc, "服务缺 usage 路由注册"
        metering = (_BASE_DIR / "app" / "usage_metering.py").read_text(encoding="utf-8")
        assert "LeaderTeamStore" in metering, "产品聚合缺团队结构读取"
        assert '"products"' in metering, "summary 缺 products 维度输出"
        assert "member_to_leader" in metering, "缺 member→leader 归属映射"

    def test_backend_policy_injected(self):
        """官方 create_app 必须注入共享策略，否则跨账号全断。"""
        svc = (_BASE_DIR / "agent_service_app.py").read_text(encoding="utf-8")
        assert "resource_access_policy=RedisAgentSharePolicy(storage)" in svc, (
            "create_app 缺 resource_access_policy 注入"
        )
        assert "app.include_router(agent_share_router)" in svc, "缺共享管理路由注册"

    def test_version_endpoints_visibility_guard(self):
        """版本端点必须带可见性校验（提示词资产防探读）。"""
        p = (_BASE_DIR / "app" / "agent_version.py").read_text(encoding="utf-8")
        assert "_require_agent_visible" in p, "版本端点缺可见性校验"
        assert "resource_access_policy" in p, "版本校验未接入共享策略"

    def test_i18n_keys_present(self):
        for locale, title in (("zh", "账户中心"), ("en", "Account Center")):
            data = json.loads(
                (_SRC_DIR / "i18n" / "locales" / f"{locale}.json").read_text(
                    encoding="utf-8",
                ),
            )
            assert "account" in data, f"{locale}.json 丢失 account 文案节点"
            keys = ("title", "subtitle", "tab-usage", "tab-share",
                    "product-usage", "product-type", "product-type-team",
                    "product-type-agent", "team-size", "team-breakdown")
            for k in keys:
                assert k in data["account"], f"{locale}.json 丢失 account.{k}"
            assert data["account"]["title"] == title
            assert "account" in data["common"], f"{locale}.json 丢失 common.account 导航词条"
            # 消费概览沿用的 usage 键
            assert "usage" in data, f"{locale}.json 丢失 usage 文案节点"
            for k in ("total-input", "total-output", "total-cache",
                      "total-calls", "daily-trend", "empty-title"):
                assert k in data["usage"], f"{locale}.json 丢失 usage.{k}"
            # 发布管理沿用的 share 键
            assert "share" in data, f"{locale}.json 丢失 share 文案节点"
            for k in ("my-agents", "shared-to-me", "mode-private",
                      "mode-users-label", "mode-public-label", "publish",
                      "dialog-title"):
                assert k in data["share"], f"{locale}.json 丢失 share.{k}"


class TestSrcSessionFlow:
    """会话流程控制（2026-09-08 v3）：暂停/继续、任意位置重新对话、
    流程重启的前端接线静态锁。

    后端 app/session_flow.py 提供 truncate / restart / team-flow
    pause/resume 端点；前端必须在 API 层、hook、UI 三层全部接线，
    任何一层回退/漂移都会让功能静默失效（按钮点了没反应）。
    """

    def test_api_layer_wired(self):
        """session API 必须暴露全部流程控制方法与类型。"""
        api = _src("api/session.ts")
        assert "FlowOpResponse" in api, "缺流程操作响应类型"
        assert "FlowArchiveEntry" in api, "缺截断归档类型"
        for method, endpoint in (
            ("truncate", "/sessions/${sessionId}/truncate"),
            ("restart", "/sessions/${sessionId}/restart"),
            ("pauseTeamFlow", "/team-flow/${leaderSessionId}/pause"),
            ("resumeFlow", "/team-flow/${sessionId}/resume"),
            ("flowArchive", "/sessions/${sessionId}/flow-archive"),
        ):
            assert method in api, f"sessionApi 缺 {method}"
            assert endpoint in api, f"{method} 端点路径漂移: {endpoint}"
        # 类型必须从 api/index.ts 再导出（ChatViewport 等按 '@/api' 引用）
        idx = _src("api/index.ts")
        assert "FlowOpResponse" in idx and "FlowArchiveEntry" in idx, (
            "api/index.ts 未再导出流程控制类型"
        )

    def test_hook_flow_methods(self):
        """useMessages 必须暴露 truncateAt / restartFlow 并在成功后重载。"""
        hook = _src("hooks/useMessages.ts")
        assert "const truncateAt" in hook, "缺 truncateAt"
        assert "const restartFlow" in hook, "缺 restartFlow"
        assert "setReloadToken" in hook, "缺重载令牌（截断/重启后界面不刷新）"
        # 重载令牌必须挂进生命周期 effect 依赖，否则令牌变化不触发重拉
        assert "reloadToken, scheduleUpdate" in hook, (
            "reloadToken 未加入生命周期 effect 依赖"
        )

    def test_bubble_truncate_button(self):
        """消息气泡必须渲染「从这里重开」hover 按钮（分叉语义）。"""
        bubble = _src("components/chat/ASMessageBubble.tsx")
        assert "onTruncateAt" in bubble, "气泡缺 onTruncateAt prop"
        assert "truncateHere" in bubble, "缺「从这里重开」tooltip 词条引用"
        # 仅空闲消息可截断：运行中回复（无 finished_at）不显示按钮
        assert "onTruncateAt && !isRunning" in bubble, (
            "运行中的回复也显示截断按钮（后端 409 之外的二道防线）"
        )

    def test_chat_content_wiring(self):
        """ChatContent 必须把 onTruncateAt 传给气泡（空闲时才传）。"""
        content = _src("components/chat/ChatContent.tsx")
        assert "onTruncateAt" in content, "ChatContent 缺 onTruncateAt prop"
        assert "phase === 'idle' ? onTruncateAt : undefined" in content, (
            "运行中未撤下截断按钮"
        )
        # 继续按钮：wake 语义，空闲且有历史才显示
        assert "onResume" in content, "ChatContent 缺 onResume prop"
        assert "msgs.length > 0" in content, "继续按钮未限定有历史会话"

    def test_viewport_controls(self):
        """ChatViewport 必须接线全部流程控制：重启按钮 + 确认对话框 +
        团队暂停/继续 + 截断确认。"""
        vp = _src("pages/chat/ChatViewport.tsx")
        assert "truncateAt" in vp and "restartFlow" in vp, (
            "ChatViewport 未从 useMessages 解构流程控制方法"
        )
        assert "pauseTeamFlow" in vp, "缺团队暂停调用"
        assert "resumeFlow" in vp, "缺继续（wake）调用"
        assert "restartTooltip" in vp, "缺重启按钮"
        assert "restartOpen" in vp, "缺重启确认对话框状态"
        assert "truncateTarget" in vp, "缺截断确认目标状态"
        # 重启按钮必须空闲才可用（运行中重启撕裂状态）
        assert "phase !== 'idle' || flowPending" in vp, (
            "重启按钮未按运行状态禁用"
        )
        # 两种布局的 TeamFlowPanel 都要传团队控制
        assert vp.count("onPauseTeam={handlePauseTeam}") == 2, (
            "专注/经典布局的团队面板缺暂停接线"
        )
        assert vp.count("onResumeTeam={handleResumeFlow}") == 2, (
            "专注/经典布局的团队面板缺继续接线"
        )

    def test_team_panel_controls(self):
        """TeamFlowPanel 头部必须有暂停/继续按钮且受 busy 禁用。"""
        panel = _src("components/panel/TeamFlowPanel.tsx")
        assert "onPauseTeam" in panel and "onResumeTeam" in panel, (
            "面板缺团队流程控制 props"
        )
        assert "teamBusy" in panel, "缺 busy 状态（运行中可重复点击暂停）"
        assert "pauseTeam" in panel and "resumeTeam" in panel, (
            "缺暂停/继续按钮文案引用"
        )

    def test_i18n_flow_keys(self):
        """zh/en 必须包含全部流程控制文案（缺词条渲染裸键名）。"""
        for locale in ("zh", "en"):
            data = json.loads(
                (_SRC_DIR / "i18n" / "locales" / f"{locale}.json").read_text(
                    encoding="utf-8",
                ),
            )
            chat_keys = (
                "restartTooltip", "restartTitle", "restartDescription",
                "restartConfirm", "restartDone", "truncateTitle",
                "truncateDescription", "truncateConfirm", "truncateDone",
                "pauseTeamDone", "resumeDone", "resume", "resumeTooltip",
            )
            for k in chat_keys:
                assert k in data["chat"], f"{locale}.json 丢失 chat.{k}"
            assert "truncateHere" in data["messageBubble"], (
                f"{locale}.json 丢失 messageBubble.truncateHere"
            )
            tf = data["panel"]["teamFlow"]
            assert "pauseTeam" in tf and "resumeTeam" in tf, (
                f"{locale}.json 丢失 panel.teamFlow.pauseTeam/resumeTeam"
            )


class TestBrandAgentForge:
    """品牌切换（2026-09-08）：UI 从 AgentScope 原生样式切换为 Agent Forge。

    平台基于 AgentScope 二次开发，但用户可见的品牌触点（浏览器标题、
    favicon、侧边栏 logo、登录页、错误文案）必须统一为 Agent Forge，
    不得回退成上游原生品牌（否则平台"看起来像官方 demo"）。
    注意：``@agentscope-ai/...`` 是 npm 包导入路径，不属于品牌，不检查。
    """

    def test_index_html_brand(self):
        """SPA 入口：title 与 favicon 必须是 Agent Forge。"""
        html = (_SRC_DIR.parent / "index.html").read_text(encoding="utf-8")
        assert "<title>Agent Forge</title>" in html, "浏览器标题不是 Agent Forge"
        assert 'href="/agentforge.svg"' in html, "favicon 未指向 /agentforge.svg"
        assert "AgentScope" not in html, "index.html 残留 AgentScope 品牌"

    def test_sidebar_logo(self):
        """侧边栏 logo 必须用 Agent Forge mono 标识（铁砧+火花）。"""
        sidebar = _src("components/layout/AppSidebar.tsx")
        assert "agentforge_mono.svg?react" in sidebar, "侧边栏未使用 Agent Forge logo"
        assert "agentscope_mono" not in sidebar, "侧边栏残留 AgentScope logo"
        assert 'title="Agent Forge"' in sidebar, "logo 无 Agent Forge 提示"

    def test_brand_assets_exist_and_old_removed(self):
        """新品牌资产存在；旧 AgentScope svg 源文件必须删除。"""
        assets = _SRC_DIR / "assets" / "images"
        assert (assets / "agentforge_mono.svg").exists(), "缺侧边栏 mono 标识"
        assert (_SRC_DIR.parent / "public" / "agentforge.svg").exists(), "缺 favicon 源"
        assert not (assets / "agentscope_mono.svg").exists(), "旧 mono logo 未删"
        assert not (assets / "agentscope.svg").exists(), "旧彩色 logo 未删"
        assert not (_SRC_DIR.parent / "public" / "agentscope.svg").exists(), (
            "旧 favicon 源未删"
        )

    def test_deployed_favicon(self):
        """部署产物必须带新 favicon 且 index.html 引用一致。"""
        assert (_WEBUI_DIR / "agentforge.svg").exists(), "webui/ 缺 agentforge.svg"
        html = (_WEBUI_DIR / "index.html").read_text(encoding="utf-8")
        assert "<title>Agent Forge</title>" in html
        assert 'href="/agentforge.svg"' in html

    def test_login_page_brand(self):
        """后端登录页（login.html）必须是 Agent Forge 品牌。"""
        login = (_BASE_DIR / "app" / "login.html").read_text(encoding="utf-8")
        assert "Agent Forge" in login, "登录页无 Agent Forge 品牌"
        assert "AgentScope" not in login, "登录页残留 AgentScope 品牌"

    def test_auth_static_allowlist(self):
        """favicon 放行名单必须包含 /agentforge.svg（未登录可取）。"""
        auth = (_BASE_DIR / "app" / "auth.py").read_text(encoding="utf-8")
        assert '"/agentforge.svg"' in auth, "STATIC_EXACT 缺 /agentforge.svg"

    def test_i18n_no_visible_agentscope_brand(self):
        """翻译文件中用户可见文案不得出现 AgentScope（品牌统一）。"""
        for locale in ("zh", "en"):
            data = json.loads(
                (_SRC_DIR / "i18n" / "locales" / f"{locale}.json").read_text(
                    encoding="utf-8",
                ),
            )
            text = json.dumps(data, ensure_ascii=False)
            assert "AgentScope" not in text, (
                f"{locale}.json 残留用户可见的 AgentScope 文案"
            )


class TestInsecureContextPolyfill:
    """裸 IP HTTP 访问（不安全上下文）回归锁。

    2026-09-07 事故：crypto.randomUUID 在 http://<裸IP> 下为
    undefined，官方包 UserMsg 抛 TypeError 被 unhandledrejection
    监听器静默吞掉——用户点发送：输入框清空、无 POST、无报错。
    修复 = polyfill.ts 最先 import + 自有代码 uuid() 回退。
    """

    def test_polyfill_exists_and_imported_first(self):
        src = (_SRC_DIR / "polyfill.ts").read_text(encoding="utf-8")
        assert "getRandomValues" in src, "polyfill 必须用 getRandomValues 回退"
        assert "0x40" in src and "0x80" in src, "必须设置 RFC4122 v4 版本/变体位"
        assert "!crypto.randomUUID" in src, "必须判断缺失才补（安全上下文走原生）"

        main = (_SRC_DIR / "main.tsx").read_text(encoding="utf-8")
        poly_pos = main.find("import './polyfill'")
        assert poly_pos != -1, "main.tsx 必须 import polyfill"
        # 必须在应用模块（App/i18n/css）之前
        for later in ("import './index.css'", "import './i18n'", "import App"):
            assert main.find(later) > poly_pos, f"polyfill 必须先于 {later}"

    def test_no_direct_random_uuid_in_our_src(self):
        """自有源码不允许直接调用 crypto.randomUUID()（用 uuid()）。"""
        offenders = []
        for f in _SRC_DIR.rglob("*.ts*") if _SRC_DIR.exists() else []:
            if f.name in ("polyfill.ts", "uuid.ts"):
                continue
            if "crypto.randomUUID()" in f.read_text(encoding="utf-8"):
                offenders.append(str(f))
        assert not offenders, f"直接调用点必须换成 uuid()：{offenders}"

    def test_uuid_util_fallback_shape(self):
        src = (_SRC_DIR / "utils" / "uuid.ts").read_text(encoding="utf-8")
        assert "crypto.randomUUID" in src and "getRandomValues" in src
        assert "export function uuid" in src

    def test_unhandledrejection_not_silent(self):
        """unhandledrejection 监听器必须 console.error 非资产错误。"""
        main = (_SRC_DIR / "main.tsx").read_text(encoding="utf-8")
        assert "console.error('[unhandledrejection]'" in main, (
            "吞异常无痕是 2026-09-07 排查灾难的帮凶，必须留 console 痕迹"
        )


class TestMemberRoleFallback:
    """团队成员职责说明回退锁（2026-09-07 团队实测：创建成员职责全空白）。

    主理人创建的成员没有 invite_description，职责在官方生成的
    system_prompt "Your role: ..." 段——前端必须两级取值。
    """

    def test_member_role_two_level_fallback(self):
        src = (
            _SRC_DIR / "pages" / "chat" / "ChatViewport.tsx"
        ).read_text(encoding="utf-8")
        assert "invite_config?.invite_description" in src, "邀请场景字段保留"
        assert "Your role:" in src, "必须回退解析 system_prompt 的职责段"
        # 函数体顺序：invited 先判断、prompt 段回退在后
        body = src[src.find("memberRole"):]
        assert body.find("invite_config?.invite_description") < body.find(
            "Your role:",
        ), "邀请字段优先，prompt 段回退"


class TestTeamFlowPanelAutoScroll:
    """团队驾驶舱 Tab 自动跟随最新（2026-09-08 用户反馈"团队动态/
    产物不沉底"）。"""

    def test_tab_content_autoscroll(self):
        src = (
            _SRC_DIR / "components" / "panel" / "TeamFlowPanel.tsx"
        ).read_text(encoding="utf-8")
        # 滚动容器必须挂 ref + onScroll（跟随判定数据源）
        assert "tabScrollRef" in src and "handleTabScroll" in src
        assert "ref={tabScrollRef}" in src, "Tab 内容区必须绑定 ref"
        assert "onScroll={handleTabScroll}" in src, "必须监听滚动判定用户是否在底部"
        # 新事件 + 切 Tab 都要沉底（stick 判定防打扰上翻阅读）
        assert "stickToBottomRef" in src
        assert "el.scrollTop = el.scrollHeight" in src, "必须显式沉底"
        assert "scrollHeight - el.scrollTop - el.clientHeight" in src


class TestWorkflowNodeRerun:
    """工作流节点级重跑（2026-09-08 用户需求：分支对比培育）。

    点击工作流汇报节点 → 弹窗看产出 → 「新建分支重跑」（fork 保留
    旧结果）或「覆盖重跑」（truncate 直接重做）。
    """

    def test_backend_fork_endpoint(self):
        backend = (
            _BASE_DIR / "app" / "team_fork.py"
        ).read_text(encoding="utf-8")
        assert "/sessions/{session_id}/team-fork" in backend
        # upsert_session(session_id=None) 必新建：绕过官方三元组去重
        assert "state=AgentState()" in backend
        # 团队接管：set_session_team_id + team.session_id 移交
        assert "set_session_team_id" in backend
        assert "team.session_id = fork_sid" in backend
        # context 截断复用 session_flow 的锚点语义
        assert "_truncate_context_at" in backend
        # 引导语：fork 后作为新分支第一条用户消息自动触发 chat run
        assert "initial_prompt" in backend
        assert "chat_run_registry" in backend
        assert "auto_started" in backend

    def test_frontend_node_click_and_dialog(self):
        panel = (
            _SRC_DIR / "components" / "panel" / "TeamFlowPanel.tsx"
        ).read_text(encoding="utf-8")
        # 事件带宿主消息 id（重跑锚点）
        assert "msgId?: string;" in panel
        assert "const push = (e: FlowEvent) => events.push({ ...e, msgId });" in panel
        # 汇报 + 被中断节点都可点击（2026-09-08 用户反馈补充）
        assert "onSelectReport" in panel
        assert "setRerunNode" in panel
        assert "clickableEvents: [...reports, ...interrupted]" in panel
        # 中断节点弹窗说明（无产出时的引导文案）
        assert "member_interrupted" in panel
        assert "interruptedNodeDesc" in panel
        # 弹窗两个动作：fork + 覆盖（复用 truncate），均带引导语
        assert "onForkNode?.(rerunNode, rerunPrompt.trim())" in panel
        assert "onRerunNode?.(rerunNode, rerunPrompt.trim())" in panel
        assert "disabled={!rerunNode?.msgId || teamBusy}" in panel
        # 引导语输入框（培育语义：对结果不满意的改进意见）
        assert "setRerunPrompt" in panel
        assert "rerunGuideLabel" in panel
        assert "rerunGuidePlaceholder" in panel

    def test_chatviewport_fork_flow(self):
        viewport = (
            _SRC_DIR / "pages" / "chat" / "ChatViewport.tsx"
        ).read_text(encoding="utf-8")
        # fork 确认 → 调 API（带引导语）→ 跳转新分支
        assert "sessionApi.teamFork(" in viewport
        assert "forkTarget.prompt" in viewport
        assert "res.auto_started" in viewport
        assert "navigate(`/chat/${agentId}/${res.session_id}`)" in viewport
        # 覆盖重跑 = 复用消息截断 + 引导语 sessionStorage 自动发送
        assert "setTruncateTarget(e.msgId)" in viewport
        assert "agentforge:auto-prompt:" in viewport
        assert "sessionStorage.removeItem(key)" in viewport
        # 两处 TeamFlowPanel（专注/经典布局）都接线
        assert viewport.count("onForkNode={handleForkNode}") == 2
        assert viewport.count("onRerunNode={handleRerunNode}") == 2

    def test_fork_navigation_not_redirected_away(self):
        """fork 跳转不被 chat 页重定向 effect 改写（2026-09-08 修复）。

        根因：handleForkConfirm navigate 到新分支会话，但会话列表
        尚未 refetch（不含新分支），index.tsx 的"sessionId 不在列表
        → 跳到列表第一个"effect 立即把 URL 改写回旧会话——用户看到
        "点了创建分支界面没反应"。修复：freshlyForked 标记放行。
        """
        api = (_SRC_DIR / "api" / "session.ts").read_text(encoding="utf-8")
        # teamFork 成功后登记新分支 id
        assert "const freshlyForked = new Set<string>();" in api
        assert "freshlyForked.add(res.session_id);" in api
        # 查询/清除 API
        assert "export function isFreshlyForked(" in api
        assert "export function clearFreshlyForked(" in api

        index = (
            _SRC_DIR / "pages" / "chat" / "index.tsx"
        ).read_text(encoding="utf-8")
        # 重定向 effect 放行刚 fork 的分支；列表确认包含后清标记
        assert "import { clearFreshlyForked, isFreshlyForked }" in index
        assert "if (urlSessionId && isFreshlyForked(urlSessionId)) return;" in index
        assert "clearFreshlyForked(urlSessionId)" in index

        viewport = (
            _SRC_DIR / "pages" / "chat" / "ChatViewport.tsx"
        ).read_text(encoding="utf-8")
        # navigate 后立即刷新会话列表（新分支尽快出现在侧栏）
        assert "navigate(`/chat/${agentId}/${res.session_id}`);" in viewport
        # onTeamUpdated 触发列表刷新，且依赖数组包含它
        idx = viewport.index("navigate(`/chat/${agentId}/${res.session_id}`);")
        tail = viewport[idx:idx + 400]
        assert "onTeamUpdated?.();" in tail
        assert "[forkTarget, agentId, sessionId, navigate, t, onTeamUpdated]" in viewport

    def test_interrupted_node_default_prompt(self):
        """被中断节点 fork：预填默认引导语（2026-09-08 用户反馈修复）。

        用户 fork 被中断节点后无人执行该成员——fork 后 auto_started
        为 False，用户点"继续"只是空唤醒，主理人认为项目已完成直接
        收尾。修复：被中断节点打开弹窗时预填引导语，fork 即自动触发
        重跑。
        """
        panel = (
            _SRC_DIR / "components" / "panel" / "TeamFlowPanel.tsx"
        ).read_text(encoding="utf-8")
        # 点击节点时按 kind 预填（member_interrupted → 默认引导语）
        assert "e.kind === 'member_interrupted'" in panel
        assert "defaultInterruptedPrompt" in panel
        assert "setRerunPrompt(" in panel

        # i18n 键（zh + en）
        zh = json.loads(
            (_SRC_DIR / "i18n" / "locales" / "zh.json").read_text(
                encoding="utf-8",
            ),
        )
        en = json.loads(
            (_SRC_DIR / "i18n" / "locales" / "en.json").read_text(
                encoding="utf-8",
            ),
        )

        def find_key(obj, key):
            if isinstance(obj, dict):
                if key in obj:
                    return obj[key]
                for v in obj.values():
                    r = find_key(v, key)
                    if r is not None:
                        return r
            if isinstance(obj, list):
                for v in obj:
                    r = find_key(v, key)
                    if r is not None:
                        return r
            return None

        zh_text = find_key(zh, "defaultInterruptedPrompt")
        en_text = find_key(en, "defaultInterruptedPrompt")
        assert zh_text and "{{name}}" in zh_text and "被中断" in zh_text
        assert en_text and "{{name}}" in en_text and "interrupted" in en_text

    def test_backend_delete_guard_for_fork_sessions(self):
        """后端删除守卫（2026-09-08 事故修复）：删 fork 分支不解散团队。

        事故链：fork 移交调度权（team.session_id = 分支 id）→ 用户
        删除该分支 → 官方 storage 级联判定"删 leader"→ 全灭式解散
        （成员 agent 物理删除）。守卫：调度权移交最早存活会话。
        """
        backend = (
            _BASE_DIR / "app" / "team_preserve.py"
        ).read_text(encoding="utf-8")
        # 第三层守卫：storage 层 delete_session patch
        assert "_patch_delete_session_guard" in backend
        assert "RedisStorage.delete_session" in backend
        # 调度权移交语义
        assert "team.session_id = target.id" in backend
        assert "others[-1]" in backend  # created_at 最早优先
        # 无其他分支 → 解绑防级联（软解散语义）
        assert 'set_session_team_id(user_id, session_id, None)' in backend

        fork = (_BASE_DIR / "app" / "team_fork.py").read_text(encoding="utf-8")
        # fork 自愈：团队记录缺失时清死引用
        assert "fork 自愈" in fork

    def test_api_team_fork_defined(self):
        api = (_SRC_DIR / "api" / "session.ts").read_text(encoding="utf-8")
        assert "TeamForkResponse" in api
        assert "`/sessions/${sessionId}/team-fork`" in api
        # 引导语参数透传
        assert "initialPrompt?: string" in api
        assert "initial_prompt: initialPrompt || undefined" in api

    def test_i18n_keys(self):
        zh = json.loads(
            (_SRC_DIR / "i18n" / "locales" / "zh.json").read_text(
                encoding="utf-8",
            ),
        )
        tf = zh["panel"]["teamFlow"]
        assert tf["rerunFork"] == "新建分支重跑"
        assert tf["rerunOverwrite"] == "覆盖重跑"
        # 引导语（2026-09-08 二次确认）与中断节点说明
        assert "引导语" in tf["rerunGuideLabel"]
        assert "slosh-modeler" in tf["rerunGuidePlaceholder"]
        assert "被中断" in tf["interruptedNodeDesc"]
        assert zh["chat"]["forkConfirm"] == "创建分支"
        assert zh["chat"]["forkAutoStarted"] == "引导语已发送，新分支开始重跑"
        en = json.loads(
            (_SRC_DIR / "i18n" / "locales" / "en.json").read_text(
                encoding="utf-8",
            ),
        )
        assert en["panel"]["teamFlow"]["rerunFork"] == "Fork branch & rerun"
        assert "interruptedNodeDesc" in en["panel"]["teamFlow"]
        assert en["chat"]["forkConfirm"] == "Create branch"
        assert "forkAutoStarted" in en["chat"]


class TestMemberIterationBackToLeader:
    """成员迭代返回入口（2026-09-08 用户反馈：进入会话迭代后无法返回）。

    URL 三段式 /chat/:agentId/:sessionId/:memberId 聚焦成员会话时，
    ChatViewport 必须渲染"返回主理人"按钮并导航回两段式主理人会话。
    """

    def test_back_button_renders_and_navigates(self):
        viewport = (
            _SRC_DIR / "pages" / "chat" / "ChatViewport.tsx"
        ).read_text(encoding="utf-8")
        index = (_SRC_DIR / "pages" / "chat" / "index.tsx").read_text(
            encoding="utf-8",
        )
        # Props 与解构：leaderNav 由外层传入
        assert "leaderNav?: { agentId: string; sessionId: string } | null;" in viewport
        assert "leaderNav," in viewport
        # 仅成员聚焦（leaderNav 非空）时渲染按钮，点击回到两段式 URL
        assert "{leaderNav && (" in viewport, "返回按钮必须只在成员聚焦时出现"
        assert (
            "navigate(`/chat/${leaderNav.agentId}/${leaderNav.sessionId}`)" in viewport
        ), "点击必须导航回主理人会话（两段式 URL）"
        # 外层计算：三段 URL 齐全才生成 leaderNav
        assert "const leaderNav =" in index
        assert "leaderNav={leaderNav}" in index, "index.tsx 必须把 leaderNav 传给 ChatViewport"
        # i18n
        zh = json.loads(
            (_SRC_DIR / "i18n" / "locales" / "zh.json").read_text(encoding="utf-8"),
        )
        assert zh["chat"]["backToLeader"] == "返回主理人"
        en = json.loads(
            (_SRC_DIR / "i18n" / "locales" / "en.json").read_text(encoding="utf-8"),
        )
        assert en["chat"]["backToLeader"] == "Back to leader"


class TestArtifactViewerDialog:
    """产物大窗阅读（2026-09-08 用户反馈：Tab 空间太小不好看）。"""

    def test_artifact_click_opens_dialog(self):
        src = (
            _SRC_DIR / "components" / "panel" / "TeamFlowPanel.tsx"
        ).read_text(encoding="utf-8")
        # 紧凑列表：点击卡片打开大窗
        assert "setViewingArtifact(e)" in src, "产物卡片必须可点击打开大窗"
        assert "viewingArtifact" in src
        # 大窗：近全屏 + 全文 markdown
        assert (
            "flex h-[85vh] max-w-4xl flex-col sm:max-w-4xl" in src
        ), "大窗必须给足阅读空间（sm:max-w-4xl 压过 dialog 默认 sm:max-w-sm）"
        assert src.count("<Markdown") >= 2, "Tab 内与大窗都要渲染 markdown"
        # i18n 键
        zh = json.loads(
            (_SRC_DIR / "i18n" / "locales" / "zh.json").read_text(
                encoding="utf-8",
            ),
        )
        assert zh["panel"]["teamFlow"]["viewArtifact"] == "查看全文"
