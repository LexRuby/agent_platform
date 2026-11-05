"""Patent-agent installation contract."""

from scripts.install_patent_agents import PATENT_AGENT_NAMES, _templates


def test_five_patent_agents_are_installable():
    templates = _templates()
    assert tuple(templates) == PATENT_AGENT_NAMES
    assert "agentforge-profile: patent-drafting" in templates["专利权利要求智能体"]
    assert "agentforge-profile: patent-technology-extraction" in templates["专利技术信息提取智能体"]
    assert "agentforge-profile: patent-technology-analysis" in templates["专利技术方案分析智能体"]
