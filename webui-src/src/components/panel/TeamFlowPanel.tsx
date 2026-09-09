/**
 * 团队互动面板：主理人（大A）对话页顶部的"工作流驾驶舱"。
 *
 * 从会话消息流解析团队全生命周期事件（2026-09-07 按用户原型图重构）：
 * - tool_call TeamCreate → 建队（团队名 + 任务描述）
 * - tool_call AgentInvite / AgentCreate → 邀请成员（名字 + 分派任务 prompt）
 * - tool_call TeamSay → 主理人 → 成员分派
 * - hint 块（<team-message from="X">）→ 成员 → 主理人汇报（完整内容）
 * - hint system-reminder（was interrupted）→ 成员执行被中断
 * - tool_call TeamDelete → 解散；其后主理人 text → 最终方案
 * - 其余 tool_call → 工具调用统计
 *
 * 渲染（原型图结构）：
 * - 头部：团队名 + 运行状态徽章 + 任务描述
 * - SVG 关系图：主理人 + 成员卡片（点击成员 → 「成员」Tab 选中查看互动，
 *   不再直接跳转会话——2026-09-07 用户反馈"点击应看互动而非跳转"）
 * - 统计行：N 位成员 · N 次协作 · N 次工具调用 · 运行 Xs
 * - Tab：团队动态（时间轴）/ 工作流（阶段）/ 成员（卡片+互动）/ 产物（汇报全文）
 */
import {
        AlertTriangle,
        CheckCircle2,
        ChevronDown,
        ChevronUp,
        CircleStop,
        ClipboardList,
        Crown,
        GitBranch,
        Clock,
        ExternalLink,
        MessageSquare,
        Play,
        RotateCw,
        Users,
        Wrench,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AnimatePresence, motion } from 'framer-motion';

import type { Msg } from '@agentscope-ai/agentscope/message';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Markdown } from '@/components/markdown';

export interface FlowEvent {
        kind:
                | 'user_task'
                | 'team_created'
                | 'member_joined'
                | 'dispatch'
                | 'member_report'
                | 'member_interrupted'
                | 'leader_say'
                | 'tool'
                | 'final'
                | 'team_deleted';
        /** 原始标识：名字或 "名字@id8"。 */
        from: string;
        to: string;
        summary: string;
        /** 完整内容（成员汇报 markdown、分派 prompt 等）。 */
        content?: string;
        /** 事件时间（块 created_at，ISO 字符串）。 */
        time?: string;
        /** 宿主消息 id（节点级重跑的截断/fork 锚点）。 */
        msgId?: string;
}

/** 成员档案：来自团队在册名单，供名字映射与点击跳转。 */
export interface FlowMember {
        id: string;
        name: string;
        /** 职责说明（AgentCreate 的 description / 邀请理由）。 */
        description?: string;
        /** 成员会话 id（可跳转迭代）。 */
        sessionId?: string | null;
}

interface Props {
        msgs: Msg[];
        leaderName: string;
        /** 团队在册成员（view.team.members），可为空数组。 */
        members?: FlowMember[];
        /** 是否有在册团队（view.team 存在）。false = 已解散/未组队：
         *  成员列表是历史/预置回退名单（2026-09-09 用户反馈"团队没了
         *  右侧还显示团队"——此前静默回退无任何标注）。 */
        teamActive?: boolean;
        /** 进入成员会话单独迭代（成员 Tab 的次要入口）。 */
        onOpenMember?: (member: FlowMember) => void;
        /** 节点级 fork（2026-09-08）：从该节点新建分支重跑后续链路。 */
        onForkNode?: (e: FlowEvent, prompt: string) => void;
        /** 节点级覆盖重跑：截断该节点之后重新处理（复用 truncate）。 */
        onRerunNode?: (e: FlowEvent, prompt: string) => void;
        /** 团队暂停（2026-09-08 v3）：中断 leader + 取消全部成员运行。 */
        onPauseTeam?: () => void;
        /** 团队继续：唤醒 leader 从当前状态恢复调度。 */
        onResumeTeam?: () => void;
        /** 解散团队（2026-09-09）：用户主动，软解散语义——LLM 的
         *  TeamDelete 已被无条件 DENY，这是唯一解散入口。 */
        onDissolveTeam?: () => void;
        /** leader 回复进行中或流程操作进行中（禁用控制按钮）。 */
        teamBusy?: boolean;
}

type Tab = 'activity' | 'workflow' | 'members' | 'artifacts';

/** "名字@id8" → "名字"；broadcast → 全体成员。 */
export function displayName(raw: string, t?: (k: string) => string): string {
        if (!raw) return '';
        if (raw === 'broadcast') return t ? t('panel.teamFlow.broadcast') : '全体成员';
        return raw.split('@')[0];
}

/** 块/消息时间 → 本地 HH:MM:SS（user 块带 Z 为 UTC，assistant 块为本地时间，
 * Date 解析后都得到正确的本地显示）。 */
function fmtTime(iso?: string): string {
        if (!iso) return '';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        return d.toLocaleTimeString('zh-CN', { hour12: false });
}

/** 两个 ISO 时间差（秒），无效返回 null。 */
function diffSeconds(a?: string, b?: string): number | null {
        if (!a || !b) return null;
        const ta = new Date(a).getTime();
        const tb = new Date(b).getTime();
        if (Number.isNaN(ta) || Number.isNaN(tb)) return null;
        return Math.max(0, Math.round((tb - ta) / 1000));
}

/** 从消息流构建团队全生命周期时间线 + 参与成员原始标识集合。 */
export function buildTimeline(
        msgs: Msg[],
): { events: FlowEvent[]; memberKeys: string[] } {
        const events: FlowEvent[] = [];
        const memberKeys = new Set<string>();
        const leader = msgs.find((m) => m.role === 'assistant')?.name || 'leader';

        const summarize = (s: unknown, max = 64): string => {
                const text = typeof s === 'string' ? s : JSON.stringify(s ?? '');
                return text.length > max ? text.slice(0, max) + '…' : text;
        };

	// 真实消息流里 tool_call 的 input 是 JSON 字符串（非对象），
	// 直接 .target 会取到 undefined——曾致邀请事件全部显示为无名"成员"
	const parseInput = (raw: unknown): Record<string, unknown> => {
		if (typeof raw === 'string') {
			try {
				const v = JSON.parse(raw);
				return typeof v === 'object' && v !== null ? (v as Record<string, unknown>) : {};
			} catch {
				return {};
			}
		}
		return typeof raw === 'object' && raw !== null ? (raw as Record<string, unknown>) : {};
	};

        for (const m of msgs) {
                const msgId = typeof m.id === 'string' ? m.id : undefined;
                // 事件统一携带宿主消息 id（节点级重跑的锚点）
                const push = (e: FlowEvent) => events.push({ ...e, msgId });

                // TeamDelete 后主理人的说明 = 最终方案（同一消息内标志传递）
                let teamDeletedSeen = events.some((e) => e.kind === 'team_deleted');

                for (const b of m.content ?? []) {
                        if (typeof b !== 'object' || b === null) continue;
                        const blk = b as unknown as Record<string, unknown>;
                        const time = typeof blk.created_at === 'string' ? blk.created_at : undefined;

                        if (blk.type === 'text' && typeof blk.text === 'string' && blk.text.trim()) {
                                if (m.role === 'user') {
                                        push({
                                                kind: 'user_task',
                                                from: 'user',
                                                to: leader,
                                                summary: summarize(blk.text, 80),
                                                content: blk.text,
                                                time,
                                        });
                                } else {
                                        push({
                                                kind: teamDeletedSeen ? 'final' : 'leader_say',
                                                from: m.name || leader,
                                                to: 'user',
                                                summary: summarize(blk.text, 80),
                                                content: blk.text,
                                                time,
                                        });
                                }
                                continue;
                        }

                        if (blk.type !== 'tool_call') continue;
                        const name = String(blk.name ?? '');
                        const input = parseInput(blk.input);
                        switch (name) {
                                case 'TeamCreate':
                                        push({
                                                kind: 'team_created',
                                                from: m.name || leader,
                                                to: '',
                                                summary: summarize(input.name ?? input.team_name ?? '', 24),
                                                content:
                                                        typeof input.description === 'string'
                                                                ? input.description
                                                                : undefined,
                                                time,
                                        });
                                        teamDeletedSeen = false;
                                        break;
                                case 'AgentInvite': {
                                        // target 形如 "名字@id8"；prompt 是分派给成员的任务
                                        const target = String(input.target ?? input.name ?? '');
                                        const memberName = target.split('@')[0] || '成员';
                                        if (target) memberKeys.add(target);
                                        push({
                                                kind: 'member_joined',
                                                from: m.name || leader,
                                                to: target || memberName,
                                                summary: summarize(
                                                        input.prompt ??
                                                                input.reason ??
                                                                input.description ??
                                                                '',
                                                ),
                                                content:
                                                        typeof input.prompt === 'string'
                                                                ? input.prompt
                                                                : undefined,
                                                time,
                                        });
                                        break;
                                }
                                case 'AgentCreate': {
                                        const memberName = String(input.name ?? '') || '成员';
                                        memberKeys.add(memberName);
                                        push({
                                                kind: 'member_joined',
                                                from: m.name || leader,
                                                to: memberName,
                                                summary: summarize(input.description ?? input.role ?? ''),
                                                content:
                                                        typeof input.description === 'string'
                                                                ? input.description
                                                                : undefined,
                                                time,
                                        });
                                        // AgentCreate 的 prompt 即成员的首次任务分派
                                        // （主理人重建团队时的任务下发方式）。2026-09-08
                                        // 用户反馈"任务都下发了，工作流没变化"——此前
                                        // 只有 TeamSay 才产生 dispatch 节点，AgentCreate
                                        // 的任务分派在链路上不可见
                                        if (typeof input.prompt === 'string' && input.prompt.trim()) {
                                                push({
                                                        kind: 'dispatch',
                                                        from: m.name || leader,
                                                        to: memberName,
                                                        summary: summarize(input.prompt),
                                                        content: input.prompt,
                                                        time,
                                                });
                                        }
                                        break;
                                }
                                case 'TeamSay': {
                                        const to = String(input.to ?? input.target ?? '') || 'broadcast';
                                        if (to !== 'broadcast') memberKeys.add(to);
                                        push({
                                                kind: 'dispatch',
                                                from: m.name || leader,
                                                to,
                                                summary: summarize(input.content ?? input.message ?? ''),
                                                content:
                                                        typeof input.content === 'string'
                                                                ? input.content
                                                                : undefined,
                                                time,
                                        });
                                        break;
                                }
                                case 'TeamDelete':
                                        push({
                                                kind: 'team_deleted',
                                                from: m.name || leader,
                                                to: '',
                                                summary: '',
                                                time,
                                        });
                                        teamDeletedSeen = true;
                                        break;
                                default:
                                        // 其他工具调用（MCP 检索等）计入工具统计
                                        push({
                                                kind: 'tool',
                                                from: m.name || leader,
                                                to: '',
                                                summary: name,
                                                time,
                                        });
                        }
                }

                // hint 块：成员 → 主理人的汇报（team-message）或系统提醒（中断）
                for (const b of m.content ?? []) {
                        if (typeof b !== 'object' || b === null) continue;
                        const blk = b as unknown as Record<string, unknown>;
                        if (blk.type !== 'hint') continue;
                        const time = typeof blk.created_at === 'string' ? blk.created_at : undefined;
                        let label = '';
                        try {
                                const src = JSON.parse(String(blk.source ?? 'null'));
                                if (src && typeof src.label === 'string') label = src.label;
                        } catch {
                                label = '';
                        }
                        const hintText = typeof blk.hint === 'string' ? blk.hint : '';
                        if (!hintText) continue;

                        if (label === 'team') {
                                // "<team-message from=\"高考志愿兵\">完整汇报…"
                                const fm = hintText.match(/<team-message[^>]*from="([^"]+)"/);
                                const sender = fm?.[1] ?? '';
                                if (!sender) continue;
                                memberKeys.add(sender);
                                const text = hintText
                                        .replace(/<\/?team-message[^>]*>/g, '')
                                        .trim();
                                if (text) {
                                        push({
                                                kind: 'member_report',
                                                from: sender,
                                                to: leader,
                                                summary: summarize(text, 80),
                                                content: text,
                                                time,
                                        });
                                }
                        } else if (label === 'System') {
                                // "Team member '政策研究员' was interrupted mid-task…"
                                const im = hintText.match(
                                        /Team member '([^']+)' was interrupted/,
                                );
                                if (im) {
                                        memberKeys.add(im[1]);
                                        push({
                                                kind: 'member_interrupted',
                                                from: im[1],
                                                to: leader,
                                                summary: '',
                                                time,
                                        });
                                }
                        }
                }
        }
        return { events, memberKeys: [...memberKeys] };
}

/** 成员配色（按索引循环，浅色系保证文字可读）。 */
const PALETTE = ['#4f7cff', '#0ea5e9', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899'];

const KIND_ICON: Record<FlowEvent['kind'], string> = {
        user_task: '👤',
        team_created: '🏛️',
        member_joined: '👋',
        dispatch: '📋',
        member_report: '📤',
        member_interrupted: '⚠️',
        leader_say: '💬',
        tool: '🔧',
        final: '🏁',
        team_deleted: '🗑️',
};

export function TeamFlowPanel({
        msgs,
        leaderName,
        members = [],
        teamActive,
        onOpenMember,
        onForkNode,
        onRerunNode,
        onPauseTeam,
        onResumeTeam,
        onDissolveTeam,
        teamBusy = false,
}: Props) {
        const { t } = useTranslation();
        const [expanded, setExpanded] = useState(true);
        const [tab, setTab] = useState<Tab>('activity');
        /** 选中的成员（成员 Tab / 时间轴过滤）。 */
        const [focus, setFocus] = useState<string | null>(null);
        /** 大窗阅读中的产物（null = 关闭）。 */
        const [viewingArtifact, setViewingArtifact] = useState<FlowEvent | null>(null);
        /** 重跑弹窗中的节点（null = 关闭）。 */
        const [rerunNode, setRerunNode] = useState<FlowEvent | null>(null);
        /** 重跑引导语：对结果不满意的改进意见（随重跑自动发送）。 */
        const [rerunPrompt, setRerunPrompt] = useState('');

        const { events } = useMemo(() => buildTimeline(msgs), [msgs]);

        // Tab 内容区自动跟随最新（2026-09-08 用户反馈"团队动态/产物
        // 不沉底"）：新事件到达时若视口在底部附近（< 60px）则自动
        // 滚到最底；用户上翻阅读历史时不打扰。切 Tab 时也回到最底。
        const tabScrollRef = useRef<HTMLDivElement>(null);
        const stickToBottomRef = useRef(true);

        const handleTabScroll = () => {
                const el = tabScrollRef.current;
                if (!el) return;
                stickToBottomRef.current =
                        el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        };

        useEffect(() => {
                const el = tabScrollRef.current;
                if (el && stickToBottomRef.current) {
                        el.scrollTop = el.scrollHeight;
                }
        }, [events, tab]);

        useEffect(() => {
                // 切 Tab 视为"回到最新"：恢复跟随并沉底
                stickToBottomRef.current = true;
                const el = tabScrollRef.current;
                if (el) el.scrollTop = el.scrollHeight;
        }, [tab]);

        // 团队名 / 任务描述：最近一次 TeamCreate
        const teamMeta = useMemo(() => {
                const created = [...events]
                        .reverse()
                        .find((e) => e.kind === 'team_created');
                const firstUser = events.find((e) => e.kind === 'user_task');
                return {
                        name: created?.summary || '',
                        description: created?.content || firstUser?.summary || '',
                };
        }, [events]);

        // 运行中判定：在册团队（teamActive）优先——无在册团队时即使
        // 消息流里有组队事件（已解散/回看历史），也不显示"运行中"
        // （2026-09-09 用户反馈：团队没了右侧还显示团队）
        const hasTeamEvents = useMemo(
                () =>
                        events.some(
                                (e) =>
                                        e.kind === 'team_created' ||
                                        e.kind === 'member_joined' ||
                                        e.kind === 'team_deleted',
                        ),
                [events],
        );
        const teamDeleted = useMemo(
                () => events.some((e) => e.kind === 'team_deleted'),
                [events],
        );
        const running = useMemo(
                () =>
                        (teamActive ??
                                events.some(
                                        (e) =>
                                                e.kind === 'team_created' ||
                                                e.kind === 'member_joined',
                                )) && !teamDeleted,
                [teamActive, events, teamDeleted],
        );

        // 图上成员：在册成员优先，再补充事件中出现但已不在册的
        const chartMembers = useMemo(() => {
                const list: FlowMember[] = [...members];
                for (const e of events) {
                        if (e.kind !== 'member_joined') continue;
                        const name = e.to.split('@')[0];
                        if (name && !list.some((m) => m.name === name)) {
                                list.push({ id: '', name, description: e.summary, sessionId: null });
                        }
                }
                return list;
        }, [members, events]);

        const hasTeam =
                events.some((e) => e.kind === 'team_created' || e.kind === 'member_joined') ||
                chartMembers.length > 0;

        // 每条边的消息计数（leader↔member，按显示名聚合：分派 + 汇报）
        const edgeCounts = useMemo(() => {
                const counts = new Map<string, number>();
                for (const e of events) {
                        if (e.kind !== 'dispatch' && e.kind !== 'member_report') continue;
                        const from = displayName(e.from, t);
                        const to = displayName(e.to, t);
                        const key = from === leaderName ? `${leaderName}→${to}` : `${from}→${leaderName}`;
                        counts.set(key, (counts.get(key) ?? 0) + 1);
                }
                return counts;
        }, [events, leaderName, t]);

        // 最新一次互动的边（高亮流动动画）
        const latestEdge = useMemo(() => {
                for (let i = events.length - 1; i >= 0; i--) {
                        const e = events[i];
                        if (e.kind !== 'dispatch' && e.kind !== 'member_report') continue;
                        const from = displayName(e.from, t);
                        const to = displayName(e.to, t);
                        return from === leaderName
                                ? `${leaderName}→${to}`
                                : `${from}→${leaderName}`;
                }
                return null;
        }, [events, leaderName, t]);

        // 统计：协作 = 分派 + 汇报；工具 = 全部 tool_call（事件流 tool 块）
        const stats = useMemo(() => {
                const collabs = events.filter(
                        (e) => e.kind === 'dispatch' || e.kind === 'member_report',
                ).length;
                const tools = events.filter((e) => e.kind === 'tool').length;
                const times = events
                        .map((e) => e.time)
                        .filter((x): x is string => !!x);
                const first = times[0];
                const end =
                        events.find((e) => e.kind === 'team_deleted')?.time ?? times[times.length - 1];
                const dur = diffSeconds(first, end);
                return { collabs, tools, dur };
        }, [events]);

        // 产物：成员汇报全文（时间正序）
        // 产物 = 成员汇报 + 主理人交付（2026-09-09 用户反馈"产物只有
        // 5 个 agent 的产出，主理人的呢？最终产物呢？"——产物 Tab
        // 此前只收 member_report，主理人整合的最终报告（报告级长文）
        // 在对话框里却不在产物里，中间对话与右侧面板割裂）。
        // 规则：成员汇报全收；主理人的报告级输出（≥500 字，如最终
        // 答案/交付清单）与解散后的收尾说明（final）也收——按时序
        // 排列，主理人的最终交付天然排在成员产物之后（链条完整）。
        const artifacts = useMemo(
                () =>
                        events.filter(
                                (e) =>
                                        e.kind === 'member_report' ||
                                        e.kind === 'final' ||
                                        (e.kind === 'leader_say' &&
                                                (e.content?.length ?? 0) >= 500),
                        ),
                [events],
        );

        // 无任何团队互动时不渲染（放在全部 hooks 之后，避免条件调用 hooks）。
        if (!hasTeam) return null;

        // SVG 布局：leader 居上中，成员卡片均分下排（两行：名字 + 职责）
        const CARD_W = 148;
        const W = Math.max(360, chartMembers.length * (CARD_W + 16) + 16);
        const H = 216;
        const leaderX = W / 2;
        const leaderY = 40;
        const memberY = 152;
        const memberX = (i: number) =>
                chartMembers.length === 1
                        ? W / 2
                        : CARD_W / 2 + 12 + (i * (W - CARD_W - 24)) / (chartMembers.length - 1);

        const colorOf = (i: number) => PALETTE[i % PALETTE.length];

        /** 点击成员卡片：切到成员 Tab 并选中（看互动，不跳转）。 */
        const focusMember = (m: FlowMember) => {
                setTab('members');
                setFocus(m.name);
        };

        /** 点击边计数：切到动态 Tab 并过滤该成员。 */
        const focusEdge = (dn: string) => {
                setTab('activity');
                setFocus(focus === dn ? null : dn);
        };

        /** 成员执行状态：已汇报 / 被中断 / 执行中 / 待命 / 已停止。
         *
         * 2026-09-09 用户反馈"结果都产出了，成员里还有 2 人显示执行
         * 中"——旧状态机把"从未汇报"一律当执行中：团队解散后挂起的
         * 分派永远显示执行中；从未被分派任务的成员也显示执行中。
         * 修正：解散后未完成的分派 → 已停止；从未分派 → 待命；
         * 只有"有未闭环分派且团队在册"才是执行中。 */
        const memberStatus = (
                name: string,
        ): 'reported' | 'interrupted' | 'working' | 'standby' | 'stopped' => {
                const dn = (raw: string) => displayName(raw);
                if (events.some((e) => e.kind === 'member_report' && dn(e.from) === name))
                        return 'reported';
                if (events.some((e) => e.kind === 'member_interrupted' && dn(e.from) === name))
                        return 'interrupted';
                const dispatched = events.some(
                        (e) => e.kind === 'dispatch' && dn(e.to) === name,
                );
                if (!dispatched) return 'standby';
                if (teamDeleted) return 'stopped';
                return 'working';
        };

        const timelineEvents = events.filter(
                (e) =>
                        !focus ||
                        displayName(e.from, t) === focus ||
                        displayName(e.to, t) === focus,
        );

        const TABS: { key: Tab; label: string }[] = [
                { key: 'activity', label: t('panel.teamFlow.tabActivity') },
                { key: 'workflow', label: t('panel.teamFlow.tabWorkflow') },
                { key: 'members', label: t('panel.teamFlow.tabMembers') },
                { key: 'artifacts', label: t('panel.teamFlow.tabArtifacts') },
        ];

        return (
                <div className="mx-auto w-full max-w-[var(--chat-content-w)] rounded-xl border bg-card shadow-sm">
					{/* 头部：团队名 + 状态 + 任务描述（可折叠）+ 流程控制 */}
					<div className="flex w-full items-center gap-2 px-3 py-2 text-sm">
						<button
							type="button"
							className="flex min-w-0 flex-1 items-center gap-2 text-left"
							onClick={() => setExpanded((v) => !v)}
						>
							<Users className="size-3.5 shrink-0 text-muted-foreground" />
							<span className="truncate font-medium">
								{teamMeta.name || t('panel.teamFlow.title')}
							</span>
							<Badge
								variant={running ? 'default' : 'secondary'}
								className="gap-1 text-[10px]"
							>
								{running ? (
									<span className="size-1.5 animate-pulse rounded-full bg-primary-foreground" />
								) : teamDeleted ? (
									<AlertTriangle className="size-2.5" />
								) : (
									<CheckCircle2 className="size-2.5" />
								)}
								{running
									? t('panel.teamFlow.statusRunning')
									: teamDeleted
										? t('panel.teamFlow.statusDissolved')
										: t('panel.teamFlow.statusEnded')}
							</Badge>
						</button>
						{/* 团队流程控制（2026-09-08 v3）：暂停 = leader +
						    全部成员停止（上下文保留）；继续 = 唤醒 leader
						    从当前状态恢复调度。stopPropagation 防触发折叠。
						    前置条件：有在册团队（teamActive）——已解散/未组队
						    时按钮隐藏，不存在"暂停一个不存在的团队"
						    （2026-09-09 用户反馈：设计语言一致性）。 */}
						{teamActive !== false && (onPauseTeam || onResumeTeam) && (
							<div className="flex shrink-0 items-center gap-1">
								{onPauseTeam && (
									<Button
										variant="ghost"
										size="sm"
										className="h-7 gap-1 px-2 text-xs"
										disabled={teamBusy}
										title={t('panel.teamFlow.pauseTeam')}
										onClick={(e) => {
											e.stopPropagation();
											onPauseTeam();
										}}
									>
										<CircleStop className="size-3.5" />
										<span className="hidden md:inline">
											{t('panel.teamFlow.pauseTeam')}
										</span>
									</Button>
								)}
								{onResumeTeam && (
										<Button
											variant="ghost"
											size="sm"
											className="h-7 gap-1 px-2 text-xs"
											disabled={teamBusy}
											title={t('panel.teamFlow.resumeTeam')}
											onClick={(e) => {
												e.stopPropagation();
												onResumeTeam();
											}}
										>
											<Play className="size-3.5" />
											<span className="hidden md:inline">
												{t('panel.teamFlow.resumeTeam')}
											</span>
										</Button>
									)}
									{/* 解散团队（2026-09-09）：唯一解散入口——
									    LLM 的 TeamDelete 已被无条件 DENY，解散是
									    用户的主动操作（软解散，资产保留） */}
									{onDissolveTeam && (
										<Button
											variant="ghost"
											size="sm"
											className="h-7 gap-1 px-2 text-xs text-destructive hover:text-destructive"
											disabled={teamBusy}
											title={t('panel.teamFlow.dissolveTeamTooltip')}
											onClick={(e) => {
												e.stopPropagation();
												onDissolveTeam();
											}}
										>
											<AlertTriangle className="size-3.5" />
											<span className="hidden md:inline">
												{t('panel.teamFlow.dissolveTeam')}
											</span>
										</Button>
									)}
								</div>
							)}
						<button
							type="button"
							className="shrink-0 text-muted-foreground"
							onClick={() => setExpanded((v) => !v)}
						>
							{expanded ? (
								<ChevronUp className="size-3.5" />
							) : (
								<ChevronDown className="size-3.5" />
							)}
						</button>
					</div>

					<AnimatePresence initial={false}>
						{expanded && (
							<motion.div
								initial={{ height: 0, opacity: 0 }}
								animate={{ height: 'auto', opacity: 1 }}
								exit={{ height: 0, opacity: 0 }}
								transition={{ duration: 0.2 }}
								className="overflow-hidden"
							>
								{/* 任务描述 */}
								{teamMeta.description && (
									<div className="px-3 pb-2 text-xs text-muted-foreground">
										<ClipboardList className="mr-1 inline size-3" />
										{t('panel.teamFlow.taskLabel')}：{teamMeta.description.slice(0, 120)}
									</div>
								)}

								{/* SVG 节点连线图（浅色系；点成员=看互动，点计数=过滤动态） */}
								<div className="px-2 pb-1">
									<svg
										viewBox={`0 0 ${W} ${H}`}
										className="h-[216px] w-full"
										preserveAspectRatio="xMidYMid meet"
									>
										{chartMembers.map((m, i) => {
											const x = memberX(i);
											const dn = m.name;
											const key = `${leaderName}→${dn}`;
											const count =
												(edgeCounts.get(key) ?? 0) +
												(edgeCounts.get(`${dn}→${leaderName}`) ?? 0);
											const isLatest =
												latestEdge === key || latestEdge === `${dn}→${leaderName}`;
											const midX = (leaderX + x) / 2;
											const midY = (leaderY + memberY) / 2 + 10;
											const st = memberStatus(dn);
											return (
												<g key={`${m.id || m.name}`}>
													{/* 连线 */}
													<line
														x1={leaderX}
														y1={leaderY + 22}
														x2={x}
														y2={memberY - 26}
														stroke={isLatest ? colorOf(i) : 'var(--border)'}
														strokeWidth={isLatest ? 2 : 1.5}
													/>
													{isLatest && running && (
														<circle r="4" fill={colorOf(i)}>
															<animateMotion
																dur="1.6s"
																repeatCount="indefinite"
																path={`M ${leaderX} ${leaderY + 22} L ${x} ${memberY - 26}`}
															/>
														</circle>
													)}
													{/* 消息计数徽章：点击过滤该成员动态 */}
													{count > 0 && (
														<g
															className="cursor-pointer"
															onClick={() => focusEdge(dn)}
														>
															<title>
																{t('panel.teamFlow.edgeCountHint', { name: dn })}
															</title>
															<rect
																x={midX - 12}
																y={midY - 10}
																width="24"
																height="20"
																rx="10"
																fill="var(--background)"
																stroke={colorOf(i)}
															/>
															<text
																x={midX}
																y={midY + 4}
																textAnchor="middle"
																fontSize="11"
																fill={colorOf(i)}
																fontWeight="600"
															>
																{count}
															</text>
														</g>
													)}
													{/* 成员卡片：点击切到成员 Tab 看互动（不跳转） */}
													<g className="cursor-pointer" onClick={() => focusMember(m)}>
														<title>
															{t('panel.teamFlow.viewMemberInteractions', { name: dn })}
														</title>
														<rect
															x={x - CARD_W / 2}
															y={memberY - 26}
															width={CARD_W}
															height="52"
															rx="10"
															fill="var(--background)"
															stroke={focus === dn ? colorOf(i) : 'var(--border)'}
															strokeWidth={focus === dn ? 2 : 1}
														/>
														<circle cx={x - CARD_W / 2 + 14} cy={memberY - 8} r="4" fill={colorOf(i)} />
														<text
															x={x - CARD_W / 2 + 24}
															y={memberY - 4}
															fontSize="12"
															fontWeight="600"
															fill="var(--foreground)"
														>
															{m.name.slice(0, 9)}
														</text>
														<text
															x={x - CARD_W / 2 + 24}
															y={memberY + 12}
															fontSize="9.5"
															fill={
																st === 'interrupted'
																		? '#d97706'
																		: st === 'reported'
																			? colorOf(i)
																			: 'var(--muted-foreground)'
															}
														>
															{(st === 'reported'
																? '✓ '
																: st === 'interrupted'
																	? '⚠ '
																	: '') +
																(m.description || t('panel.teamFlow.noDescription')).slice(
																		0,
																		st === 'working' ? 15 : 12,
																)}
														</text>
													</g>
												</g>
											);
										})}

										{/* 主理人节点 */}
										<motion.g
											initial={{ scale: 0.8, opacity: 0 }}
											animate={{ scale: 1, opacity: 1 }}
										>
											<rect
												x={leaderX - 64}
												y={leaderY - 20}
												width="128"
												height="40"
												rx="12"
												fill="var(--primary)"
											/>
											<text
												x={leaderX}
												y={leaderY + 5}
												textAnchor="middle"
												fontSize="13"
												fontWeight="700"
												fill="var(--primary-foreground)"
											>
												{leaderName.slice(0, 9)}
											</text>
											<text
												x={leaderX}
												y={leaderY - 26}
												textAnchor="middle"
												fontSize="10"
												fill="var(--muted-foreground)"
											>
												{t('panel.teamFlow.leaderLabel')}
											</text>
										</motion.g>
									</svg>
								</div>

								{/* 统计行 */}
								<div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t px-3 py-1.5 text-[11px] text-muted-foreground">
									<span className="inline-flex items-center gap-1">
										<Users className="size-3" />
										{t('panel.teamFlow.memberCount', { count: chartMembers.length })}
									</span>
									<span className="inline-flex items-center gap-1">
										<MessageSquare className="size-3" />
										{t('panel.teamFlow.collabCount', { count: stats.collabs })}
									</span>
									<span className="inline-flex items-center gap-1">
										<Wrench className="size-3" />
										{t('panel.teamFlow.toolCount', { count: stats.tools })}
									</span>
									{stats.dur !== null && stats.dur > 0 && (
										<span className="inline-flex items-center gap-1">
											<Clock className="size-3" />
											{t('panel.teamFlow.duration', { sec: stats.dur })}
										</span>
									)}
								</div>

								{/* Tabs */}
								<div className="flex border-b px-1">
									{TABS.map((tb) => (
										<button
											key={tb.key}
											type="button"
											className={
												'border-b-2 px-2.5 py-1.5 text-xs transition-colors ' +
												(tab === tb.key
													? 'border-primary font-medium text-foreground'
													: 'border-transparent text-muted-foreground hover:text-foreground')
											}
											onClick={() => setTab(tb.key)}
										>
											{tb.label}
										</button>
									))}
									{focus && (
										<button
											type="button"
											className="ml-auto self-center pr-2 text-[10px] text-muted-foreground underline"
											onClick={() => setFocus(null)}
										>
											{t('panel.teamFlow.clearFilter')}：{focus}
										</button>
									)}
								</div>

								{/* Tab 内容区：自动跟随最新（上翻阅读时不打扰） */}
					<div
						ref={tabScrollRef}
						onScroll={handleTabScroll}
						className="no-scrollbar max-h-64 overflow-y-auto px-3 py-2"
					>
									{tab === 'activity' && (
										<div className="space-y-1">
												{timelineEvents.map((e, i) => (
														<TimelineRow key={i} e={e} leaderName={leaderName} />
												))}
												{timelineEvents.length === 0 && (
														<div className="py-2 text-center text-xs text-muted-foreground">
																{t('panel.teamFlow.noEvents')}
														</div>
												)}
										</div>
									)}

									{tab === 'workflow' && (
										<PipelineView
											events={events}
											leaderName={leaderName}
											onSelectReport={(e) => {
                                                                                        // 被中断节点：预填默认引导语——fork 后
                                                                                        // auto_started 直接重跑该成员（2026-09-08
                                                                                        // 用户反馈：fork 后没人执行被中断节点）
                                                                                        setRerunNode(e);
                                                                                        setRerunPrompt(
                                                                                                e.kind === 'member_interrupted'
                                                                                                        ? t('panel.teamFlow.defaultInterruptedPrompt', {
                                                                                                                  name: displayName(e.from, t),
                                                                                                          })
                                                                                                        : '',
                                                                                        );
                                                                                }}
										/>
									)}

									{tab === 'members' && (
										<div className="space-y-2">
												{/* 无在册团队时的明确标注（2026-09-09）：
												    成员列表是历史/预置回退名单，不是在册团队 */}
												{teamActive === false && hasTeamEvents && (
													<div className="flex items-start gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2 py-1.5 text-[11px] leading-relaxed text-amber-700">
														<AlertTriangle className="mt-0.5 size-3 shrink-0" />
														<span>
															{t('panel.teamFlow.teamDissolvedBanner')}
														</span>
													</div>
												)}
												{teamActive === false && !hasTeamEvents && (
													<div className="flex items-start gap-1.5 rounded-md border border-muted bg-muted/50 px-2 py-1.5 text-[11px] leading-relaxed text-muted-foreground">
														<Users className="mt-0.5 size-3 shrink-0" />
														<span>
															{t('panel.teamFlow.teamNotFormedBanner')}
														</span>
													</div>
												)}
												{/* 主理人卡片（2026-09-09 用户反馈"工作流、成员、
												    产物上都不涉及主理人"——培育的是大A+Team 整体，
												    主理人是团队的一员）：置顶展示，点击过滤其执行
												    动态（说明/工具/最终交付） */}
												{(() => {
														const leaderEvents = events.filter(
																(e) =>
																		e.kind === 'leader_say' ||
																		e.kind === 'final' ||
																		e.kind === 'tool' ||
																		e.kind === 'user_task',
														);
														const detail = focus === leaderName;
														return (
																<div
																		className="rounded-lg border-2 border-primary/60 bg-primary/5 p-2"
																>
																		<div className="flex items-center gap-2">
																				<Crown className="size-3.5 text-primary" />
																				<button
																						type="button"
																						className="text-sm font-semibold"
																						onClick={() => setFocus(detail ? null : leaderName)}
																				>
																						{leaderName}
																				</button>
																				<Badge className="bg-primary/10 text-[10px] text-primary">
																						{t('panel.teamFlow.leaderBadge')}
																				</Badge>
																				<span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
																						{t('panel.teamFlow.leaderDesc')}
																				</span>
																		</div>
																		{detail && leaderEvents.length > 0 && (
																				<div className="mt-2 max-h-40 space-y-1 overflow-y-auto border-t pt-2">
																						{leaderEvents.map((e, j) => (
																								<TimelineRow key={j} e={e} leaderName={leaderName} />
																						))}
																				</div>
																		)}
																</div>
														);
												})()}
												{chartMembers.map((m, i) => {
														const st = memberStatus(m.name);
														const color = colorOf(i);
														const detail = focus === m.name;
														const memberEvents = events.filter(
																(e) =>
																		displayName(e.from) === m.name ||
																		displayName(e.to) === m.name,
														);
														return (
																<div
																		key={m.id || m.name}
																		className="rounded-lg border p-2"
																		style={detail ? { borderColor: color } : undefined}
																>
																		<div className="flex items-center gap-2">
																				<span className="size-2 rounded-full" style={{ background: color }} />
																				<button
																						type="button"
																						className="text-sm font-medium"
																						onClick={() => setFocus(detail ? null : m.name)}
																				>
																						{m.name}
																				</button>
																				<Badge
																						variant={st === 'reported' ? 'default' : 'secondary'}
																						className="text-[10px]"
																				>
																						{st === 'reported'
																								? t('panel.teamFlow.stReported')
																								: st === 'interrupted'
																										? t('panel.teamFlow.stInterrupted')
																										: st === 'standby'
																												? t('panel.teamFlow.stStandby')
																												: st === 'stopped'
																														? t('panel.teamFlow.stStopped')
																														: t('panel.teamFlow.stWorking')}
																				</Badge>
																				<span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
																						{m.description || t('panel.teamFlow.noDescription')}
																				</span>
																				{m.id && onOpenMember && (
																						<button
																								type="button"
																								className="inline-flex shrink-0 items-center gap-0.5 text-[10px] text-muted-foreground underline"
																								onClick={() => onOpenMember(m)}
																						>
																								<ExternalLink className="size-2.5" />
																								{t('panel.teamFlow.openMemberShort')}
																						</button>
																				)}
																		</div>
																		{detail && memberEvents.length > 0 && (
																				<div className="mt-2 space-y-1 border-t pt-2">
																						{memberEvents.map((e, j) => (
																								<TimelineRow key={j} e={e} leaderName={leaderName} />
																						))}
																				</div>
																		)}
																</div>
														);
												})}
										</div>
									)}

									{tab === 'artifacts' && (
										<div className="space-y-2">
												{artifacts.map((e, i) => {
														// 主理人产出（报告级长文/final）：显示"主理人"标签；
														// 列表中最后一条主理人产物 = 最终交付，金色高亮
														const isLeader = e.kind !== 'member_report';
														const leaderArts = artifacts.filter(
																(a) => a.kind !== 'member_report',
														);
														const isFinal =
																isLeader && e === leaderArts[leaderArts.length - 1];
														return (
														<button
																key={i}
																type="button"
																className={
																		'w-full rounded-lg border p-2 text-left transition-colors hover:bg-muted/50 ' +
																		(isFinal ? 'border-amber-300 bg-amber-50/60' : '')
																}
																onClick={() => setViewingArtifact(e)}
														>
																<div className="flex items-center gap-2 text-xs">
																		<span className="font-medium">
																				{displayName(e.from, t)}
																		</span>
																		{isLeader && (
																				<span className="rounded bg-primary/10 px-1 text-[10px] font-medium text-primary">
																						{t('panel.teamFlow.leaderArtifactBadge')}
																				</span>
																		)}
																		{isFinal && (
																				<span className="rounded bg-amber-100 px-1 text-[10px] font-medium text-amber-700">
																						{t('panel.teamFlow.finalArtifactBadge')}
																				</span>
																		)}
																		<span className="text-muted-foreground">
																				{t('panel.teamFlow.artifactOf')} · {fmtTime(e.time)}
																		</span>
																		<span className="ml-auto text-[10px] text-muted-foreground">
																				{t('panel.teamFlow.viewArtifact')} →
																		</span>
																</div>
																<div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
																		{e.summary}
																</div>
														</button>
														);
												})}
												{artifacts.length === 0 && (
														<div className="py-2 text-center text-xs text-muted-foreground">
																{t('panel.teamFlow.noArtifactsV2')}
														</div>
												)}
										</div>
									)}
								</div>
							</motion.div>
						)}
					</AnimatePresence>

					{/* 产物大窗阅读（2026-09-08 用户反馈：Tab 空间太小）：
					    紧凑列表点击 → 近全屏弹窗看汇报全文 markdown */}
					<Dialog
						open={!!viewingArtifact}
						onOpenChange={(v) => {
							if (!v) setViewingArtifact(null);
						}}
					>
						<DialogContent className="flex h-[85vh] max-w-4xl flex-col sm:max-w-4xl">
							<DialogHeader>
								<DialogTitle className="flex items-center gap-2 text-base">
									<ClipboardList className="size-4 text-muted-foreground" />
									{viewingArtifact
										? `${displayName(viewingArtifact.from, t)} · ${t('panel.teamFlow.artifactOf')}`
										: ''}
								</DialogTitle>
								{viewingArtifact && (
									<p className="text-xs text-muted-foreground">
										{fmtTime(viewingArtifact.time)}
									</p>
								)}
							</DialogHeader>
							<div className="min-h-0 flex-1 overflow-y-auto pr-2">
								<Markdown className="text-sm">
									{viewingArtifact?.content ||
										viewingArtifact?.summary ||
										''}
								</Markdown>
							</div>
						</DialogContent>
				</Dialog>

				{/* 节点级重跑弹窗（2026-09-08 用户需求：
				    点击工作流节点 → 看产出 → 分支/覆盖重跑；
				    二次确认补充：引导语输入（对结果不满意的改进
				    意见，作为新分支/截断后的第一条消息自动发送） */}
				<Dialog
					open={!!rerunNode}
					onOpenChange={(v) => {
						if (!v) {
							setRerunNode(null);
							setRerunPrompt('');
						}
					}}
				>
					<DialogContent className="flex h-[85vh] max-w-4xl flex-col sm:max-w-4xl">
						<DialogHeader>
							<DialogTitle className="flex items-center gap-2 text-base">
								<RotateCw className="size-4 text-muted-foreground" />
								{rerunNode
									? `${displayName(rerunNode.from, t)} · ${t('panel.teamFlow.nodeRerunTitle')}`
								: ''}
							</DialogTitle>
							<p className="text-xs text-muted-foreground">
								{t('panel.teamFlow.nodeRerunDesc')}
							</p>
						</DialogHeader>
						<div className="min-h-0 flex-1 overflow-y-auto pr-2">
							{rerunNode?.kind === 'member_interrupted' ? (
								<p className="rounded-md border border-amber-300/60 bg-amber-50 p-3 text-sm text-amber-700 dark:border-amber-700/60 dark:bg-amber-950/40 dark:text-amber-300">
									{t('panel.teamFlow.interruptedNodeDesc', {
										name: displayName(rerunNode.from, t),
									})}
								</p>
							) : (
								<Markdown className="text-sm">
									{rerunNode?.content ||
										rerunNode?.summary ||
										''}
								</Markdown>
							)}
						</div>
						{/* 引导语：培育语义——告诉主理人如何改进 */}
						<div className="mt-2 space-y-1">
							<label
								htmlFor="rerun-prompt"
								className="text-xs font-medium text-foreground"
							>
								{t('panel.teamFlow.rerunGuideLabel')}
							</label>
							<textarea
								id="rerun-prompt"
								className="min-h-[64px] w-full resize-y rounded-md border bg-background px-2 py-1.5 text-sm outline-none focus-visible:ring-1 focus-visible:ring-ring"
								placeholder={t('panel.teamFlow.rerunGuidePlaceholder')}
								value={rerunPrompt}
								onChange={(e) => setRerunPrompt(e.target.value)}
							/>
						</div>
						<div className="flex flex-col gap-2 border-t pt-3 sm:flex-row sm:justify-end">
							<Button
								variant="outline"
								disabled={!rerunNode?.msgId || teamBusy}
								onClick={() => {
									if (rerunNode) onRerunNode?.(rerunNode, rerunPrompt.trim());
									setRerunNode(null);
									setRerunPrompt('');
								}}
							>
								<RotateCw className="size-4" />
								{t('panel.teamFlow.rerunOverwrite')}
							</Button>
							<Button
								disabled={!rerunNode?.msgId || teamBusy}
								onClick={() => {
									if (rerunNode) onForkNode?.(rerunNode, rerunPrompt.trim());
									setRerunNode(null);
									setRerunPrompt('');
								}}
							>
								<GitBranch className="size-4" />
								{t('panel.teamFlow.rerunFork')}
							</Button>
						</div>
					</DialogContent>
				</Dialog>
				</div>
		);
	}

/** 时间轴行：时间 + 图标 + 事件描述 + 可展开内容。 */
function TimelineRow({ e, leaderName }: { e: FlowEvent; leaderName: string }) {
        const { t } = useTranslation();
        const [open, setOpen] = useState(false);
        const from = displayName(e.from, t);
        const to = displayName(e.to, t);
        const hasContent = !!e.content && e.content.length > 90;

        const text = (() => {
                switch (e.kind) {
                        case 'user_task':
                                return (
                                        <>
                                                <b>{t('panel.teamFlow.evUser')}</b>
                                                <span className="text-muted-foreground">：{e.summary}</span>
                                        </>
                                );
                        case 'team_created':
                                return (
                                        <span className="text-muted-foreground">
                                                {t('panel.teamFlow.teamCreated', { name: e.summary })}
                                        </span>
                                );
                        case 'member_joined':
                                return (
                                        <span className="text-muted-foreground">
                                                {t('panel.teamFlow.memberJoined', { name: to })}
                                                {e.summary ? ` — ${e.summary}` : ''}
                                        </span>
                                );
                        case 'dispatch':
                                return (
                                        <span className="min-w-0">
                                                <b>{from}</b>
                                                <span className="text-muted-foreground">
                                                        {' '}
                                                        {t('panel.teamFlow.evDispatch')}{' '}
                                                </span>
                                                <b>{to}</b>
                                                <span className="text-muted-foreground">：{e.summary}</span>
                                        </span>
                                );
                        case 'member_report':
                                return (
                                        <span className="min-w-0">
                                                <b>{from}</b>
                                                <span className="text-muted-foreground">
                                                        {' '}
                                                        {t('panel.teamFlow.evReport')}{' '}
                                                </span>
                                                <b>{leaderName}</b>
                                                <span className="text-muted-foreground">：{e.summary}</span>
                                        </span>
                                );
                        case 'member_interrupted':
                                return (
                                        <span className="text-amber-600">
                                                {t('panel.teamFlow.evInterrupted', { name: from })}
                                        </span>
                                );
                        case 'leader_say':
                                return (
                                        <span className="min-w-0">
                                                <b>{from}</b>
                                                <span className="text-muted-foreground">
                                                        {' '}
                                                        {t('panel.teamFlow.evLeaderSay')}：{e.summary}
                                                </span>
                                        </span>
                                );
                        case 'tool':
                                return (
                                        <span className="text-muted-foreground">
                                                {t('panel.teamFlow.evTool', { name: e.summary })}
                                        </span>
                                );
                        case 'final':
                                return (
                                        <span className="min-w-0">
                                                <b>{from}</b>
                                                <span className="text-muted-foreground">
                                                        {' '}
                                                        {t('panel.teamFlow.evFinal')}：{e.summary}
                                                </span>
                                        </span>
                                );
                        case 'team_deleted':
                                return (
                                        <span className="text-muted-foreground">
                                                {t('panel.teamFlow.teamDeleted')}
                                        </span>
                                );
                }
        })();

        return (
                <div className="text-xs">
                        <div className="flex items-baseline gap-1.5 py-0.5">
                                <span className="shrink-0 tabular-nums text-[10px] text-muted-foreground">
                                        {fmtTime(e.time)}
                                </span>
                                <span className="shrink-0">{KIND_ICON[e.kind]}</span>
                                <button
                                        type="button"
                                        className={'min-w-0 text-left' + (hasContent ? ' cursor-pointer' : '')}
                                        onClick={hasContent ? () => setOpen((v) => !v) : undefined}
                                >
                                        {text}
                                        {hasContent && (
                                                <span className="ml-1 text-[10px] text-muted-foreground">
                                                        {open ? '▲' : '▼'}
                                                </span>
                                        )}
                                </button>
                        </div>
                        {open && e.content && (
                                <div className="ml-8 mb-1 rounded-md border bg-background p-2">
                                        <Markdown className="text-xs">{e.content}</Markdown>
                                </div>
                        )}
                </div>
        );
}

/**
 * 工作流 Tab：从上至下的执行流水线（2026-09-09 用户需求重做）。
 *
 * 用户任务 → 组队 → 分派 → 汇报/中断 → … → 最终交付，严格按时序
 * 竖排；每个分派节点带执行状态（进行中/已完成/被中断/已停止——由
 * 其后同成员的下一个汇报/中断事件推导；团队解散后未闭环的分派为
 * 已停止）。主理人（大A）是团队的一员：其每个执行回合（说明 +
 * 工具操作）聚合为一个节点，与成员节点同链展示（2026-09-09 用户
 * 反馈"工作流不涉及主理人、消息框很多对话工作流却很少"）。任务
 * 节点均可点击：查看详情 + 从该节点新建分支重跑（分支对比培育的
 * 核心入口，任意节点皆可重开）。
 */
function PipelineView({
        events,
        leaderName,
        onSelectReport,
}: {
        events: FlowEvent[];
        leaderName: string;
        onSelectReport?: (e: FlowEvent) => void;
}) {
        const { t } = useTranslation();
        const dn = (raw: string) => displayName(raw, t);

        const teamDeleted = events.some((e) => e.kind === 'team_deleted');

        /** 分派节点的执行状态：向后扫描同成员的下一个汇报/中断
         *  （在下一次同成员分派之前）——都无即"进行中"；团队解散
         *  后未闭环 → "已停止"（2026-09-09：不再永远显示执行中）。 */
        const statusOf = (
                ev: FlowEvent,
                idx: number,
        ): 'working' | 'done' | 'interrupted' | 'stopped' => {
                const member = dn(ev.to);
                for (let j = idx + 1; j < events.length; j++) {
                        const e = events[j];
                        if (e.kind === 'dispatch' && dn(e.to) === member) break;
                        if (e.kind === 'member_report' && dn(e.from) === member)
                                return 'done';
                        if (e.kind === 'member_interrupted' && dn(e.from) === member)
                                return 'interrupted';
                }
                return teamDeleted ? 'stopped' : 'working';
        };

        /** 主理人执行节点：同一宿主消息内的 leader_say + tool 事件
         *  聚合为一个节点（叙述文本 + 工具操作数）——消息框里主理
         *  人的每个回合（思考/写代码/跑脚本/补位成员）在工作流上
         *  都有对应节点，不再"只看到成员汇报"。 */
        const leaderGroups = new Map<
                string,
                { narration?: string; tools: number; firstIdx: number }
        >();
        events.forEach((e, i) => {
                if (e.kind !== 'leader_say' && e.kind !== 'tool') return;
                const key = e.msgId ?? `idx:${i}`;
                const g = leaderGroups.get(key);
                if (g) {
                        if (e.kind === 'tool') g.tools += 1;
                        else if (!g.narration) g.narration = e.summary;
                } else {
                        leaderGroups.set(key, {
                                narration: e.kind === 'leader_say' ? e.summary : undefined,
                                tools: e.kind === 'tool' ? 1 : 0,
                                firstIdx: i,
                        });
                }
        });

        type NodeStatus =
                | 'working'
                | 'done'
                | 'interrupted'
                | 'stopped'
                | 'leader'
                | 'final'
                | 'deleted'
                | 'minor';
        const nodes: {
                ev: FlowEvent;
                label: string;
                status: NodeStatus;
                /** 聚合节点（主理人回合）的自定义摘要。 */
                summaryOverride?: string;
        }[] = [];
        events.forEach((e, i) => {
                switch (e.kind) {
                        case 'user_task':
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeUserTask'),
                                        status: 'done',
                                });
                                break;
                        case 'team_created':
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeTeamCreated'),
                                        status: 'minor',
                                });
                                break;
                        case 'member_joined':
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeMemberJoined', {
                                                name: dn(e.to),
                                        }),
                                        status: 'minor',
                                });
                                break;
                        case 'dispatch':
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeDispatch', {
                                                name: dn(e.to),
                                        }),
                                        status: statusOf(e, i),
                                });
                                break;
                        case 'member_report':
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeReport', {
                                                name: dn(e.from),
                                        }),
                                        status: 'done',
                                });
                                break;
                        case 'member_interrupted':
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeInterrupted', {
                                                name: dn(e.from),
                                        }),
                                        status: 'interrupted',
                                });
                                break;
                        case 'final':
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeFinal', {
                                                name: leaderName,
                                        }),
                                        status: 'final',
                                });
                                break;
                        case 'team_deleted':
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeTeamDeleted'),
                                        status: 'deleted',
                                });
                                break;
                        case 'leader_say':
                        case 'tool': {
                                // 主理人执行节点：每个宿主消息聚合一次
                                // （firstIdx 命中时发射，其余跳过）
                                const key = e.msgId ?? `idx:${i}`;
                                const g = leaderGroups.get(key);
                                if (!g || g.firstIdx !== i) break;
                                const summaryOverride = g.narration
                                        ? g.tools > 0
                                                ? `${g.narration}（${t(
                                                          'panel.teamFlow.pipeToolCount',
                                                          { count: g.tools },
                                                  )}）`
                                                : g.narration
                                        : t('panel.teamFlow.pipeToolCount', {
                                                  count: g.tools,
                                          });
                                nodes.push({
                                        ev: e,
                                        label: t('panel.teamFlow.pipeLeader', {
                                                name: leaderName,
                                        }),
                                        status: 'leader',
                                        summaryOverride,
                                });
                                break;
                        }
                }
        });

        if (nodes.length === 0) {
                return (
                        <div className="py-2 text-center text-xs text-muted-foreground">
                                {t('panel.teamFlow.noEvents')}
                        </div>
                );
        }

        // 状态 → 圆点样式（进行中脉冲动画）
        const DOT_STYLE: Record<NodeStatus, string> = {
                working: 'bg-blue-500 animate-pulse',
                done: 'bg-emerald-500',
                interrupted: 'bg-amber-500',
                stopped: 'bg-slate-400',
                leader: 'bg-primary',
                final: 'bg-primary',
                deleted: 'bg-muted-foreground/50',
                minor: 'bg-muted-foreground/40',
        };
        const ST_LABEL: Record<NodeStatus, string> = {
                working: t('panel.teamFlow.stWorking'),
                done: t('panel.teamFlow.stDone'),
                interrupted: t('panel.teamFlow.stInterrupted'),
                stopped: t('panel.teamFlow.stStopped'),
                leader: '',
                final: t('panel.teamFlow.stFinal'),
                deleted: '',
                minor: '',
        };

        // 可点击节点：任务流节点（用户任务/分派/汇报/被中断/主理人
        // 回合）——任意节点皆可重开分支
        const clickable = (kind: FlowEvent['kind']) =>
                kind === 'dispatch' ||
                kind === 'member_report' ||
                kind === 'member_interrupted' ||
                kind === 'user_task' ||
                kind === 'leader_say' ||
                kind === 'tool';

        return (
                <div>
                        {nodes.map((n, i) => {
                                const canClick = clickable(n.ev.kind) && !!onSelectReport;
                                const body = (
                                        <>
                                                <span
                                                        className={
                                                                'inline-block size-2 shrink-0 rounded-full ' +
                                                                DOT_STYLE[n.status]
                                                        }
                                                />
                                                <span className="shrink-0 text-[10px] text-muted-foreground">
                                                        {fmtTime(n.ev.time)}
                                                </span>
                                                <span className="shrink-0 text-xs font-medium">
                                                        {n.label}
                                                </span>
                                                {ST_LABEL[n.status] && (
                                                        <span
                                                                className={
                                                                        'shrink-0 rounded px-1 text-[10px] ' +
                                                                        (n.status === 'working'
                                                                                ? 'bg-blue-100 text-blue-700'
                                                                                : n.status === 'done'
                                                                                        ? 'bg-emerald-100 text-emerald-700'
                                                                                        : n.status === 'interrupted'
                                                                                                ? 'bg-amber-100 text-amber-700'
                                                                                                : n.status === 'stopped'
                                                                                                        ? 'bg-slate-200 text-slate-600'
                                                                                                        : 'bg-muted text-muted-foreground')
                                                                }
                                                        >
                                                                {ST_LABEL[n.status]}
                                                        </span>
                                                )}
                                                <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">
                                                        {n.summaryOverride ?? n.ev.summary}
                                                </span>
                                                {canClick && (
                                                        <span className="shrink-0 text-primary">↻</span>
                                                )}
                                        </>
                                );
                                return (
                                        <div key={i} className="flex items-center gap-1.5 py-0.5">
                                                {canClick ? (
                                                        <button
                                                                type="button"
                                                                className="flex min-w-0 flex-1 items-center gap-1.5 rounded px-1 py-0.5 text-left transition-colors hover:bg-muted/50"
                                                                onClick={() => onSelectReport?.(n.ev)}
                                                                title={t('panel.teamFlow.nodeRerunHint')}
                                                        >
                                                                {body}
                                                        </button>
                                                ) : (
                                                        <div className="flex min-w-0 flex-1 items-center gap-1.5 px-1 py-0.5">
                                                                {body}
                                                        </div>
                                                )}
                                        </div>
                                );
                        })}
                </div>
        );
}
