import { CircleAlert, History, Loader2, Lock, LockOpen, RotateCcw, Send } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { AgentView } from '@/api';
import { agentVersionApi, type AgentVersionStatus } from '@/api/agentVersion';
import { Alert, AlertDescription } from '@/components/ui/alert.tsx';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { useAgents } from '@/hooks/useAgents';
import { formatApiErrorForAlert } from '@/lib/api-error';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	agent: AgentView;
	onUpdated?: () => void;
}

/**
 * 版本管理对话框（发版中心）：
 * - 「发布新版本」：把当前配置固化为 vN（研发完成后的发版动作）
 * - 「冻结」：封板当前版本，停止自我迭代（对外提供服务形态）
 * - 版本列表：每个历史版本可一键「切换到此版本」（切换后对话即用该版配置）
 */
export function AgentVersionDialog({ open, onOpenChange, agent, onUpdated }: Props) {
	const { t } = useTranslation();
	const { refetch } = useAgents();
	const [status, setStatus] = useState<AgentVersionStatus | null>(null);
	const [loading, setLoading] = useState(false);
	const [busy, setBusy] = useState(false);
	const [errorMsg, setErrorMsg] = useState('');
	const [releaseLabel, setReleaseLabel] = useState('');

	// 打开时拉取版本状态（列表接口含 frozen/versions，权威数据源）
	useEffect(() => {
		if (!open) return;
		let cancelled = false;
		setLoading(true);
		setErrorMsg('');
		setReleaseLabel('');
		agentVersionApi
			.list(agent.id)
			.then((s) => {
				if (!cancelled) setStatus(s);
			})
			.catch((e) => setErrorMsg(formatApiErrorForAlert(e)))
			.finally(() => {
				if (!cancelled) setLoading(false);
			});
		return () => {
			cancelled = true;
		};
	}, [open, agent.id]);

	const runOp = async (op: () => Promise<AgentVersionStatus>, closeOnDone = false) => {
		setBusy(true);
		setErrorMsg('');
		try {
			const s = await op();
			setStatus(s);
			await refetch();
			onUpdated?.();
			if (closeOnDone) onOpenChange(false);
		} catch (e) {
			setErrorMsg(formatApiErrorForAlert(e));
		} finally {
			setBusy(false);
		}
	};

	const frozen = status?.frozen ?? agent.version?.frozen ?? false;
	const currentVersion = status?.current_version ?? agent.version?.current_version ?? null;

	const formatVersionDate = (iso: string) => {
		try {
			return new Date(iso).toLocaleString();
		} catch {
			return iso;
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="!w-[440px] !max-w-[440px]">
				<DialogHeader>
					<DialogTitle>
						{t('dialog-agent-version.title', { name: agent.data.name })}
					</DialogTitle>
					<DialogDescription>{t('dialog-agent-version.description')}</DialogDescription>
				</DialogHeader>

				{loading ? (
					<p className="text-muted-foreground text-sm">{t('common.loading')}</p>
				) : (
					<div className="flex flex-col gap-3">
						{/* 当前状态 */}
						<div className="flex items-center justify-between gap-2 rounded-md border px-3 py-2">
							<div className="flex min-w-0 items-center gap-2">
								<span className="text-sm text-muted-foreground">
									{t('dialog-agent-version.currentState')}
								</span>
								{frozen ? (
									<Badge className="gap-1">
										<Lock className="size-3" />
										{t('dialog-agent-version.frozenBadge', {
											version: currentVersion ?? '-',
										})}
									</Badge>
								) : (
									<Badge variant="outline" className="gap-1 text-muted-foreground">
										<LockOpen className="size-3" />
										{t('dialog-agent-version.openBadge')}
									</Badge>
								)}
							</div>
							<Button
								variant={frozen ? 'outline' : 'secondary'}
								size="sm"
								disabled={busy}
								onClick={() =>
									runOp(() =>
										frozen
											? agentVersionApi.unfreeze(agent.id)
											: agentVersionApi.freeze(agent.id),
									)
								}
							>
								{busy ? (
									<Loader2 className="size-3.5 animate-spin" />
								) : frozen ? (
									<LockOpen className="size-3.5" />
								) : (
									<Lock className="size-3.5" />
								)}
								{frozen
									? t('dialog-agent-version.unfreeze')
									: t('dialog-agent-version.freeze')}
							</Button>
						</div>

						{/* 发版（发布新版本） */}
						<div className="flex items-center gap-2 rounded-md border px-3 py-2">
							<Input
								className="h-8 flex-1"
								placeholder={t('dialog-agent-version.releaseLabelPlaceholder')}
								value={releaseLabel}
								onChange={(e) => setReleaseLabel(e.target.value)}
								disabled={busy || frozen}
							/>
							<Button
								size="sm"
								disabled={busy || frozen}
								title={
									frozen
										? t('dialog-agent-version.frozenHint', {
											version: currentVersion ?? '-',
										})
										: t('dialog-agent-version.releaseTooltip')
								}
								onClick={() =>
									runOp(() =>
										agentVersionApi.saveVersion(agent.id, releaseLabel),
									)
								}
							>
								{busy ? (
									<Loader2 className="size-3.5 animate-spin" />
								) : (
									<Send className="size-3.5" />
								)}
								{t('dialog-agent-version.release')}
							</Button>
						</div>

						{/* 版本列表：切换版本 = 对话用该版配置 */}
						<div className="max-h-[280px] overflow-y-auto">
							<div className="mb-1 flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
								<History className="size-3.5" />
								{t('dialog-agent-version.historyHeading')}
							</div>
							{status && status.versions.length > 0 ? (
								<ul className="flex flex-col gap-1">
									{[...status.versions].reverse().map((v) => {
										const isCurrent = v.version === currentVersion;
										return (
											<li
												key={v.version}
												className="flex items-center justify-between gap-2 rounded-md border px-2 py-1.5"
											>
												<div className="flex min-w-0 items-center gap-2">
													<span className="shrink-0 font-mono text-sm">
														v{v.version}
													</span>
													{isCurrent && (
														<Badge
															variant="secondary"
															className="shrink-0 px-1 py-0 text-[10px]"
														>
															{t('dialog-agent-version.current')}
														</Badge>
													)}
													<span className="min-w-0 truncate text-xs text-muted-foreground">
														{v.label || formatVersionDate(v.created_at)}
													</span>
												</div>
												<Button
													variant={isCurrent ? 'ghost' : 'outline'}
													size="sm"
													className="h-6 shrink-0 px-2 text-xs"
													disabled={busy || isCurrent}
													title={t('dialog-agent-version.switchTooltip')}
													onClick={() =>
														runOp(
															() =>
																agentVersionApi.restore(
																	agent.id,
																	v.version,
																),
															true,
														)
													}
												>
													<RotateCcw className="size-3" />
													{isCurrent
														? t('dialog-agent-version.current')
														: t('dialog-agent-version.switch')}
												</Button>
											</li>
										);
									})}
								</ul>
							) : (
								<p className="px-1 text-xs text-muted-foreground">
									{t('dialog-agent-version.empty')}
								</p>
							)}
						</div>

						{frozen && (
							<p className="text-xs text-muted-foreground">
								{t('dialog-agent-version.frozenHint', {
									version: currentVersion ?? '-',
								})}
							</p>
						)}
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
			</DialogContent>
		</Dialog>
	);
}
