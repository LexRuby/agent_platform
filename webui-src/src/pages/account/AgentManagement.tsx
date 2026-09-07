import {
	Check,
	Copy,
	Ellipsis,
	Globe,
	History,
	Pencil,
	Plus,
	Rocket,
	Settings2,
	Share2,
	Trash2,
	UserPlus,
	Users,
	X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';

import { agentApi, agentShareApi, agentVersionApi } from '@/api';
import type { AgentView, PublicationInfo, ShareInfo } from '@/api';
import { getUserId } from '@/api/client';
import { AgentDialog } from '@/components/dialog/AgentDialog';
import { AgentVersionDialog } from '@/components/dialog/AgentVersionDialog';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { EditAgentDialog } from '@/components/dialog/EditAgentDialog';
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
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuSeparator,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '@/components/ui/table';
import { cn } from '@/lib/utils';

type ShareMode = 'private' | 'users' | 'public';

// ─── 徽标 ────────────────────────────────────────────────────────────────────

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

// ─── 可见性设置对话框（普通共享：发布当前活配置） ─────────────────────────────

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
					<DialogTitle>{t('share.dialog-title', { name: agent.data.name })}</DialogTitle>
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
							<div className="text-xs text-muted-foreground">{t('share.mode-private-desc')}</div>
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
							<div className="text-xs text-muted-foreground">{t('share.mode-users-desc')}</div>
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
							<div className="text-xs text-muted-foreground">{t('share.mode-public-desc')}</div>
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

// ─── 复制对话框：从当前配置或版本快照分叉新个体 ──────────────────────────────

function DuplicateDialog({
	open,
	onOpenChange,
	agent,
	onDone,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	agent: AgentView;
	onDone: () => void;
}) {
	const { t } = useTranslation();
	const [name, setName] = useState('');
	const [version, setVersion] = useState<string>('current');
	const [versions, setVersions] = useState<{ version: number; label: string }[]>([]);
	const [submitting, setSubmitting] = useState(false);

	useEffect(() => {
		if (!open) return;
		setName(`${agent.data.name} ${t('account.dup-suffix')}`);
		setVersion('current');
		agentVersionApi
			.list(agent.id)
			.then((s) =>
				setVersions(
					[...s.versions].reverse().map((v) => ({
						version: v.version,
						label: v.label,
					})),
				),
			)
			.catch(() => setVersions([]));
	}, [open, agent.id, agent.data.name, t]);

	const submit = async () => {
		setSubmitting(true);
		try {
			const ver = version === 'current' ? null : Number(version);
			const res = await agentVersionApi.duplicate(agent.id, name.trim(), ver);
			toast.success(t('account.dup-toast', { name: res.name }));
			onOpenChange(false);
			onDone();
		} finally {
			setSubmitting(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="sm:max-w-md">
				<DialogHeader>
					<DialogTitle className="flex items-center gap-2">
						<Copy className="size-4 text-primary" />
						{t('account.dup-title')}
					</DialogTitle>
					<DialogDescription>{t('account.dup-description')}</DialogDescription>
				</DialogHeader>

				<div className="flex flex-col gap-4">
					<div className="flex flex-col gap-1.5">
						<Label htmlFor="dup-name">{t('account.dup-name-label')}</Label>
						<Input
							id="dup-name"
							value={name}
							onChange={(e) => setName(e.target.value)}
							placeholder={agent.data.name}
						/>
					</div>
					<div className="flex flex-col gap-1.5">
						<Label>{t('account.dup-version-label')}</Label>
						<Select value={version} onValueChange={setVersion}>
							<SelectTrigger className="w-full">
								<SelectValue />
							</SelectTrigger>
							<SelectContent>
								<SelectItem value="current">
									{t('account.dup-current-config')}
								</SelectItem>
								{versions.map((v) => (
									<SelectItem key={v.version} value={String(v.version)}>
										{`v${v.version}${v.label ? ` · ${v.label}` : ''}`}
									</SelectItem>
								))}
							</SelectContent>
						</Select>
						<p className="text-xs text-muted-foreground">
							{t('account.dup-version-hint')}
						</p>
					</div>
				</div>

				<DialogFooter>
					<Button variant="outline" onClick={() => onOpenChange(false)}>
						{t('common.cancel')}
					</Button>
					<Button onClick={submit} disabled={submitting || !name.trim()}>
						{submitting ? t('common.saving') : t('account.dup-submit')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}

// ─── 版本发布对话框：版本快照 → 独立对外产品（重命名）+ 共享 ─────────────────

function PublishDialog({
	open,
	onOpenChange,
	agent,
	onDone,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	agent: AgentView;
	onDone: () => void;
}) {
	const { t } = useTranslation();
	const [versions, setVersions] = useState<{ version: number; label: string }[]>([]);
	const [version, setVersion] = useState<string>('');
	const [displayName, setDisplayName] = useState('');
	const [mode, setMode] = useState<'users' | 'public'>('users');
	const [users, setUsers] = useState<string[]>([]);
	const [input, setInput] = useState('');
	const [submitting, setSubmitting] = useState(false);

	useEffect(() => {
		if (!open) return;
		setUsers([]);
		setInput('');
		setMode('users');
		agentVersionApi
			.list(agent.id)
			.then((s) => {
				const vs = [...s.versions].reverse().map((v) => ({
					version: v.version,
					label: v.label,
				}));
				setVersions(vs);
				if (vs.length > 0) {
					const latest = String(vs[0].version);
					setVersion(latest);
					setDisplayName(`${agent.data.name} v${vs[0].version}`);
				} else {
					setVersion('');
					setDisplayName('');
				}
			})
			.catch(() => setVersions([]));
	}, [open, agent.id, agent.data.name]);

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

	const submit = async () => {
		setSubmitting(true);
		try {
			const res = await agentShareApi.publish(
				agent.id,
				Number(version),
				displayName.trim(),
				mode,
				mode === 'users' ? users : [],
			);
			toast.success(t('account.pub-toast', { name: res.display_name }));
			onOpenChange(false);
			onDone();
		} finally {
			setSubmitting(false);
		}
	};

	const canSubmit =
		!!version &&
		displayName.trim().length > 0 &&
		(mode === 'public' || users.length > 0);

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="sm:max-w-md">
				<DialogHeader>
					<DialogTitle className="flex items-center gap-2">
						<Rocket className="size-4 text-primary" />
						{t('account.pub-title')}
					</DialogTitle>
					<DialogDescription>{t('account.pub-description')}</DialogDescription>
				</DialogHeader>

				{versions.length === 0 ? (
					<div className="rounded-lg border border-dashed p-4 text-center text-sm text-muted-foreground">
						{t('account.pub-no-versions')}
					</div>
				) : (
					<div className="flex flex-col gap-4">
						<div className="flex flex-col gap-1.5">
							<Label>{t('account.pub-version-label')}</Label>
							<Select value={version} onValueChange={(v) => {
								setVersion(v);
								setDisplayName(`${agent.data.name} v${v}`);
							}}>
								<SelectTrigger className="w-full">
									<SelectValue />
								</SelectTrigger>
								<SelectContent>
									{versions.map((v) => (
										<SelectItem key={v.version} value={String(v.version)}>
											{`v${v.version}${v.label ? ` · ${v.label}` : ''}`}
										</SelectItem>
									))}
								</SelectContent>
							</Select>
						</div>
						<div className="flex flex-col gap-1.5">
							<Label htmlFor="pub-name">{t('account.pub-name-label')}</Label>
							<Input
								id="pub-name"
								value={displayName}
								onChange={(e) => setDisplayName(e.target.value)}
							/>
							<p className="text-xs text-muted-foreground">
								{t('account.pub-name-hint')}
							</p>
						</div>

						<RadioGroup value={mode} onValueChange={(v) => setMode(v as 'users' | 'public')} className="gap-3">
							<Label
								className={cn(
									'flex cursor-pointer items-start gap-3 rounded-lg border p-3',
									mode === 'users' && 'border-primary bg-primary/5',
								)}
							>
								<RadioGroupItem value="users" className="mt-0.5" />
								<div className="flex-1 space-y-1">
									<div className="text-sm font-medium">{t('share.mode-users-label')}</div>
									<div className="text-xs text-muted-foreground">{t('share.mode-users-desc')}</div>
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
									<div className="text-xs text-muted-foreground">{t('share.mode-public-desc')}</div>
								</div>
							</Label>
						</RadioGroup>
					</div>
				)}

				<DialogFooter>
					<Button variant="outline" onClick={() => onOpenChange(false)}>
						{t('common.cancel')}
					</Button>
					<Button onClick={submit} disabled={submitting || !canSubmit}>
						{submitting ? t('common.saving') : t('account.pub-submit')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}

// ─── 主组件：智能体管理（增删改查 + 版本 + 复制 + 发布） ─────────────────────

/**
 * 智能体管理（账户中心 Tab，2026-09-08 v2）。
 *
 * 不止"发布管理"——这是大小A 的完整增删改查入口：
 * - 新建（AgentDialog）/ 删除（DeleteDialog）
 * - 编辑（EditAgentDialog）/ 版本管理（AgentVersionDialog：历史版本/
 *   发布新版本/冻结/切换）
 * - 复制（DuplicateDialog：从当前配置或任意版本快照分叉）
 * - 发布版本（PublishDialog：版本快照 → 独立对外产品，可重命名——
 *   同一智能体的 v2/v3 可分别发布给不同账号）
 * - 可见性（ShareSettingDialog：私有/指定账号/全部可见）
 * - 我的发布物（独立产品列表 + 溯源）
 * - 共享给我（只读使用）
 */
export function AgentManagement() {
	const { t } = useTranslation();
	const [agents, setAgents] = useState<AgentView[]>([]);
	const [shares, setShares] = useState<ShareInfo[]>([]);
	const [publications, setPublications] = useState<PublicationInfo[]>([]);
	const [loading, setLoading] = useState(true);

	// 对话框目标
	const [editing, setEditing] = useState<AgentView | null>(null);
	const [versioning, setVersioning] = useState<AgentView | null>(null);
	const [duplicating, setDuplicating] = useState<AgentView | null>(null);
	const [publishing, setPublishing] = useState<AgentView | null>(null);
	const [settingShare, setSettingShare] = useState<AgentView | null>(null);
	const [deleting, setDeleting] = useState<AgentView | null>(null);

	const load = useCallback(async () => {
		setLoading(true);
		try {
			const [agentRes, shareRes, pubRes] = await Promise.all([
				agentApi.list(),
				agentShareApi.mine(),
				agentShareApi.publications().catch(() => ({ publications: [] as PublicationInfo[] })),
			]);
			setAgents(agentRes.agents);
			setShares(shareRes.shares);
			setPublications(pubRes.publications);
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
	const publishedIds = useMemo(
		() => new Set(publications.map((p) => p.agent_id)),
		[publications],
	);
	const myAgents = useMemo(
		() => agents.filter((a) => a.editable),
		[agents],
	);
	const sharedToMe = useMemo(
		() => agents.filter((a) => !a.editable && a.user_id !== me),
		[agents, me],
	);

	const handleDelete = async () => {
		if (!deleting) return;
		const target = deleting;
		// 发布物：先下架（清共享 + 溯源）再删本体
		if (publishedIds.has(target.id)) {
			await agentShareApi.remove(target.id).catch(() => undefined);
		}
		await agentApi.delete(target.id);
		setDeleting(null);
		await load();
	};

	if (loading) {
		return (
			<div className="flex flex-col gap-4">
				<Skeleton className="h-10 w-64" />
				<Skeleton className="h-48 rounded-xl" />
				<Skeleton className="h-32 rounded-xl" />
			</div>
		);
	}

	return (
		<div className="flex flex-col gap-6">
			{/* 我的智能体：完整增删改查 */}
			<Card className="gap-3">
				<CardHeader className="flex-row items-center justify-between space-y-0">
					<div className="space-y-0.5">
						<CardTitle className="flex items-center gap-2 text-base">
							<Share2 className="size-4 text-primary" />
							{t('share.my-agents')}
						</CardTitle>
						<CardDescription>{t('account.manage-description')}</CardDescription>
					</div>
					<AgentDialog onCreated={() => void load()}>
						<Button size="sm">
							<Plus className="size-3.5" />
							{t('account.new-agent')}
						</Button>
					</AgentDialog>
				</CardHeader>
				<CardContent>
					{myAgents.length === 0 ? (
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
									<TableHead>{t('account.col-version')}</TableHead>
									<TableHead>{t('share.visibility')}</TableHead>
									<TableHead className="text-right">{t('account.col-actions')}</TableHead>
								</TableRow>
							</TableHeader>
							<TableBody>
								{myAgents.map((a) => {
									const s = shareByAgent.get(a.id) ?? null;
									const mode = (s?.mode as ShareMode) ?? 'private';
									const isPub = publishedIds.has(a.id);
									return (
										<TableRow key={a.id}>
											<TableCell className="max-w-[240px]">
												<div className="flex items-center gap-2">
													<span className="truncate font-medium">{a.data.name}</span>
													{isPub && (
														<Badge className="shrink-0 gap-1 bg-violet-50 text-violet-700 hover:bg-violet-50">
															<Rocket className="size-3" />
															{t('account.pub-badge')}
														</Badge>
													)}
												</div>
											</TableCell>
											<TableCell>
												<TypeBadge agent={a} />
											</TableCell>
											<TableCell>
												{a.version?.current_version != null ? (
													<Badge variant="outline" className="font-mono text-xs">
														v{a.version.current_version}
													</Badge>
												) : (
													<span className="text-xs text-muted-foreground">—</span>
												)}
											</TableCell>
											<TableCell>
												<ModeBadge mode={mode} users={s?.users ?? []} />
											</TableCell>
											<TableCell className="text-right">
												<DropdownMenu>
													<DropdownMenuTrigger asChild>
														<Button variant="ghost" size="icon-sm">
															<Ellipsis />
														</Button>
													</DropdownMenuTrigger>
													<DropdownMenuContent align="end" className="w-auto">
														<DropdownMenuItem onClick={() => setEditing(a)}>
															<Pencil />
															{t('account.act-edit')}
														</DropdownMenuItem>
														<DropdownMenuItem onClick={() => setVersioning(a)}>
															<History />
															{t('account.act-versions')}
														</DropdownMenuItem>
														<DropdownMenuItem onClick={() => setDuplicating(a)}>
															<Copy />
															{t('account.act-duplicate')}
														</DropdownMenuItem>
														<DropdownMenuItem onClick={() => setPublishing(a)}>
															<Rocket />
															{t('account.act-publish')}
														</DropdownMenuItem>
														<DropdownMenuItem onClick={() => setSettingShare(a)}>
															<Share2 />
															{mode === 'private'
																? t('share.publish')
																: t('share.edit-visibility')}
														</DropdownMenuItem>
														<DropdownMenuSeparator />
														<DropdownMenuItem
															variant="destructive"
															onClick={() => setDeleting(a)}
														>
															<Trash2 />
															{t('account.act-delete')}
														</DropdownMenuItem>
													</DropdownMenuContent>
												</DropdownMenu>
											</TableCell>
										</TableRow>
									);
								})}
							</TableBody>
						</Table>
					)}
				</CardContent>
			</Card>

			{/* 我的发布物：版本快照发布的独立对外产品 */}
			<Card className="gap-3">
				<CardHeader>
					<CardTitle className="flex items-center gap-2 text-base">
						<Rocket className="size-4 text-violet-600" />
						{t('account.pubs-title')}
						{publications.length > 0 && (
							<Badge variant="secondary">{publications.length}</Badge>
						)}
					</CardTitle>
					<CardDescription>{t('account.pubs-description')}</CardDescription>
				</CardHeader>
				<CardContent>
					{publications.length === 0 ? (
						<Empty className="border-none">
							<EmptyHeader>
								<EmptyTitle>{t('account.pubs-empty-title')}</EmptyTitle>
								<EmptyDescription>{t('account.pubs-empty-description')}</EmptyDescription>
							</EmptyHeader>
						</Empty>
					) : (
						<div className="flex flex-col gap-2">
							{publications.map((p) => (
								<div
									key={p.agent_id}
									className="flex items-center gap-3 rounded-lg border px-3 py-2"
								>
									<Rocket className="size-4 shrink-0 text-violet-600" />
									<div className="min-w-0 flex-1">
										<div className="flex items-center gap-2">
											<span className="truncate text-sm font-medium">
												{p.display_name}
											</span>
											<ModeBadge mode={p.mode as ShareMode} users={p.users} />
										</div>
										<div className="text-xs text-muted-foreground">
											{t('account.pub-source', {
												name: p.source_agent_name || p.source_agent_id.slice(0, 8),
												version: p.source_version,
											})}
										</div>
									</div>
									<Button
										size="sm"
										variant="ghost"
										title={t('share.edit-visibility')}
										onClick={() => {
											const target = agents.find((x) => x.id === p.agent_id);
											if (target) setSettingShare(target);
										}}
									>
										<Settings2 className="size-3.5" />
									</Button>
								</div>
							))}
						</div>
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

			{/* 对话框组 */}
			{editing && (
				<EditAgentDialog
					open
					onOpenChange={(open) => !open && setEditing(null)}
					agent={editing}
					onUpdated={() => void load()}
				/>
			)}
			{versioning && (
				<AgentVersionDialog
					open
					onOpenChange={(open) => !open && setVersioning(null)}
					agent={versioning}
					onUpdated={() => void load()}
				/>
			)}
			{duplicating && (
				<DuplicateDialog
					open
					onOpenChange={(open) => !open && setDuplicating(null)}
					agent={duplicating}
					onDone={() => void load()}
				/>
			)}
			{publishing && (
				<PublishDialog
					open
					onOpenChange={(open) => !open && setPublishing(null)}
					agent={publishing}
					onDone={() => void load()}
				/>
			)}
			{settingShare && (
				<ShareSettingDialog
					open
					onOpenChange={(open) => !open && setSettingShare(null)}
					agent={settingShare}
					share={shareByAgent.get(settingShare.id) ?? null}
					onSaved={() => void load()}
				/>
			)}
			{deleting && (
				<DeleteDialog
					open
					onOpenChange={(open) => !open && setDeleting(null)}
					title={t('common.deleteTitle', {
						entity: t('dialog-agent-delete.entity'),
						name: deleting.data.name,
					})}
					description={t('common.deleteDescription')}
					confirmLabel={t('dialog-agent-delete.confirm')}
					onConfirm={handleDelete}
				/>
			)}
		</div>
	);
}
