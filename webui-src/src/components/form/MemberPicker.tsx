/**
 * 预置团队成员选择器：创建/编辑主理人（大A）时勾选在册小A。
 *
 * - member 列表多选（名称 + 描述 + 复选框）
 * - 「AI 推荐」：把 system_prompt / 任务议题 + 在册清单交给大模型，
 *   返回推荐卡片（名称 + 推荐理由），一键勾选
 * - 名单是"预置"而非限制——运行时主理人仍可邀请他人或临时创建新成员
 */
import { Bot, Check, Loader2, Sparkles, Users } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { leaderTeamApi, type MemberRecommendation } from '@/api/leaderTeam';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useAgents } from '@/hooks/useAgents';
import { formatApiErrorForAlert } from '@/lib/api-error';

interface Props {
	/** 已选 member agent id 列表 */
	selected: string[];
	onChange: (ids: string[]) => void;
	/** 主理人系统提示词（推荐上下文） */
	systemPrompt?: string;
	/** 排除的 agent id（编辑自己时） */
	excludeIds?: string[];
}

export function MemberPicker({ selected, onChange, systemPrompt, excludeIds = [] }: Props) {
	const { t } = useTranslation();
	const { agents } = useAgents();
	const [topic, setTopic] = useState('');
	const [recommending, setRecommending] = useState(false);
	const [recommendations, setRecommendations] = useState<MemberRecommendation[] | null>(null);
	const [recError, setRecError] = useState('');

	const memberAgents = useMemo(
		() =>
			(agents ?? []).filter(
				(a) =>
					a.agent_type !== 'leader' &&
					!excludeIds.includes(a.id) &&
					a.editable !== false,
			),
		[agents, excludeIds],
	);

	const toggle = (id: string) => {
		onChange(
			selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id],
		);
	};

	const runRecommend = async () => {
		setRecError('');
		setRecommending(true);
		setRecommendations(null);
		try {
			const res = await leaderTeamApi.recommendMembers({
				system_prompt: systemPrompt || undefined,
				task_topic: topic || undefined,
				members: memberAgents.map((a) => ({
					id: a.id,
					name: a.data.name,
					description: '',
				})),
			});
			setRecommendations(res.recommendations);
			if (res.fallback) {
				setRecError(t('dialog-agent-create.member.recommendFallback'));
			}
		} catch (e) {
			setRecError(formatApiErrorForAlert(e));
		} finally {
			setRecommending(false);
		}
	};

	const canRecommend = !!(
		(systemPrompt || '').trim() ||
		topic.trim()
	);

	if (memberAgents.length === 0) {
		return (
			<div className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
				{t('dialog-agent-create.member.noCandidates')}
			</div>
		);
	}

	return (
		<div className="space-y-2">
			<div className="flex items-center gap-1.5 text-sm font-medium">
				<Users className="size-3.5" />
				{t('dialog-agent-create.member.title')}
				<Badge variant="outline" className="text-[10px]">
					{selected.length}
				</Badge>
			</div>
			<p className="text-xs text-muted-foreground">
				{t('dialog-agent-create.member.hint')}
			</p>

			{/* AI 推荐：任务议题输入 + 推荐按钮 */}
			<div className="flex gap-2">
				<Input
					placeholder={t('dialog-agent-create.member.topicPlaceholder')}
					value={topic}
					onChange={(e) => setTopic(e.target.value)}
					className="h-8 text-sm"
				/>
				<Button
					type="button"
					variant="outline"
					size="sm"
					className="h-8 shrink-0"
					disabled={!canRecommend || recommending}
					onClick={runRecommend}
				>
					{recommending ? (
						<Loader2 className="size-3.5 animate-spin" />
					) : (
						<Sparkles className="size-3.5" />
					)}
					{t('dialog-agent-create.member.recommend')}
				</Button>
			</div>

			{recError && <p className="text-xs text-destructive">{recError}</p>}

			{recommendations && recommendations.length > 0 && (
				<div className="space-y-1.5 rounded-md border bg-muted/40 p-2">
					<p className="text-xs font-medium text-muted-foreground">
						{t('dialog-agent-create.member.recommendTitle')}
					</p>
					{recommendations.map((r) => (
						<button
							key={r.id}
							type="button"
							className="flex w-full items-start gap-2 rounded-md border bg-background p-2 text-left text-sm hover:bg-accent"
							onClick={() => toggle(r.id)}
						>
							<Check
								className={`mt-0.5 size-3.5 shrink-0 ${
									selected.includes(r.id) ? 'opacity-100' : 'opacity-0'
								}`}
							/>
							<span className="min-w-0 flex-1">
								<span className="font-medium">{r.name}</span>
								{r.reason && (
									<span className="block text-xs text-muted-foreground">
										<Bot className="mr-1 inline size-3" />
										{r.reason}
									</span>
								)}
							</span>
						</button>
					))}
				</div>
			)}

			{/* 全量 member 复选列表 */}
			<div className="max-h-40 space-y-1 overflow-y-auto rounded-md border p-1.5">
				{memberAgents.map((a) => (
					<button
						key={a.id}
						type="button"
						className="flex w-full items-center gap-2 rounded-sm px-1.5 py-1 text-left text-sm hover:bg-accent"
						onClick={() => toggle(a.id)}
					>
						<span
							className={`flex size-4 shrink-0 items-center justify-center rounded border ${
								selected.includes(a.id)
									? 'border-primary bg-primary text-primary-foreground'
									: 'border-muted-foreground/30'
							}`}
						>
							{selected.includes(a.id) && <Check className="size-3" />}
						</span>
						<span className="min-w-0 flex-1 truncate">
							{a.data.name}
							<span className="ml-1.5 text-xs text-muted-foreground">
								{t('chat.agent.memberBadge')}
							</span>
						</span>
					</button>
				))}
			</div>
		</div>
	);
}
