/**
 * 任务归档对话框：把主理人会话协作过程沉淀为可复用的封档团队。
 *
 * 流程：LLM 生成草稿（任务总结 + 工作流步骤 + 建议注册的新 agent）
 * → 人工编辑确认 → 提交封档（新 agent 自动注册入库，
 * 后续可在创建大A 时「从封档导入」预填工作流提示词）。
 */
import { Archive, CircleAlert, Loader2, Plus, Sparkles, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { teamArchiveApi, type NewAgentDraft } from '@/api/leaderTeam';
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
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { formatApiErrorForAlert } from '@/lib/api-error';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	agentId: string;
	sessionId: string;
	leaderName: string;
	/** 图上当前的团队成员（自动带入封档记录） */
	members: { id: string; name: string }[];
}

type Stage = 'idle' | 'draft' | 'submitting';

export function ArchiveDialog({
	open,
	onOpenChange,
	agentId,
	sessionId,
	leaderName,
	members,
}: Props) {
	const { t } = useTranslation();
	const [stage, setStage] = useState<Stage>('idle');
	const [name, setName] = useState('');
	const [summary, setSummary] = useState('');
	const [steps, setSteps] = useState<string[]>([]);
	const [newAgents, setNewAgents] = useState<NewAgentDraft[]>([]);
	const [errorMsg, setErrorMsg] = useState('');
	const [fallbackNote, setFallbackNote] = useState(false);

	const reset = () => {
		setStage('idle');
		setName('');
		setSummary('');
		setSteps([]);
		setNewAgents([]);
		setErrorMsg('');
		setFallbackNote(false);
	};

	const handleClose = (v: boolean) => {
		if (!v) reset();
		onOpenChange(v);
	};

	/** 第一步：LLM 生成草稿 */
	const generateDraft = async () => {
		setErrorMsg('');
		setStage('draft');
		setFallbackNote(false);
		try {
			const res = await teamArchiveApi.summarize(agentId, sessionId);
			setSummary(res.summary);
			setSteps(res.workflow_steps);
			setNewAgents(res.new_agents);
			if (!name) setName(`${leaderName}·${t('dialog-archive.defaultName')}`);
			if (res.fallback) setFallbackNote(true);
		} catch (e) {
			setErrorMsg(formatApiErrorForAlert(e));
			// LLM 失败仍允许人工填写
			if (!name) setName(`${leaderName}·${t('dialog-archive.defaultName')}`);
		}
	};

	/** 第二步：确认提交封档 */
	const submit = async () => {
		if (!name.trim() || !summary.trim()) return;
		setErrorMsg('');
		setStage('submitting');
		try {
			await teamArchiveApi.create({
				name: name.trim(),
				summary,
				workflow_steps: steps.filter((s) => s.trim()),
				team_members: members,
				new_agents: newAgents.filter((a) => a.name.trim()),
				source_agent_id: agentId,
				source_session_id: sessionId,
			});
			handleClose(false);
		} catch (e) {
			setErrorMsg(formatApiErrorForAlert(e));
			setStage('draft');
		}
	};

	return (
		<Dialog open={open} onOpenChange={handleClose}>
			<DialogContent className="!w-[560px] !max-w-[560px]">
				<DialogHeader>
					<DialogTitle>{t('dialog-archive.title')}</DialogTitle>
					<DialogDescription className="sr-only">
						{t('dialog-archive.description')}
					</DialogDescription>
				</DialogHeader>

				{stage === 'idle' ? (
					<div className="space-y-3 py-2">
						<p className="text-sm text-muted-foreground">
							{t('dialog-archive.intro')}
						</p>
						{members.length > 0 && (
							<div className="flex flex-wrap gap-1.5">
								{members.map((m) => (
									<Badge key={m.id} variant="secondary">
										{m.name}
									</Badge>
								))}
							</div>
						)}
						<Button onClick={generateDraft}>
							<Sparkles className="size-3.5" />
							{t('dialog-archive.generate')}
						</Button>
					</div>
				) : (
					<div className="no-scrollbar -mx-4 max-h-[60vh] space-y-3 overflow-y-auto px-4">
						{fallbackNote && (
							<Alert>
								<CircleAlert />
								<AlertDescription>
									{t('dialog-archive.fallbackNote')}
								</AlertDescription>
							</Alert>
						)}
						<div className="space-y-1">
							<label className="text-sm font-medium">
								{t('dialog-archive.name')}
							</label>
							<Input value={name} onChange={(e) => setName(e.target.value)} />
						</div>
						<div className="space-y-1">
							<label className="text-sm font-medium">
								{t('dialog-archive.summary')}
							</label>
							<Textarea
								value={summary}
								onChange={(e) => setSummary(e.target.value)}
								rows={3}
								placeholder={t('dialog-archive.summaryPlaceholder')}
							/>
						</div>
						<div className="space-y-1">
							<label className="text-sm font-medium">
								{t('dialog-archive.workflow')}
							</label>
							{steps.length === 0 && (
								<Button
									variant="outline"
									size="sm"
									onClick={() => setSteps([''])}
								>
									<Plus className="size-3.5" />
									{t('dialog-archive.addStep')}
								</Button>
							)}
							{steps.map((s, i) => (
								<div key={i} className="flex gap-1.5">
									<span className="pt-1.5 text-xs text-muted-foreground">
										{i + 1}.
									</span>
									<Input
										value={s}
										onChange={(e) =>
											setSteps(steps.map((x, j) => (j === i ? e.target.value : x)))
										}
										className="h-8 text-sm"
									/>
									<Button
										variant="ghost"
										size="sm"
										className="h-8 shrink-0 px-2"
										onClick={() => setSteps(steps.filter((_, j) => j !== i))}
									>
										<Trash2 className="size-3.5" />
									</Button>
								</div>
							))}
							{steps.length > 0 && (
								<Button
									variant="ghost"
									size="sm"
									onClick={() => setSteps([...steps, ''])}
								>
									<Plus className="size-3.5" />
									{t('dialog-archive.addStep')}
								</Button>
							)}
						</div>
						<div className="space-y-1">
							<label className="text-sm font-medium">
								{t('dialog-archive.newAgents')}
							</label>
							<p className="text-xs text-muted-foreground">
								{t('dialog-archive.newAgentsHint')}
							</p>
							{newAgents.map((a, i) => (
								<div
									key={i}
									className="space-y-1.5 rounded-md border p-2"
								>
									<div className="flex gap-1.5">
										<Input
											value={a.name}
											placeholder={t('dialog-archive.agentName')}
											onChange={(e) =>
												setNewAgents(
													newAgents.map((x, j) =>
														j === i ? { ...x, name: e.target.value } : x,
													),
												)
											}
											className="h-8 text-sm"
										/>
										<Button
											variant="ghost"
											size="sm"
											className="h-8 shrink-0 px-2"
											onClick={() => setNewAgents(newAgents.filter((_, j) => j !== i))}
										>
											<Trash2 className="size-3.5" />
										</Button>
									</div>
									<Textarea
										value={a.system_prompt}
										placeholder={t('dialog-archive.agentPrompt')}
										onChange={(e) =>
											setNewAgents(
												newAgents.map((x, j) =>
													j === i ? { ...x, system_prompt: e.target.value } : x,
												),
											)
										}
										rows={2}
										className="text-sm"
									/>
								</div>
							))}
							<Button
								variant="outline"
								size="sm"
								onClick={() =>
									setNewAgents([...newAgents, { name: '', description: '', system_prompt: '' }])
								}
							>
								<Plus className="size-3.5" />
								{t('dialog-archive.addAgent')}
							</Button>
						</div>
					</div>
				)}

				{errorMsg && (
					<Alert variant="destructive">
						<CircleAlert />
						<AlertDescription className="whitespace-pre-wrap">
							{errorMsg}
						</AlertDescription>
					</Alert>
				)}

				{stage !== 'idle' && (
					<DialogFooter>
						<Button
							variant="ghost"
							onClick={() => handleClose(false)}
							disabled={stage === 'submitting'}
						>
							{t('common.cancel')}
						</Button>
						<Button
							onClick={submit}
							disabled={
								stage === 'submitting' || !name.trim() || !summary.trim()
							}
						>
							{stage === 'submitting' ? (
								<Loader2 className="size-3.5 animate-spin" />
							) : (
								<Archive className="size-3.5" />
							)}
							{t('dialog-archive.confirm')}
						</Button>
					</DialogFooter>
				)}
			</DialogContent>
		</Dialog>
	);
}
