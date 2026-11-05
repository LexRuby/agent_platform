import { Bot, Check, ChevronDown, Crown, Lock } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import type { AgentView } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuGroup,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuSeparator,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useTranslation } from '@/i18n/useI18n.ts';
import { cn } from '@/lib/utils';

interface Props extends Omit<React.ComponentPropsWithoutRef<typeof Button>, 'onChange' | 'value'> {
	agents: AgentView[];
	/** Currently selected agent id, or `null` when none is selected. */
	value?: string | null;
	onChange?: (agentId: string) => void;
	/** Override the trigger label shown when no agent is selected. */
	placeholder?: string;
	className?: string;
}

function readRecentAgentIds(): string[] {
	try {
		const value = JSON.parse(localStorage.getItem('agentforge-agent-recent') || '[]');
		return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : [];
	} catch {
		return [];
	}
}

/**
 * Agent picker used in the chat sidebar. Rendered on top of the
 * shadcn dropdown-menu primitives (same base as `LlmSelect`) so
 * grouping and per-row content (icons, badges) is fully composable.
 *
 * Grouping rule: when every visible agent is editable (i.e. no
 * cross-owner shares in play), the list is rendered flat. As soon
 * as at least one entry is `editable === false`, the list splits
 * into "Yours" and "Shared with you" groups so the read-only
 * origin of the shared entries is unambiguous at a glance.
 */
export function AgentSelect({ agents, value, onChange, placeholder, className, ...props }: Props) {
	const { t } = useTranslation();
	const selected = agents.find((a) => a.id === value) ?? null;
	const selectedId = selected?.id;
	const selectedType: 'leader' | 'member' = selected?.agent_type === 'leader' ? 'leader' : 'member';
	const [activeType, setActiveType] = useState<'leader' | 'member'>(
		selectedType,
	);
	const [recentAgentIds, setRecentAgentIds] = useState<string[]>(readRecentAgentIds);
	useEffect(() => {
		if (selectedId) setActiveType(selectedType);
	}, [selectedId, selectedType]);
	const sortedAgents = useMemo(() => {
		const rank = new Map(recentAgentIds.map((id, index) => [id, index]));
		return [...agents].sort((a, b) => {
			const recentA = rank.get(a.id);
			const recentB = rank.get(b.id);
			if (recentA !== undefined || recentB !== undefined) {
				return (recentA ?? Number.MAX_SAFE_INTEGER) - (recentB ?? Number.MAX_SAFE_INTEGER);
			}
			return String(b.updated_at || b.created_at).localeCompare(String(a.updated_at || a.created_at));
		});
	}, [agents, recentAgentIds]);
	const displayLabel = selected
		? selected.data.name
		: (placeholder ?? t('chat.agent.selectPlaceholder'));

	const leaders = sortedAgents.filter((a) => a.agent_type === 'leader');
	const members = sortedAgents.filter((a) => a.agent_type !== 'leader');
	const visibleAgents = activeType === 'leader' ? leaders : members;
	const visibleYours = visibleAgents.filter((a) => a.editable);
	const visibleShared = visibleAgents.filter((a) => !a.editable);
	const markRecent = (agent: AgentView) => {
		const next = [agent.id, ...recentAgentIds.filter((id) => id !== agent.id)].slice(0, 100);
		setRecentAgentIds(next);
		try {
			localStorage.setItem('agentforge-agent-recent', JSON.stringify(next));
		} catch {
			// Selection must still work when browser storage is unavailable.
		}
		onChange?.(agent.id);
	};

	const typeBadge = (agent: AgentView) =>
		agent.agent_type === 'leader' ? (
			<Badge
				variant="default"
				className="text-[10px] px-1 py-0 shrink-0"
				title={t('chat.agent.leaderTooltip')}
			>
				{t('chat.agent.leaderBadge')}
			</Badge>
		) : (
			<Badge
				variant="outline"
				className="text-[10px] px-1 py-0 shrink-0 text-muted-foreground"
				title={t('chat.agent.memberTooltip')}
			>
				{t('chat.agent.memberBadge')}
			</Badge>
		);

	const renderItem = (agent: AgentView) => {
		const isSelected = agent.id === value;
		return (
			<DropdownMenuItem key={agent.id} className="items-start py-2" onSelect={() => markRecent(agent)}>
				<Check
					className={`size-3.5 shrink-0 ${isSelected ? 'opacity-100' : 'opacity-0'}`}
				/>
				<span className="min-w-0 flex-1 whitespace-normal break-words leading-5" title={agent.data.name}>{agent.data.name}</span>
				{typeBadge(agent)}
				{agent.version?.frozen && (
					<Badge
						variant="secondary"
						className="shrink-0 gap-0.5 px-1 py-0 text-[10px]"
						title={t('chat.agent.frozenTooltip', {
							version: agent.version.current_version ?? '-',
						})}
					>
						<Lock className="size-2.5" />
						v{agent.version.current_version ?? '-'}
					</Badge>
				)}
				{!agent.editable && (
					<Badge
						variant="secondary"
						className="text-[10px] px-1 py-0"
						title={t('common.readOnlyTooltip')}
					>
						{t('common.readOnly')}
					</Badge>
				)}
			</DropdownMenuItem>
		);
	};

	return (
		<DropdownMenu>
			<DropdownMenuTrigger asChild>
				<Button
					variant="outline"
					size="sm"
					className={cn('justify-between gap-1 font-normal', className)}
					{...props}
				>
					<span className="min-w-0 whitespace-normal break-words text-left leading-5">{displayLabel}</span>
					<ChevronDown className="size-3.5 text-muted-foreground" />
				</Button>
			</DropdownMenuTrigger>
			<DropdownMenuContent align="start" className="w-[min(30rem,calc(100vw-1rem))] max-h-[min(32rem,calc(100vh-5rem))] overflow-y-auto p-1">
				{agents.length === 0 ? (
					<div className="px-2 py-3 text-center text-sm text-muted-foreground">
						<p className="font-medium">{t('chat.agent.emptyTitle')}</p>
						<p className="text-xs mt-1">{t('chat.agent.emptyDescription')}</p>
					</div>
				) : (
					<>
						<div className="grid grid-cols-2 gap-1 p-1" role="tablist" aria-label={t('chat.agent.typeLabel')}>
							<button type="button" role="tab" aria-selected={activeType === 'leader'} onClick={() => setActiveType('leader')} className={cn('flex items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium', activeType === 'leader' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-accent/60')}>
								<Crown className="size-4" />{t('chat.agent.groupLeader')}<span className="text-xs opacity-60">{leaders.length}</span>
							</button>
							<button type="button" role="tab" aria-selected={activeType === 'member'} onClick={() => setActiveType('member')} className={cn('flex items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium', activeType === 'member' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-accent/60')}>
								<Bot className="size-4" />{t('chat.agent.groupMember')}<span className="text-xs opacity-60">{members.length}</span>
							</button>
						</div>
						<DropdownMenuSeparator />
						{visibleAgents.length === 0 ? (
							<div className="px-2 py-6 text-center text-sm text-muted-foreground">{t('chat.agent.emptyType')}</div>
						) : (
							<>
								{visibleYours.length > 0 && <DropdownMenuGroup>
									<DropdownMenuLabel>{t('chat.agent.groupYours')}</DropdownMenuLabel>
									{visibleYours.map(renderItem)}
								</DropdownMenuGroup>}
								{visibleYours.length > 0 && visibleShared.length > 0 && <DropdownMenuSeparator />}
								{visibleShared.length > 0 && <DropdownMenuGroup>
									<DropdownMenuLabel>{t('chat.agent.groupShared')}</DropdownMenuLabel>
									{visibleShared.map(renderItem)}
								</DropdownMenuGroup>}
							</>
						)}
					</>
				)}
			</DropdownMenuContent>
		</DropdownMenu>
	);
}
