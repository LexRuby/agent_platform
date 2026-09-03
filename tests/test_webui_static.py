"""webui 部署产物与前端定制补丁的回归测试。

背景事故：去掉"连接服务器"设置页时，曾把 ``getBaseUrl()`` 改成返回空字符串，
``new URL(path, '')`` 在运行时抛 "Invalid base URL"，导致前端**全部 API 请求
失败**——历史会话、表单 schema、凭据列表统统加载不出，页面却显示"正常"的
空状态。该 bug 在 TS 编译期和后端 pytest 中均不可见。

本文件从两个可测面锁住这类回归：
1. **部署产物完整性**（TestDeployedWebui）：webui/index.html 存在、引用的
   静态资源全部存在（防半截部署）、无 vite 开发模式残留；
2. **前端定制补丁内容**（TestPatchSameOrigin / TestPatchNoSetupGate）：
   重建 webui 所依赖的 ``scripts/webui-customizations.patch`` 必须包含
   同源请求基址（location.origin）、401 跳登录、移除 setup 引导门禁——
   补丁一旦回退（重建时用了旧版补丁），测试立刻转红。

前端运行时逻辑仍需浏览器端到端验证（见 TESTS.md 前端验证清单），
此处只锁"源码/产物可静态断言"的部分。
"""

import re
from pathlib import Path

import pytest

_BASE_DIR = Path(__file__).resolve().parent.parent
_WEBUI_DIR = _BASE_DIR / "webui"
_PATCH_FILE = _BASE_DIR / "scripts" / "webui-customizations.patch"


def _read_patch() -> str:
    """补丁全文；不存在时跳过（未发布环境可无补丁）。"""
    if not _PATCH_FILE.exists():
        pytest.skip("webui 定制补丁不存在")
    return _PATCH_FILE.read_text(encoding="utf-8")


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


class TestPatchSameOrigin:
    """补丁必须保留同源 API 基址的正确实现（事故回归锁）。"""

    def test_base_url_uses_location_origin(self):
        """getBaseUrl 必须返回 location.origin——空字符串会让 new URL() 抛异常。"""
        patch = _read_patch()
        assert (
            "export const getBaseUrl = () => window.location.origin;" in patch
        ), "补丁丢失同源基址实现（window.location.origin）——重建 webui 将复现全部 API 失败的事故"

    def test_base_url_not_empty_string(self):
        """空字符串基址是已确认的事故根因，出现即失败。"""
        patch = _read_patch()
        assert (
            "export const getBaseUrl = () => '';" not in patch
        ), "getBaseUrl 返回空字符串——new URL(path, '') 运行时抛 Invalid URL，前端所有请求会失败"

    def test_401_redirects_to_login(self):
        """API 401 必须跳 /login（会话过期自动回登录页，而非停留报错）。"""
        patch = _read_patch()
        assert "window.location.assign('/login');" in patch, "补丁丢失 401 → /login 跳转"


class TestPatchNoSetupGate:
    """补丁必须移除官方 UI 的"连接服务器"引导页（同源部署不该出现）。"""

    def test_setup_gate_removed(self):
        """App.tsx 的 setupComplete 门禁必须被删除（源码为 Tab 缩进，用正则容错）。"""
        patch = _read_patch()
        assert re.search(r"^-\s*const \[setupComplete, setSetupComplete\] = useState", patch, re.M), (
            "补丁未移除 setup 门禁——重建后 '连接到服务器' 设置页会回来"
        )

    def test_setup_route_redirects(self):
        """/setup 路由必须重定向到 /chat 而非渲染设置页。"""
        patch = _read_patch()
        assert "path: '/setup', element: <Navigate to=\"/chat\" replace />" in patch, (
            "补丁未将 /setup 重定向——访问旧链接会看到设置页"
        )


class TestPatchAgentVersion:
    """补丁必须包含 agent 版本封板的前端实现（功能回归锁）。

    版本封板（freeze/unfreeze/save-version/restore）的后端拦截在
    pytest（test_agent_version.py）已覆盖；但前端若丢失版本区
    （如重建 webui 用了旧补丁），用户将无法冻结/恢复——tsc 与后端
    测试均不可见，只能靠补丁内容断言 + 浏览器 E2E。
    """

    def test_version_api_module_present(self):
        """agentVersion API 模块必须在补丁里（四个端点齐全）。"""
        patch = _read_patch()
        for endpoint in (
            "/agent/${agentId}/freeze",
            "/agent/${agentId}/unfreeze",
            "/agent/${agentId}/save-version",
            "/agent/${agentId}/versions/${version}/restore",
        ):
            assert endpoint in patch, f"补丁丢失版本封板端点 {endpoint}"

    def test_edit_dialog_has_version_section(self):
        """编辑对话框必须有版本封板区：冻结/解冻按钮 + 冻结时禁用保存。"""
        patch = _read_patch()
        assert "dialog-agent-edit.version.freeze" in patch, "补丁丢失冻结封板按钮"
        assert "dialog-agent-edit.version.unfreeze" in patch, "补丁丢失解冻按钮"
        # 冻结中主保存按钮必须禁用（自我迭代停止的前端表现）
        assert "submitting || !schema || !values || frozen" in patch, (
            "补丁丢失冻结时禁用保存逻辑——冻结的 agent 仍可提交修改"
        )

    def test_version_i18n_keys_present(self):
        """zh.json 必须有版本封板文案（用户界面全中文要求）。"""
        patch = _read_patch()
        for key in (
            '"section": "版本封板"',
            '"frozenBadge": "已冻结 v{{version}}"',
            '"openBadge": "开放模式"',
            '"frozenTooltip": "已冻结封板 v{{version}}：配置固定，自我迭代停止"',
        ):
            assert key in patch, f"补丁丢失版本封板中文文案: {key}"

    def test_agent_select_frozen_badge(self):
        """agent 选择器必须有冻结徽章（锁图标 + 版本号）。"""
        patch = _read_patch()
        assert "agent.version?.frozen && (" in patch, (
            "补丁丢失选择器冻结徽章——用户无法分辨正在对话的 agent 是否已封板"
        )
