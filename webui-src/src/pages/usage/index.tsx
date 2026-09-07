import { ArrowDownToLine, ArrowUpFromLine, Coins, RotateCw } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { usageApi } from '@/api';
import type { UsageSummary } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
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
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import { formatNumber } from '@/utils/common';

/** 可选统计窗口（天）。 */
const RANGE_OPTIONS = [7, 30, 90] as const;

/**
 * 用量统计页（消费计量 v1，2026-09-07）。
 *
 * 回答用户问题"我这个账号的消耗情况"：
 * - 顶部总计：输入 / 输出 / 缓存 tokens + 调用次数
 * - 按日期：每日消耗趋势（输入/输出双色条）
 * - 按智能体：大A（主理人）/ 小A（成员）各自消耗
 * - 按模型：不同模型的消耗分布
 *
 * 数据按登录账号隔离（后端会话注入身份），充值/计费暂未接入，
 * 此页先做"消耗可见"。
 */
export function UsagePage() {
	const { t } = useTranslation();
	const [days, setDays] = useState<number>(30);
	const [summary, setSummary] = useState<UsageSummary | null>(null);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState<string | null>(null);

	const load = useCallback(
		async (rangeDays: number) => {
			setLoading(true);
			setError(null);
			try {
				setSummary(await usageApi.summary(rangeDays));
			} catch (e) {
				setError(e instanceof Error ? e.message : String(e));
			} finally {
				setLoading(false);
			}
		},
		[],
	);

	useEffect(() => {
		void load(days);
	}, [days, load]);

	const hasData = !!summary && (summary.totals.calls > 0 || summary.totals.in > 0);

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				{/* 页头：标题 + 时间窗口切换 + 手动刷新 */}
				<div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
					<div>
						<div className="text-2xl font-semibold">{t('usage.title')}</div>
						<div className="mt-1 text-sm text-muted-foreground">{t('usage.subtitle')}</div>
					</div>
					<div className="flex items-center gap-2">
						<Button
							size="icon-sm"
							variant="ghost"
							disabled={loading}
							onClick={() => void load(days)}
							title={t('usage.refresh')}
						>
							<RotateCw className={cn(loading && 'animate-spin')} />
						</Button>
						<Tabs
							value={String(days)}
							onValueChange={(value) => setDays(Number(value))}
						>
							<TabsList>
								{RANGE_OPTIONS.map((d) => (
									<TabsTrigger key={d} value={String(d)} className="border-none w-[72px]">
										{t('usage.range-days', { days: d })}
									</TabsTrigger>
								))}
							</TabsList>
						</Tabs>
					</div>
				</div>
				<Separator />

				<div className="flex-1 overflow-y-auto px-6 py-5">
					{loading && !summary ? (
						<UsageSkeleton />
					) : error ? (
						<div className="flex h-full items-center justify-center">
							<Empty className="border-destructive/30">
								<EmptyHeader>
									<EmptyTitle>{t('usage.load-failed')}</EmptyTitle>
									<EmptyDescription>{error}</EmptyDescription>
								</EmptyHeader>
							</Empty>
						</div>
					) : !hasData ? (
						<div className="flex h-full items-center justify-center">
							<Empty>
								<EmptyHeader>
									<EmptyTitle>{t('usage.empty-title')}</EmptyTitle>
									<EmptyDescription>{t('usage.empty-description')}</EmptyDescription>
								</EmptyHeader>
							</Empty>
						</div>
					) : (
						<div className="flex flex-col gap-5">
							{/* 总计卡片：输入 / 输出 / 缓存 / 调用次数 */}
							<TotalCards summary={summary} />
							{/* 每日趋势 */}
							<DailyTrendCard summary={summary} />
							{/* 大A/小A + 模型 双栏 */}
							<div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
								<AgentTableCard summary={summary} />
								<ModelTableCard summary={summary} />
							</div>
						</div>
					)}
				</div>
			</main>
		</div>
	);
}

// ─── 总计卡片 ─────────────────────────────────────────────────────────────────

function TotalCards({ summary }: { summary: UsageSummary }) {
	const { t } = useTranslation();
	const cards = [
		{
			label: t('usage.total-input'),
			value: summary.totals.in,
			icon: <ArrowDownToLine className="size-4 text-blue-600" />,
			badge: 'bg-blue-50 text-blue-700',
		},
		{
			label: t('usage.total-output'),
			value: summary.totals.out,
			icon: <ArrowUpFromLine className="size-4 text-emerald-600" />,
			badge: 'bg-emerald-50 text-emerald-700',
		},
		{
			label: t('usage.total-cache'),
			value: summary.totals.cache,
			icon: <Coins className="size-4 text-amber-600" />,
			badge: 'bg-amber-50 text-amber-700',
		},
		{
			label: t('usage.total-calls'),
			value: summary.totals.calls,
			icon: null,
			badge: 'bg-violet-50 text-violet-700',
		},
	];
	return (
		<div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
			{cards.map((c) => (
				<Card key={c.label} className="gap-2 py-4">
					<CardContent className="flex flex-col gap-1.5 px-4">
						<div className="flex items-center gap-2">
							<span
								className={cn(
									'flex size-6 items-center justify-center rounded-md',
									c.badge,
								)}
							>
								{c.icon ?? <span className="text-xs font-semibold">#</span>}
							</span>
							<span className="text-sm text-muted-foreground">{c.label}</span>
						</div>
						<div className="text-2xl font-semibold tabular-nums">
							{formatNumber(c.value)}
						</div>
					</CardContent>
				</Card>
			))}
		</div>
	);
}

// ─── 每日趋势（纯 CSS 双色条，输入蓝 / 输出绿） ────────────────────────────────

function DailyTrendCard({ summary }: { summary: UsageSummary }) {
	const { t } = useTranslation();
	// by_date 后端倒序（最新在前）；渲染按时间正序，近 N 天贴底更直观
	const rows = useMemo(() => [...summary.by_date].reverse(), [summary.by_date]);
	const max = useMemo(
		() => Math.max(1, ...rows.map((r) => r.in + r.out)),
		[rows],
	);
	// 日期太密时只隔行标日期，避免横向挤压
	const showLabel = (i: number) => rows.length <= 16 || i % Math.ceil(rows.length / 8) === 0;

	return (
		<Card className="gap-3">
			<CardHeader>
				<CardTitle className="text-base">{t('usage.daily-trend')}</CardTitle>
				<CardDescription>
					<span className="mr-3 inline-flex items-center gap-1.5">
						<span className="inline-block h-2.5 w-2.5 rounded-sm bg-blue-500/80" />
						{t('usage.input-tokens')}
					</span>
					<span className="inline-flex items-center gap-1.5">
						<span className="inline-block h-2.5 w-2.5 rounded-sm bg-emerald-500/80" />
						{t('usage.output-tokens')}
					</span>
				</CardDescription>
			</CardHeader>
			<CardContent>
				<div className="flex h-40 items-end gap-1">
					{rows.map((r, i) => {
						const inPct = (r.in / max) * 100;
						const outPct = (r.out / max) * 100;
						return (
							<div
								key={r.date}
								className="group relative flex h-full flex-1 flex-col justify-end"
								title={`${r.date}  ${t('usage.input-tokens')}: ${formatNumber(r.in)}  ${t('usage.output-tokens')}: ${formatNumber(r.out)}  ${t('usage.total-calls')}: ${formatNumber(r.calls)}`}
							>
								<div
									className="w-full rounded-t-sm bg-emerald-500/80 transition-colors group-hover:bg-emerald-500"
									style={{ height: `${Math.max(outPct, r.out > 0 ? 2 : 0)}%` }}
								/>
								<div
									className="w-full bg-blue-500/80 transition-colors group-hover:bg-blue-500"
									style={{ height: `${Math.max(inPct, r.in > 0 ? 2 : 0)}%` }}
								/>
								{showLabel(i) && (
									<div className="mt-1 truncate text-center text-[10px] text-muted-foreground">
										{r.date.slice(5)}
									</div>
								)}
							</div>
						);
					})}
				</div>
			</CardContent>
		</Card>
	);
}

// ─── 按智能体（大A / 小A） ────────────────────────────────────────────────────

function AgentTableCard({ summary }: { summary: UsageSummary }) {
	const { t } = useTranslation();
	return (
		<Card className="gap-3">
			<CardHeader>
				<CardTitle className="text-base">{t('usage.by-agent')}</CardTitle>
				<CardDescription>{t('usage.by-agent-description')}</CardDescription>
			</CardHeader>
			<CardContent>
				<Table>
					<TableHeader>
						<TableRow>
							<TableHead>{t('common.name')}</TableHead>
							<TableHead className="text-right">{t('usage.input-tokens')}</TableHead>
							<TableHead className="text-right">{t('usage.output-tokens')}</TableHead>
							<TableHead className="text-right">{t('usage.total-calls')}</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{summary.by_agent.map((a) => (
							<TableRow key={a.agent_id}>
								<TableCell className="max-w-[180px] truncate font-medium">
									{a.name}
								</TableCell>
								<TableCell className="text-right tabular-nums text-blue-700">
									{formatNumber(a.in)}
								</TableCell>
								<TableCell className="text-right tabular-nums text-emerald-700">
									{formatNumber(a.out)}
								</TableCell>
								<TableCell className="text-right tabular-nums">
									{formatNumber(a.calls)}
								</TableCell>
							</TableRow>
						))}
					</TableBody>
				</Table>
			</CardContent>
		</Card>
	);
}

// ─── 按模型 ───────────────────────────────────────────────────────────────────

function ModelTableCard({ summary }: { summary: UsageSummary }) {
	const { t } = useTranslation();
	// 占比条：按总 token 消耗的比例
	const max = Math.max(1, ...summary.by_model.map((m) => m.in + m.out));
	return (
		<Card className="gap-3">
			<CardHeader>
				<CardTitle className="text-base">{t('usage.by-model')}</CardTitle>
				<CardDescription>{t('usage.by-model-description')}</CardDescription>
			</CardHeader>
			<CardContent>
				<Table>
					<TableHeader>
						<TableRow>
							<TableHead>{t('common.model')}</TableHead>
							<TableHead className="text-right">{t('usage.input-tokens')}</TableHead>
							<TableHead className="text-right">{t('usage.output-tokens')}</TableHead>
							<TableHead className="text-right">{t('usage.total-calls')}</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{summary.by_model.map((m) => (
							<TableRow key={m.model}>
								<TableCell className="max-w-[200px]">
									<div className="flex items-center gap-2">
										<Badge variant="outline" className="max-w-[160px] truncate font-mono">
											{m.model}
										</Badge>
									</div>
									{/* 占比条：直观看出哪个模型吃得最多 */}
									<div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
										<div
											className="h-full rounded-full bg-violet-400"
											style={{ width: `${((m.in + m.out) / max) * 100}%` }}
										/>
									</div>
								</TableCell>
								<TableCell className="text-right tabular-nums text-blue-700">
									{formatNumber(m.in)}
								</TableCell>
								<TableCell className="text-right tabular-nums text-emerald-700">
									{formatNumber(m.out)}
								</TableCell>
								<TableCell className="text-right tabular-nums">
									{formatNumber(m.calls)}
								</TableCell>
							</TableRow>
						))}
					</TableBody>
				</Table>
			</CardContent>
		</Card>
	);
}

// ─── 加载骨架 ─────────────────────────────────────────────────────────────────

function UsageSkeleton() {
	return (
		<div className="flex flex-col gap-5">
			<div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
				{Array.from({ length: 4 }).map((_, i) => (
					<Skeleton key={i} className="h-24 rounded-xl" />
				))}
			</div>
			<Skeleton className="h-64 rounded-xl" />
			<div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
				<Skeleton className="h-56 rounded-xl" />
				<Skeleton className="h-56 rounded-xl" />
			</div>
		</div>
	);
}
