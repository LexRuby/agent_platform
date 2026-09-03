import { Archive, CircleAlert, Loader2, PlusCircle } from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import type { ContextConfig, InviteConfig, ReActConfig } from '@/api';
import { teamArchiveApi, type TeamArchive } from '@/api/leaderTeam';
import {
	AgentFormFields,
	defaultAgentFormValues,
	type AgentFormValues,
	type AgentSection,
} from '@/components/form/AgentFormFields';
import { MemberPicker } from '@/components/form/MemberPicker';
import type { SchemaFormValue } from '@/components/form/SchemaForm';
import { Alert, AlertDescription } from '@/components/ui/alert.tsx';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogFooter,
	DialogHeader,
	DialogTitle,
	DialogDescription,
	DialogTrigger,
} from '@/components/ui/dialog';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
import { useAgents } from '@/hooks/useAgents';
import { useAgentSchema } from '@/hooks/useAgentSchema';
import { formatApiErrorForAlert } from '@/lib/api-error';

interface Props {
	onCreated?: () => void;
	children: ReactNode;
}

export function AgentDialog({ onCreated, children }: Props) {
	const { create } = useAgents();
	const { t } = useTranslation();
	const { schema } = useAgentSchema();
	const [open, setOpen] = useState(false);
	const [submitting, setSubmitting] = useState(false);
	const [values, setValues] = useState<AgentFormValues | null>(null);
	const [errorMsg, setErrorMsg] = useState('');
	// 预置团队成员（大A 专属）
	const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
	// 封档列表（从封档导入）
	const [archives, setArchives] = useState<TeamArchive[]>([]);

	useEffect(() => {
		if (open && schema && !values) {
			setValues(defaultAgentFormValues(schema));
		}
		if (!open) {
			setValues(null);
			setErrorMsg('');
			setSelectedMembers([]);
		}
	}, [open, schema, values]);

	// 打开时拉取封档列表（供「从封档导入」）
	useEffect(() => {
		if (!open) return;
		teamArchiveApi
			.list()
			.then(setArchives)
			.catch(() => setArchives([]));
	}, [open]);

	const isLeader = values?.identity.agent_type === 'leader';

	const handleChange = (section: AgentSection, key: string, value: SchemaFormValue) => {
		setErrorMsg('');
		setValues((prev) =>
			prev ? { ...prev, [section]: { ...prev[section], [key]: value } } : prev,
		);
	};

	/** 从封档导入：预填 system_prompt（工作流提示词）+ 团队成员 */
	const handleImportArchive = (archiveId: string) => {
		const archive = archives.find((a) => a.id === archiveId);
		if (!archive || !values) return;
		const workflowPrompt = [
			`你是一个按已验证工作流运作的团队主理人（leader agent）。`,
			`团队任务领域：${archive.name}`,
			``,
			`## 已验证的工作流程`,
			...archive.workflow_steps.map((s, i) => `${i + 1}. ${s}`),
			``,
			`## 任务总结（参考）`,
			archive.summary,
		].join('\n');
		setValues((prev) =>
			prev
				? {
						...prev,
						identity: {
							...prev.identity,
							system_prompt: workflowPrompt,
							agent_type: 'leader',
						},
					}
				: prev,
		);
		setSelectedMembers(archive.team_members.map((m) => m.id));
	};

	// 成员档案：封档里的成员 + 全量在册 agent（MemberPicker 的候选）。
	// 名字/职责必须用真实值——后端把它注入主理人提示词，
	// 曾因回退 id.slice(0,8) 导致提示词里只剩裸 id，模型无法
	// 理解成员身份，只能邀请无名"成员"。
	const { agents: allAgents } = useAgents();
	const memberDetails = useMemo(() => {
		const map: Record<
			string,
			{ id: string; name: string; description: string }
		> = {};
		archives.forEach((a) =>
			a.team_members.forEach((m) => {
				map[m.id] = { ...m, description: m.description ?? '' };
			}),
		);
		allAgents
			.filter((a) => a.agent_type !== 'leader')
			.forEach((a) => {
				map[a.id] = {
					id: a.id,
					name: a.data.name,
					description: a.data.invite_config?.invite_description ?? '',
				};
			});
		return map;
	}, [archives, allAgents]);

	const handleSubmit = async () => {
		if (!values) return;
		const name = (values.identity.name as string | undefined)?.trim();
		if (!name) return;
		setErrorMsg('');
		setSubmitting(true);
		try {
			await create(
				{
					name,
					system_prompt: values.identity.system_prompt as string | undefined,
					agent_type: values.identity.agent_type as 'leader' | 'member' | undefined,
					team_members: isLeader
						? selectedMembers.map((id) => ({
								id,
								name: memberDetails[id]?.name ?? id.slice(0, 8),
								description: memberDetails[id]?.description ?? '',
							}))
						: undefined,
					context_config: values.context_config as unknown as ContextConfig,
					react_config: values.react_config as unknown as ReActConfig,
					invite_config: values.invite_config as unknown as InviteConfig,
				},
				{ silent: true },
			);
			setOpen(false);
			onCreated?.();
		} catch (e) {
			setErrorMsg(formatApiErrorForAlert(e));
		} finally {
			setSubmitting(false);
		}
	};

	const nameValid = !!(values?.identity.name as string | undefined)?.trim();

	return (
		<Dialog open={open} onOpenChange={setOpen}>
			<DialogTrigger asChild>{children}</DialogTrigger>
			<DialogContent className="!w-[500px] !max-w-[500px]">
				<DialogHeader>
					<DialogTitle>{t('dialog-agent-create.title')}</DialogTitle>
					<DialogDescription className="sr-only">
						{t('dialog-agent-create.description')}
					</DialogDescription>
				</DialogHeader>
				<div className="no-scrollbar -mx-4 max-h-[75vh] space-y-3 overflow-y-auto px-4">
					{archives.length > 0 && (
						<div className="flex items-center gap-2">
							<Archive className="size-3.5 shrink-0 text-muted-foreground" />
							<Select onValueChange={handleImportArchive}>
								<SelectTrigger className="h-8 flex-1 text-sm">
									<SelectValue
										placeholder={t('dialog-agent-create.importArchive')}
									/>
								</SelectTrigger>
								<SelectContent>
									{archives.map((a) => (
										<SelectItem key={a.id} value={a.id}>
											{a.name}
										</SelectItem>
									))}
								</SelectContent>
							</Select>
						</div>
					)}
					{schema && values ? (
						<AgentFormFields schema={schema} values={values} onChange={handleChange} />
					) : (
						<p className="text-muted-foreground text-sm">{t('common.loading')}</p>
					)}
					{isLeader && (
						<MemberPicker
							selected={selectedMembers}
							onChange={setSelectedMembers}
							systemPrompt={values?.identity.system_prompt as string | undefined}
						/>
					)}
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
					<Button variant="ghost" onClick={() => setOpen(false)} disabled={submitting}>
						<CircleAlert className="size-3.5" />
						{t('common.cancel')}
					</Button>
					<Button
						onClick={handleSubmit}
						disabled={!nameValid || submitting || !schema || !values}
					>
						{submitting ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<PlusCircle className="size-3.5" />
						)}
						{submitting ? t('common.creating') : t('common.create')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
