import { Check, Globe, Share2, UserPlus, Users, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';

import { agentShareApi, agentApi } from '@/api';
import type { AgentView, ShareInfo } from '@/api';
import { getUserId } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '@/components/ui/table';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

type ShareMode = 'private' | 'users' | 'public';

/** 可见性徽标。 */
function ModeBadge({ mode, users }: { mode: ShareMode; users: string[] }) {
	const { t } = useTranslation();
	if (mode === 'public') {
		return (
			<Badge className="gap-1 bg-emerald-50 text-emerald-700 hover:bg-emerald-50">
				<Globe className="size-3" />
				{t('share.mode-public')}
			</Badge>
		);
	}
	if (mode === 'users') {
		return (
			<Badge className="gap-1 bg-blue-50 text-blue-700 hover:bg-blue-50">
				<Users className="size-3" />
				{t('share.mode-users', { count: users.length })}
			</Badge>
		);
	}
	return (
		<Badge variant="outline" className="text-muted-foreground">
			{t('share.mode-private')}
		</Badge>
	);
}

/** 智能体类型徽标（大A/小A）。 */
function TypeBadge({ agent }: { agent: AgentView }) {
	const { t } = useTranslation();
	return agent.agent_type === 'leader' ? (
		<Badge variant="default" className="text-[10px]">
			{t('chat.agent.leaderBadge')}
		</Badge>
	) : (
		<Badge variant="outline" className="text-[10px] text-muted-foreground">
			{t('chat.agent.memberBadge')}
		</Badge>
	);
}

/** 发布设置对话框：可见范围单选 + 账号列表编辑。 */
function ShareSettingDialog({
	open,
	onOpenChange,
	agent,
	share,
	onSaved,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	agent: AgentView;
	share: ShareInfo | null;
	onSaved: () => void;
}) {
	const { t } = useTranslation();
	const [mode, setMode] = useState<ShareMode>('private');
	const [users, setUsers] = useState<string[]>([]);
	const [input, setInput] = useState('');
	const [saving, setSaving] = useState(false);

	useEffect(() => {
		if (open) {
			setMode((share?.mode as ShareMode) ?? 'private');
			setUsers(share?.users ?? []);
			setInput('');
		}
	}, [open, share]);

	const addUser = () => {
		const name = input.trim();
		if (!name) return;
		if (users.includes(name)) {
			toast.warning(t('share.user-duplicated'));
			return;
		}
		if (!/^[a-zA-Z0-9_-]{2,32}$/.test(name)) {
			toast.error(t('share.user-invalid'));
			return;
		}
		setUsers([...users, name]);
		setInput('');
	};

	const save = async () => {
		setSaving(true);
		try {
			await agentShareApi.set(agent.id, mode, mode === 'users' ? users : []);
			toast.success(
				mode === 'private' ? t('share.toast-unpublished') : t('share.toast-saved'),
			);
			onOpenChange(false);
			onSaved();
		} finally {
			setSaving(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="sm:max-w-md">
				<DialogHeader>
					<DialogTitle>
						{t('share.dialog-title', { name: agent.data.name })}
					</DialogTitle>
					<DialogDescription>{t('share.dialog-description')}</DialogDescription>
				</DialogHeader>

				<RadioGroup value={mode} onValueChange={(v) => setMode(v as ShareMode)} className="gap-3">
					<Label
						className={cn(
							'flex cursor-pointer items-start gap-3 rounded-lg border p-3',
							mode === 'private' && 'border-primary bg-primary/5',
						)}
					>
						<RadioGroupItem value="private" className="mt-0.5" />
						<div className="space-y-1">
							<div className="text-sm font-medium">{t('share.mode-private')}</div>
							<div className="text-xs text-muted-foreground">
								{t('share.mode-private-desc')}
							</div>
						</div>
					</Label>
					<Label
						className={cn(
							'flex cursor-pointer items-start gap-3 rounded-lg border p-3',
							mode === 'users' && 'border-primary bg-primary/5',
						)}
					>
						<RadioGroupItem value="users" className="mt-0.5" />
						<div className="flex-1 space-y-1">
							<div className="text-sm font-medium">{t('share.mode-users-label')}</div>
							<div className="text-xs text-muted-foreground">
								{t('share.mode-users-desc')}
							</div>
							{mode === 'users' && (
								<div className="pt-2">
									<div className="flex gap-2">
										<Input
											value={input}
											onChange={(e) => setInput(e.target.value)}
											onKeyDown={(e) => {
												if (e.key === 'Enter') {
													e.preventDefault();
													addUser();
												}
											}}
											placeholder={t('share.user-input-placeholder')}
											className="h-8"
										/>
										<Button size="sm" variant="outline" className="h-8" onClick={addUser}>
											<UserPlus className="size-3.5" />
										</Button>
									</div>
									{users.length > 0 && (
										<div className="mt-2 flex flex-wrap gap-1.5">
											{users.map((u) => (
												<span
													key={u}
													className="inline-flex items-center gap-1 rounded-md bg-blue-50 px-2 py-0.5 text-xs text-blue-700"
												>
													{u}
													<button
														type="button"
														className="text-blue-400 hover:text-blue-700"
														onClick={() => setUsers(users.filter((x) => x !== u))}
													>
														<X className="size-3" />
													</button>
												</span>
											))}
										</div>
									)}
								</div>
							)}
						</div>
					</Label>
					<Label
						className={cn(
							'flex cursor-pointer items-start gap-3 rounded-lg border p-3',
							mode === 'public' && 'border-primary bg-primary/5',
						)}
					>
						<RadioGroupItem value="public" className="mt-0.5" />
						<div className="space-y-1">
							<div className="text-sm font-medium">{t('share.mode-public-label')}</div>
							<div className="text-xs text-muted-foreground">
								{t('share.mode-public-desc')}
							</div>
						</div>
					</Label>
				</RadioGroup>

				<DialogFooter>
					<Button variant="outline" onClick={() => onOpenChange(false)}>
						{t('common.cancel')}
					</Button>
					<Button onClick={save} disabled={saving}>
						{saving ? t('common.saving') : t('common.save')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}

/**
 * 共享管理页（2026-09-07 共享 v1）。
 *
 * 回答用户问题"我能选择我发布之后，这些大A/小A 是什么账号能看到的"：
 * - 我的智能体：逐个设置可见性（私有/指定账号/公开）
 * - 共享给我：他人发布的、我可使用的（只读，可直接开对话）
 *
 * 被共享账号在聊天页的智能体选择器自动出现（官方链路 + 只读保护），
 * 团队维度：共享的小A 可被邀请进他人团队（官方 invite 机制）。
 */
export function SharePage() {
	const { t } = useTranslation();
	const [agents, setAgents] = useState<AgentView[]>([]);
	const [shares, setShares] = useState<ShareInfo[]>([]);
	const [loading, setLoading] = useState(true);
	const [settingAgent, setSettingAgent] = useState<AgentView | null>(null);

	const load = useCallback(async () => {
		setLoading(true);
		try {
			const [agentRes, shareRes] = await Promise.all([
				agentApi.list(),
				agentShareApi.mine(),
			]);
			setAgents(agentRes.agents);
			setShares(shareRes.shares);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void load();
	}, [load]);

	const me = getUserId();
	const shareByAgent = useMemo(
		() => new Map(shares.map((s) => [s.agent_id, s])),
		[shares],
	);
	// 共享给我 = 官方列表合并的跨账号条目（editable=false 且 owner≠我）
	const sharedToMe = useMemo(
		() => agents.filter((a) => !a.editable && a.user_id !== me),
		[agents, me],
	);
	const published = useMemo(
		() => shares.filter((s) => s.mode !== 'private'),
		[shares],
	);

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
					<div>
						<div className="text-2xl font-semibold">{t('share.title')}</div>
						<div className="mt-1 text-sm text-muted-foreground">
							{t('share.subtitle')}
						</div>
					</div>
				</div>
				<Separator />

				<div className="flex-1 overflow-y-auto px-6 py-5">
					{loading ? (
						<div className="flex flex-col gap-4">
							<Skeleton className="h-10 w-64" />
							<Skeleton className="h-48 rounded-xl" />
							<Skeleton className="h-32 rounded-xl" />
						</div>
					) : (
						<div className="flex flex-col gap-6">
							{/* 我的智能体：可见性管理 */}
							<Card className="gap-3">
								<CardHeader>
									<CardTitle className="flex items-center gap-2 text-base">
										<Share2 className="size-4 text-primary" />
										{t('share.my-agents')}
										{published.length > 0 && (
											<Badge variant="secondary">
												{t('share.published-count', { count: published.length })}
											</Badge>
										)}
									</CardTitle>
									<CardDescription>{t('share.my-agents-description')}</CardDescription>
								</CardHeader>
								<CardContent>
									{agents.filter((a) => a.editable).length === 0 ? (
										<Empty className="border-none">
											<EmptyHeader>
												<EmptyTitle>{t('share.no-agents-title')}</EmptyTitle>
												<EmptyDescription>{t('share.no-agents-description')}</EmptyDescription>
											</EmptyHeader>
										</Empty>
									) : (
										<Table>
											<TableHeader>
												<TableRow>
													<TableHead>{t('common.name')}</TableHead>
													<TableHead>{t('share.type')}</TableHead>
													<TableHead>{t('share.visibility')}</TableHead>
													<TableHead>{t('share.visible-users')}</TableHead>
													<TableHead className="text-right">{t('share.actions')}</TableHead>
												</TableRow>
											</TableHeader>
											<TableBody>
												{agents
													.filter((a) => a.editable)
													.map((a) => {
														const s = shareByAgent.get(a.id) ?? null;
														const mode = (s?.mode as ShareMode) ?? 'private';
														return (
															<TableRow key={a.id}>
																<TableCell className="max-w-[220px] truncate font-medium">
																	{a.data.name}
																</TableCell>
																<TableCell>
																	<TypeBadge agent={a} />
																</TableCell>
																<TableCell>
																	<ModeBadge mode={mode} users={s?.users ?? []} />
																</TableCell>
																<TableCell className="max-w-[260px]">
																	{mode === 'users' && s && s.users.length > 0 ? (
																		<span
																			className="truncate font-mono text-xs text-muted-foreground"
																			title={s.users.join('、')}
																		>
																			{s.users.join('、')}
																		</span>
																	) : (
																		<span className="text-xs text-muted-foreground">
																			{mode === 'public'
																				? t('share.all-accounts')
																				: '—'}
																		</span>
																	)}
																</TableCell>
																<TableCell className="text-right">
																	<Button
																		size="sm"
																		variant="outline"
																		onClick={() => setSettingAgent(a)}
																	>
																		{mode === 'private'
																			? t('share.publish')
																			: t('share.edit-visibility')}
																	</Button>
																</TableCell>
															</TableRow>
														);
													})}
											</TableBody>
										</Table>
									)}
								</CardContent>
							</Card>

							{/* 共享给我：他人发布、我可使用（只读） */}
							<Card className="gap-3">
								<CardHeader>
									<CardTitle className="flex items-center gap-2 text-base">
										<Users className="size-4 text-primary" />
										{t('share.shared-to-me')}
										{sharedToMe.length > 0 && (
											<Badge variant="secondary">{sharedToMe.length}</Badge>
										)}
									</CardTitle>
									<CardDescription>{t('share.shared-to-me-description')}</CardDescription>
								</CardHeader>
								<CardContent>
									{sharedToMe.length === 0 ? (
										<Empty className="border-none">
											<EmptyHeader>
												<EmptyTitle>{t('share.no-shared-title')}</EmptyTitle>
												<EmptyDescription>{t('share.no-shared-description')}</EmptyDescription>
											</EmptyHeader>
										</Empty>
									) : (
										<div className="flex flex-col gap-2">
											{sharedToMe.map((a) => (
												<div
													key={a.id}
													className="flex items-center gap-3 rounded-lg border px-3 py-2"
												>
													<Check className="size-4 shrink-0 text-emerald-600" />
													<div className="min-w-0 flex-1">
														<div className="flex items-center gap-2">
															<span className="truncate text-sm font-medium">
																{a.data.name}
															</span>
															<TypeBadge agent={a} />
														</div>
														<div className="text-xs text-muted-foreground">
															{t('share.owner-label', { owner: a.user_id })}
														</div>
													</div>
													<Badge variant="outline" className="text-muted-foreground">
														{t('common.readOnly')}
													</Badge>
												</div>
											))}
										</div>
									)}
								</CardContent>
							</Card>
						</div>
					)}
				</div>
			</main>

			{settingAgent && (
				<ShareSettingDialog
					open
					onOpenChange={(open) => !open && setSettingAgent(null)}
					agent={settingAgent}
					share={shareByAgent.get(settingAgent.id) ?? null}
					onSaved={() => void load()}
				/>
			)}
		</div>
	);
}
