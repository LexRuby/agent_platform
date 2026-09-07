/**
 * 右侧资源面板：MCP / 技能 / 知识库 三类"给会话加载的能力"合并
 * 为一个 Tab 组（2026-09-07 用户反馈：同类资源应集中一页、类型
 * 区分，不再在右上角菜单里逐个开关）。
 *
 * 数据绑定由 ChatViewport 完成，本组件只做 Tab 壳（与 PanelDock
 * 的 descriptor 思路一致，保持布局组件零业务依赖）。
 */
import { BookText, Database } from 'lucide-react';
import { useState, type ReactNode } from 'react';

import MCPSvg from '@/assets/images/mcp.svg?react';
import { Badge } from '@/components/ui/badge';
import { useTranslation } from '@/i18n/useI18n';

type Tab = 'mcp' | 'skill' | 'knowledge';

interface Props {
        /** MCP Tab 内容（<McpPanel …/>）。 */
        mcp: ReactNode;
        /** 技能 Tab 内容（<SkillPanel …/>）。 */
        skill: ReactNode;
        /** 知识库 Tab 内容（<KnowledgeBasePanel …/>）。 */
        knowledge: ReactNode;
        /** 知识库 Tab 的工具按钮（参数弹窗），渲染在 Tab 栏右侧。 */
        knowledgeActions?: ReactNode;
        /** 各 Tab 徽章计数（当前会话已加载数量）。 */
        mcpCount?: number;
        skillCount?: number;
        knowledgeCount?: number;
}

export function ResourceTabsPanel({
        mcp,
        skill,
        knowledge,
        knowledgeActions,
        mcpCount = 0,
        skillCount = 0,
        knowledgeCount = 0,
}: Props) {
        const { t } = useTranslation();
        const [tab, setTab] = useState<Tab>('mcp');

        const TABS: { key: Tab; label: string; icon: ReactNode; count: number }[] = [
                { key: 'mcp', label: 'MCP', icon: <MCPSvg className="size-3.5" />, count: mcpCount },
                {
                        key: 'skill',
                        label: t('panel.skill.title'),
                        icon: <BookText className="size-3.5" />,
                        count: skillCount,
                },
                {
                        key: 'knowledge',
                        label: t('panel.knowledge.title'),
                        icon: <Database className="size-3.5" />,
                        count: knowledgeCount,
                },
        ];

        return (
                <div className="flex min-h-0 flex-1 flex-col rounded-xl border bg-card shadow-sm">
                        {/* Tab 栏：与 TeamFlowPanel 相同的浅色下划线风格 */}
                        <div className="flex items-center border-b px-1">
                                {TABS.map((tb) => (
                                        <button
                                                key={tb.key}
                                                type="button"
                                                className={
                                                        'inline-flex items-center gap-1 border-b-2 px-2.5 py-1.5 text-xs transition-colors ' +
                                                        (tab === tb.key
                                                                ? 'border-primary font-medium text-foreground'
                                                                : 'border-transparent text-muted-foreground hover:text-foreground')
                                                }
                                                onClick={() => setTab(tb.key)}
                                        >
                                                {tb.icon}
                                                {tb.label}
                                                {tb.count > 0 && (
                                                        <Badge variant="outline" className="px-1 text-[10px]">
                                                                {tb.count}
                                                        </Badge>
                                                )}
                                        </button>
                                ))}
                                {/* 知识库参数按钮只在知识库 Tab 激活时出现 */}
                                {tab === 'knowledge' && knowledgeActions && (
                                        <span className="ml-auto pr-1">{knowledgeActions}</span>
                                )}
                        </div>
                        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
                                {tab === 'mcp' && mcp}
                                {tab === 'skill' && skill}
                                {tab === 'knowledge' && knowledge}
                        </div>
                </div>
        );
}
