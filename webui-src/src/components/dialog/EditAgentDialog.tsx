import { CircleAlert, History, Loader2, Lock, LockOpen, RotateCcw, Save } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { AgentView, ContextConfig, InviteConfig, ReActConfig } from '@/api';
import { agentVersionApi, type AgentVersionStatus } from '@/api/agentVersion';
import { MemberPicker } from '@/components/form/MemberPicker';
import {
	AgentFormFields,
	defaultAgentFormValues,
	type AgentFormValues,
	type AgentSection,
} from '@/components/form/AgentFormFields';
import type { SchemaFormValue } from '@/components/form/SchemaForm';
import { Alert, AlertDescription } from '@/components/ui/alert.tsx';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogFooter,
	DialogHeader,
	DialogTitle,
	DialogDescription,
} from '@/components/ui/dialog';
import { Separator } from '@/components/ui/separator';
import { useAgents } from '@/hooks/useAgents';
import { useAgentSchema } from '@/hooks/useAgentSchema';
import { formatApiErrorForAlert } from '@/lib/api-error';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	agent: AgentView;
	onUpdated?: () => void;
}

export function EditAgentDialog({ open, onOpenChange, agent, onUpdated }: Props) {
	const { update, agents, refetch } = useAgents();
	const { t } = useTranslation();
	const { schema } = useAgentSchema();
	const [submitting, setSubmitting] = useState(false);
	const [values, setValues] = useState<AgentFormValues | null>(null);
	const [errorMsg, setErrorMsg] = useState('');
	const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
	// 版本封板状态（打开对话框时从后端拉取）
	const [versionStatus, setVersionStatus] = useState<AgentVersionStatus | null>(null);
	const [versionBusy, setVersionBusy] = useState(false);
	// 表单已为哪个 agent 初始化过：版本操作后 refetch 会换 agent 对象引用，
	// 不加守卫会把用户未保存的编辑冲掉
	const initedFor = useRef<string | null>(null);

	useEffect(() => {
		if (!open || !schema) {
			if (!open) {
				setValues(null);
				setErrorMsg('');
				setSelectedMembers([]);
				setVersionStatus(null);
				initedFor.current = null;
			}
			return;
		}
		if (initedFor.current === agent.id && values) return;
		initedFor.current = agent.id;
		// Start from schema defaults, then overlay the existing agent's data so
		// any unset fields fall back to defaults rather than empty.
		const base = defaultAgentFormValues(schema);
		const d = agent.data;
		setSelectedMembers(agent.team_members ?? []);
		setValues({
			identity: {
				...base.identity,
				name: d.name,
				system_prompt: d.system_prompt,
				agent_type: agent.agent_type ?? 'member',
			},
			context_config: { ...base.context_config, ...(d.context_config ?? {}) },
			react_config: { ...base.react_config, ...(d.react_config ?? {}) },
			invite_config: { ...base.invite_config, ...(d.invite_config ?? {}) },
		});
		setErrorMsg('');
	}, [open, schema, agent, values]);

	// 打开对话框时拉取版本列表（列表接口含 frozen/versions，权威）
	useEffect(() => {
		if (!open) return;
		let cancelled = false;
		agentVersionApi
			.list(agent.id)
			.then((s) => {
				if (!cancelled) setVersionStatus(s);
			})
			.catch(() => {
				// 拉取失败降级为列表注入的粗粒度状态（无版本明细）
				if (!cancelled && agent.version) {
					setVersionStatus({
						agent_id: agent.id,
						frozen: agent.version.frozen,
						current_version: agent.version.current_version,
						latest_version: agent.version.latest_version,
						versions: [],
					});
				}
			});
		return () => {
			cancelled = true;
		};
	}, [open, agent.id]);

	const handleChange = (section: AgentSection, key: string, value: SchemaFormValue) => {
		setErrorMsg('');
		setValues((prev) =>
			prev ? { ...prev, [section]: { ...prev[section], [key]: value } } : prev,
		);
	};

	const handleSubmit = async () => {
		if (!values) return;
		const name = (values.identity.name as string | undefined)?.trim();
		if (!name) return;
		setErrorMsg('');
		setSubmitting(true);
		try {
			const isLeader = values.identity.agent_type === 'leader';
			// 成员档案用真实名字/职责（后端注入主理人提示词用），
			// 回退 id.slice(0,8) 会让提示词只剩裸 id（已修的 bug）
			const detailOf = (id: string) => {
				const a = agents.find((x) => x.id === id);
				return a
					? {
						id,
						name: a.data.name,
						description:
							a.data.invite_config?.invite_description ?? '',
					}
					: { id, name: id.slice(0, 8), description: '' };
			};
			await update(
				agent.id,
				{
					name,
					system_prompt: values.identity.system_prompt as string | undefined,
					agent_type: values.identity.agent_type as 'leader' | 'member' | undefined,
					team_members: isLeader
						? selectedMembers.map(detailOf)
						: undefined,
					context_config: values.context_config as unknown as ContextConfig,
					react_config: values.react_config as unknown as ReActConfig,
					invite_config: values.invite_config as unknown as InviteConfig,
				},
				{ silent: true },
			);
			onOpenChange(false);
			onUpdated?.();
		} catch (e) {
			setErrorMsg(formatApiErrorForAlert(e));
		} finally {
			setSubmitting(false);
		}
	};

	// ── 版本封板操作 ────────────────────────────────────────────────────
	const runVersionOp = async (op: () => Promise<AgentVersionStatus>) => {
		setVersionBusy(true);
		setErrorMsg('');
		try {
			const status = await op();
			setVersionStatus(status);
			// 刷新列表让选择器/侧栏的冻结徽章同步（表单有守卫不会被冲掉）
			await refetch();
		} catch (e) {
			setErrorMsg(formatApiErrorForAlert(e));
		} finally {
			setVersionBusy(false);
		}
	};

	const handleRestore = async (version: number) => {
		await runVersionOp(() => agentVersionApi.restore(agent.id, version));
		// 恢复改写了配置：关闭对话框，让父级以新数据重新打开
		onOpenChange(false);
		onUpdated?.();
	};

	const frozen = versionStatus?.frozen ?? agent.version?.frozen ?? false;
	const currentVersion = versionStatus?.current_version ?? agent.version?.current_version ?? null;

	const nameValid = !!(values?.identity.name as string | undefined)?.trim();

	const formatVersionDate = (iso: string) => {
		try {
			return new Date(iso).toLocaleString();
		} catch {
			return iso;
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="!w-[500px] !max-w-[500px]">
				<DialogHeader>
					<DialogTitle>{t('dialog-agent-edit.title')}</DialogTitle>
					<DialogDescription className="sr-only">
						{t('dialog-agent-edit.description')}
					</DialogDescription>
				</DialogHeader>
				<div className="no-scrollbar -mx-4 max-h-[75vh] overflow-y-auto px-4">
					{schema && values ? (
						<AgentFormFields schema={schema} values={values} onChange={handleChange} />
					) : (
						<p className="text-muted-foreground text-sm">{t('common.loading')}</p>
					)}
					{values?.identity.agent_type === 'leader' && (
						<div className="pt-2">
							<MemberPicker
								selected={selectedMembers}
								onChange={setSelectedMembers}
								systemPrompt={values.identity.system_prompt as string | undefined}
								excludeIds={[agent.id]}
							/>
						</div>
					)}

					{/* ── 版本封板区：冻结/解冻/存版/恢复 ── */}
					<div className="pt-4">
						<Separator className="mb-3" />
						<div className="flex items-center justify-between gap-2">
							<div className="flex min-w-0 items-center gap-2">
								<History className="size-4 shrink-0 text-muted-foreground" />
								<span className="text-sm font-medium">
									{t('dialog-agent-edit.version.section')}
								</span>
								{frozen ? (
									<Badge variant="default" className="shrink-0 gap-1">
										<Lock className="size-3" />
										{t('dialog-agent-edit.version.frozenBadge', {
											version: currentVersion ?? '-',
										})}
									</Badge>
								) : (
									<Badge
										variant="outline"
										className="shrink-0 gap-1 text-muted-foreground"
									>
										<LockOpen className="size-3" />
										{t('dialog-agent-edit.version.openBadge')}
									</Badge>
								)}
							</div>
							<div className="flex shrink-0 gap-1.5">
								{frozen ? (
									<Button
										variant="outline"
										size="sm"
										disabled={versionBusy}
										onClick={() =>
											runVersionOp(() => agentVersionApi.unfreeze(agent.id))
										}
									>
										{versionBusy ? (
											<Loader2 className="size-3.5 animate-spin" />
										) : (
											<LockOpen className="size-3.5" />
										)}
										{t('dialog-agent-edit.version.unfreeze')}
									</Button>
								) : (
									<>
										<Button
											variant="outline"
											size="sm"
											disabled={versionBusy}
											onClick={() =>
												runVersionOp(() => agentVersionApi.saveVersion(agent.id))
											}
										>
											{versionBusy ? (
												<Loader2 className="size-3.5 animate-spin" />
											) : (
												<Save className="size-3.5" />
											)}
											{t('dialog-agent-edit.version.saveVersion')}
										</Button>
										<Button
											variant="outline"
											size="sm"
											disabled={versionBusy}
											onClick={() =>
												runVersionOp(() => agentVersionApi.freeze(agent.id))
											}
										>
											{versionBusy ? (
												<Loader2 className="size-3.5 animate-spin" />
											) : (
												<Lock className="size-3.5" />
											)}
											{t('dialog-agent-edit.version.freeze')}
										</Button>
									</>
								)}
							</div>
						</div>

						{frozen && (
							<p className="mt-2 text-xs text-muted-foreground">
								{t('dialog-agent-edit.version.frozenHint', {
									version: currentVersion ?? '-',
								})}
							</p>
						)}

						{versionStatus && versionStatus.versions.length > 0 ? (
							<ul className="mt-2 flex flex-col gap-1">
								{[...versionStatus.versions].reverse().map((v) => {
									const isCurrent = v.version === currentVersion;
									return (
										<li
											key={v.version}
											className="flex items-center justify-between gap-2 rounded-md border px-2 py-1.5 text-sm"
										>
											<div className="flex min-w-0 items-center gap-2">
												<span className="shrink-0 font-mono text-xs">
													v{v.version}
												</span>
												{isCurrent && (
													<Badge
														variant="secondary"
														className="shrink-0 px-1 py-0 text-[10px]"
													>
														{t('dialog-agent-edit.version.current')}
													</Badge>
												)}
												<span className="min-w-0 truncate text-xs text-muted-foreground">
													{v.label ||
														formatVersionDate(v.created_at)}
												</span>
											</div>
											<Button
												variant="ghost"
												size="sm"
												className="h-6 shrink-0 px-2 text-xs"
												disabled={versionBusy || isCurrent}
												title={t('dialog-agent-edit.version.restoreTooltip')}
												onClick={() => handleRestore(v.version)}
											>
												<RotateCcw className="size-3" />
												{t('dialog-agent-edit.version.restore')}
											</Button>
										</li>
									);
								})}
							</ul>
						) : (
							!frozen && (
								<p className="mt-2 text-xs text-muted-foreground">
									{t('dialog-agent-edit.version.empty')}
								</p>
							)
						)}
					</div>
				</div>
				{errorMsg && (
					<Alert variant="destructive">
						<CircleAlert />
						<AlertDescription className="whitespace-pre-wrap">
							{errorMsg}
						</AlertDescription>
					</Alert>
				)}
				<DialogFooter>
					<Button
						variant="ghost"
						onClick={() => onOpenChange(false)}
						disabled={submitting}
					>
						<CircleAlert className="size-3.5" />
						{t('common.cancel')}
					</Button>
					<Button
						onClick={handleSubmit}
						disabled={!nameValid || submitting || !schema || !values || frozen}
						title={
							frozen
								? t('dialog-agent-edit.version.frozenHint', {
									version: currentVersion ?? '-',
								})
								: undefined
						}
					>
						{submitting ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<Save className="size-3.5" />
						)}
						{submitting ? t('common.saving') : t('common.save')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
