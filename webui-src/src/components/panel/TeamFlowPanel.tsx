/**
 * 团队互动流程图：主理人（大A）对话页顶部面板。
 *
 * 从会话消息流解析团队互动时间线（谁对谁说了什么、达成什么结果）：
 * - tool_call TeamCreate/TeamDelete → 建队/解散事件
 * - tool_call AgentInvite（input.target = "名字@id8"）/ AgentCreate（name+description）
 *   → 成员加入（带名字与职责）
 * - tool_call TeamSay（input.to + input.content）→ 主理人 → 成员消息
 * - hint 块（source.label === 'team'）→ 成员 → 主理人汇报
 *
 * 渲染：
 * - SVG 节点连线图（主理人居上、成员卡片在下：名字 + 职责，点击成员卡片
 *   可跳转到该小A 的会话进行单独迭代优化）
 * - 边上挂消息计数，最新互动边高亮流动
 * - 下方可滚动互动时间线（中文文案 + 成员彩色标识）
 */
import { ChevronDown, ChevronUp, MessageSquare, Users } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AnimatePresence, motion } from 'framer-motion';

import type { Msg } from '@agentscope-ai/agentscope/message';

import { Badge } from '@/components/ui/badge';

export interface FlowEvent {
	kind: 'team_created' | 'member_joined' | 'message' | 'team_deleted';
	/** 原始标识：名字或 "名字@id8"（AgentInvite target / TeamSay to）。 */
	from: string;
	to: string;
	summary: string;
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
	/** 点击成员节点跳转（进入该小A 的会话）。 */
	onOpenMember?: (member: FlowMember) => void;
}

/** "名字@id8" → "名字"；broadcast → 全体成员。 */
export function displayName(raw: string, t?: (k: string) => string): string {
	if (!raw) return '';
	if (raw === 'broadcast') return t ? t('panel.teamFlow.broadcast') : '全体成员';
	return raw.split('@')[0];
}

/** 从消息流构建互动时间线 + 参与成员原始标识集合。 */
export function buildTimeline(
	msgs: Msg[],
): { events: FlowEvent[]; memberKeys: string[] } {
	const events: FlowEvent[] = [];
	const memberKeys = new Set<string>();
	const leader = msgs.find((m) => m.role === 'assistant')?.name || 'leader';

	const summarize = (s: unknown, max = 48): string => {
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
		for (const b of m.content ?? []) {
			if (b.type !== 'tool_call') continue;
			const input = parseInput(b.input);
			switch (b.name) {
				case 'TeamCreate':
					events.push({
						kind: 'team_created',
						from: m.name || leader,
						to: '',
						summary: summarize(input.name ?? input.team_name ?? '', 20),
					});
					break;
				case 'AgentInvite': {
					// target 形如 "名字@id8"；reason 是邀请理由
					const target = String(input.target ?? input.name ?? '');
					const name = target.split('@')[0] || '成员';
					if (target) memberKeys.add(target);
					events.push({
						kind: 'member_joined',
						from: m.name || leader,
						to: target || name,
						summary: summarize(
							input.reason ?? input.description ?? input.invite_description ?? '',
						),
					});
					break;
				}
				case 'AgentCreate': {
					const name = String(input.name ?? '') || '成员';
					memberKeys.add(name);
					events.push({
						kind: 'member_joined',
						from: m.name || leader,
						to: name,
						summary: summarize(input.description ?? input.role ?? ''),
					});
					break;
				}
				case 'TeamSay': {
					const to = String(input.to ?? input.target ?? '') || 'broadcast';
					if (to !== 'broadcast') memberKeys.add(to);
					events.push({
						kind: 'message',
						from: m.name || leader,
						to,
						summary: summarize(input.content ?? input.message ?? ''),
					});
					break;
				}
				case 'TeamDelete':
					events.push({
						kind: 'team_deleted',
						from: m.name || leader,
						to: '',
						summary: '',
					});
					break;
			}
		}
		// hint 块：成员 → 主理人的汇报（TeamSay 投递）
		for (const b of m.content ?? []) {
			if (b.type !== 'hint') continue;
			let sender = '';
			try {
				const src = JSON.parse(b.source ?? 'null');
				if (src && src.label === 'team') sender = String(src.sublabel ?? '');
			} catch {
				sender = '';
			}
			if (!sender) continue;
			const hintText =
				typeof b.hint === 'string'
					? b.hint
					: (b.hint ?? [])
							.map((x) => ('text' in x ? x.text : ''))
							.join(' ');
			// 剥掉 <team-message> 包装
			const text = hintText.replace(/<\/?team-message[^>]*>/g, '').trim();
			if (text) {
				events.push({
					kind: 'message',
					from: sender,
					to: leader,
					summary: summarize(text),
				});
			}
		}
	}
	return { events, memberKeys: [...memberKeys] };
}

/** 成员配色（按索引循环，浅色系保证文字可读）。 */
const PALETTE = ['#4f7cff', '#0ea5e9', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899'];

const KIND_ICON: Record<FlowEvent['kind'], string> = {
	team_created: '🏛️',
	member_joined: '👋',
	message: '💬',
	team_deleted: '🗑️',
};

export function TeamFlowPanel({ msgs, leaderName, members = [], onOpenMember }: Props) {
	const { t } = useTranslation();
	const [expanded, setExpanded] = useState(true);
	const [selected, setSelected] = useState<string | null>(null);

	const { events } = useMemo(() => buildTimeline(msgs), [msgs]);

	// 图上成员：在册成员优先，再补充事件中出现但已不在册的
	const chartMembers = useMemo(() => {
		const list: FlowMember[] = [...members];
		for (const e of events) {
			if (e.kind !== 'member_joined') continue;
			const name = e.to.split('@')[0];
			if (name && !list.some((m) => m.name === name)) {
				list.push({
					id: '',
					name,
					description: e.summary,
					sessionId: null,
				});
			}
		}
		return list;
	}, [members, events]);

	const hasTeam =
		events.some((e) => e.kind === 'team_created' || e.kind === 'member_joined') ||
		chartMembers.length > 0;

	// 每条边的消息计数（leader↔member，按显示名聚合）
	const edgeCounts = useMemo(() => {
		const counts = new Map<string, number>();
		for (const e of events) {
			if (e.kind !== 'message') continue;
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
			if (e.kind !== 'message') continue;
			const from = displayName(e.from, t);
			const to = displayName(e.to, t);
			return from === leaderName
				? `${leaderName}→${to}`
				: `${from}→${leaderName}`;
		}
		return null;
	}, [events, leaderName, t]);

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

	const handleOpen = (m: FlowMember) => {
		if (m.id && onOpenMember) onOpenMember(m);
	};

	const messageCount = events.filter((e) => e.kind === 'message').length;

	return (
		<div className="mx-auto w-full max-w-[var(--chat-content-w)] rounded-xl border bg-card shadow-sm">
			<button
				type="button"
				className="flex w-full items-center gap-2 px-3 py-2 text-sm"
				onClick={() => setExpanded((v) => !v)}
			>
				<Users className="size-3.5 text-muted-foreground" />
				<span className="font-medium">{t('panel.teamFlow.title')}</span>
				<Badge variant="outline" className="text-[10px]">
					{t('panel.teamFlow.memberCount', { count: chartMembers.length })}
				</Badge>
				<Badge variant="secondary" className="text-[10px]">
					<MessageSquare className="size-2.5" />
					{t('panel.teamFlow.messageCount', { count: messageCount })}
				</Badge>
				<span className="flex-1" />
				{expanded ? (
					<ChevronUp className="size-3.5 text-muted-foreground" />
				) : (
					<ChevronDown className="size-3.5 text-muted-foreground" />
				)}
			</button>

			<AnimatePresence initial={false}>
				{expanded && (
					<motion.div
						initial={{ height: 0, opacity: 0 }}
						animate={{ height: 'auto', opacity: 1 }}
						exit={{ height: 0, opacity: 0 }}
						transition={{ duration: 0.2 }}
						className="overflow-hidden"
					>
						{/* SVG 节点连线图（浅色系，成员卡片可点击进入会话） */}
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
									const clickable = !!m.id && !!onOpenMember;
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
											{isLatest && (
												<circle r="4" fill={colorOf(i)}>
													<animateMotion
														dur="1.6s"
														repeatCount="indefinite"
														path={`M ${leaderX} ${leaderY + 22} L ${x} ${memberY - 26}`}
													/>
												</circle>
											)}
											{/* 消息计数徽章 */}
											{count > 0 && (
												<g
													className="cursor-pointer"
													onClick={() => setSelected(selected === dn ? null : dn)}
												>
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
											{/* 成员卡片（名字 + 职责，可点击进入会话） */}
											<g
												className={clickable ? 'cursor-pointer' : ''}
												onClick={() => handleOpen(m)}
											>
												<title>
													{clickable
														? t('panel.teamFlow.openMember', { name: m.name })
														: m.name}
												</title>
												<rect
													x={x - CARD_W / 2}
													y={memberY - 26}
													width={CARD_W}
													height="52"
													rx="10"
													fill="var(--background)"
													stroke={
														selected === dn
															? colorOf(i)
															: `var(--border)`
													}
													strokeWidth={selected === dn ? 2 : 1}
												/>
												{clickable && (
													<rect
														x={x - CARD_W / 2}
														y={memberY - 26}
														width={CARD_W}
														height="52"
														rx="10"
														fill="transparent"
														className="hover:opacity-100"
														style={{ opacity: 0 }}
													/>
												)}
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
													fill="var(--muted-foreground)"
												>
													{(m.description || t('panel.teamFlow.noDescription')).slice(0, 15)}
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

						{/* 互动时间线（点击成员过滤；中文文案 + 彩色标识） */}
						<div className="no-scrollbar max-h-36 overflow-y-auto border-t px-3 py-2">
							{[...events]
								.reverse()
								.filter(
									(e) =>
										!selected ||
										displayName(e.from, t) === selected ||
										displayName(e.to, t) === selected,
								)
								.slice(0, 30)
								.map((e, i) => {
									const from = displayName(e.from, t);
									const to = displayName(e.to, t);
									return (
										<div
											key={i}
											className="flex items-baseline gap-1.5 py-0.5 text-xs"
										>
											<span>{KIND_ICON[e.kind]}</span>
											{e.kind === 'message' ? (
												<span className="min-w-0">
													<b>{from}</b>
													<span className="text-muted-foreground">
														{' '}
														{t('panel.teamFlow.saidTo')}{' '}
													</span>
													<b>{to}</b>
													<span className="text-muted-foreground">：{e.summary}</span>
												</span>
											) : e.kind === 'team_created' ? (
												<span className="text-muted-foreground">
													{t('panel.teamFlow.teamCreated', { name: e.summary })}
												</span>
											) : e.kind === 'member_joined' ? (
												<span className="text-muted-foreground">
													{t('panel.teamFlow.memberJoined', { name: to })}
													{e.summary ? ` — ${e.summary}` : ''}
												</span>
											) : (
												<span className="text-muted-foreground">
													{t('panel.teamFlow.teamDeleted')}
												</span>
											)}
										</div>
									);
								})}
						</div>
					</motion.div>
				)}
			</AnimatePresence>
		</div>
	);
}
