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
