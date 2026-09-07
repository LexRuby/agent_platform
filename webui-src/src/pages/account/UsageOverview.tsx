import {
	ArrowDownToLine,
	ArrowUpFromLine,
	Bot,
	ChevronDown,
	ChevronRight,
	Coins,
	RotateCw,
	Users,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { usageApi } from '@/api';
import type { UsageProduct, UsageSummary } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
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
 * 消费概览（账户中心 Tab，2026-09-08 产品维度 v2）。
 *
 * 平台产出的产品只有两种形态：大A及team / 独立小A。
 * 产品用量表回答"调用一次任务的成本"：
 *   类型 | 名字 | 模型 | 输入 | 输出 | 调用次数
 *   - team 行 = 大A本体 + 全体成员小A 的整体消耗（可展开成员构成）
 *   - agent 行 = 独立小A 自身消耗
 */
export function UsageOverview() {
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
		<div className="flex flex-col gap-5">
			{/* 工具行：时间窗口 + 刷新 */}
			<div className="flex items-center justify-end gap-2">
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

			{loading && !summary ? (
				<UsageSkeleton />
			) : error ? (
				<div className="flex min-h-64 items-center justify-center">
					<Empty className="border-destructive/30">
						<EmptyHeader>
							<EmptyTitle>{t('usage.load-failed')}</EmptyTitle>
							<EmptyDescription>{error}</EmptyDescription>
						</EmptyHeader>
					</Empty>
				</div>
			) : !hasData ? (
				<div className="flex min-h-64 items-center justify-center">
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
					{/* 产品用量表（v2 核心：类型 | 名字 | 模型 | 输入 | 输出） */}
					<ProductUsageCard summary={summary} />
					{/* 每日趋势 */}
					<DailyTrendCard summary={summary} />
				</div>
			)}
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

// ─── 产品用量表（v2：大A及team 整体 / 独立小A） ───────────────────────────────

/** 产品类型徽标：大A及团队（含成员整体） / 独立小A。 */
function ProductTypeBadge({ product }: { product: UsageProduct }) {
	const { t } = useTranslation();
	return product.type === 'team' ? (
		<Badge className="gap-1 bg-violet-50 text-violet-700 hover:bg-violet-50">
			<Users className="size-3" />
			{t('account.product-type-team')}
		</Badge>
	) : (
		<Badge variant="outline" className="gap-1 text-muted-foreground">
			<Bot className="size-3" />
			{t('account.product-type-agent')}
		</Badge>
	);
}

function ProductUsageCard({ summary }: { summary: UsageSummary }) {
	const { t } = useTranslation();
	const [expanded, setExpanded] = useState<Set<string>>(new Set());

	const toggle = (pid: string) => {
		setExpanded((prev) => {
			const next = new Set(prev);
			if (next.has(pid)) {
				next.delete(pid);
			} else {
				next.add(pid);
			}
			return next;
		});
	};

	return (
		<Card className="gap-3">
			<CardHeader>
				<CardTitle className="text-base">{t('account.product-usage')}</CardTitle>
				<CardDescription>{t('account.product-usage-description')}</CardDescription>
			</CardHeader>
			<CardContent>
				<Table>
					<TableHeader>
						<TableRow>
							<TableHead className="w-10" />
							<TableHead>{t('account.product-type')}</TableHead>
							<TableHead>{t('common.name')}</TableHead>
							<TableHead>{t('common.model')}</TableHead>
							<TableHead className="text-right">{t('usage.input-tokens')}</TableHead>
							<TableHead className="text-right">{t('usage.output-tokens')}</TableHead>
							<TableHead className="text-right">{t('usage.total-calls')}</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{summary.products.map((p) => {
							const open = expanded.has(p.product_id);
							return (
								<ProductRows
									key={p.product_id}
									product={p}
									open={open}
									onToggle={() => toggle(p.product_id)}
								/>
							);
						})}
					</TableBody>
				</Table>
			</CardContent>
		</Card>
	);
}

/** 单个产品：产品汇总行 + 模型明细行 +（team）成员构成。 */
function ProductRows({
	product,
	open,
	onToggle,
}: {
	product: UsageProduct;
	open: boolean;
	onToggle: () => void;
}) {
	const { t } = useTranslation();
	const expandable = product.type === 'team';
	return (
		<>
			{/* 产品汇总行 */}
			<TableRow className="bg-muted/40 hover:bg-muted/40">
				<TableCell>
					{expandable ? (
						<button
							type="button"
							onClick={onToggle}
							className="flex size-6 items-center justify-center rounded-md hover:bg-muted"
							title={open
								? t('account.collapse-detail')
								: t('account.expand-detail')}
						>
							{open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
						</button>
					) : null}
				</TableCell>
				<TableCell>
					<ProductTypeBadge product={product} />
				</TableCell>
				<TableCell className="max-w-[220px] truncate font-medium">
					{product.name}
					{expandable && product.members && (
						<span className="ml-1.5 text-xs text-muted-foreground">
							{t('account.team-size', { count: product.members.length })}
						</span>
					)}
				</TableCell>
				<TableCell className="text-xs text-muted-foreground">
					{t('account.models-count', { count: product.by_model.length })}
				</TableCell>
				<TableCell className="text-right font-semibold tabular-nums text-blue-700">
					{formatNumber(product.in)}
				</TableCell>
				<TableCell className="text-right font-semibold tabular-nums text-emerald-700">
					{formatNumber(product.out)}
				</TableCell>
				<TableCell className="text-right font-semibold tabular-nums">
					{formatNumber(product.calls)}
				</TableCell>
			</TableRow>
			{/* 模型明细行 */}
			{product.by_model.map((m) => (
				<TableRow key={`${product.product_id}:${m.model}`} className="text-muted-foreground">
					<TableCell />
					<TableCell className="text-xs">└</TableCell>
					<TableCell className="text-xs text-muted-foreground">
						{t('account.model-detail')}
					</TableCell>
					<TableCell>
						<Badge variant="outline" className="max-w-[180px] truncate font-mono text-xs">
							{m.model}
						</Badge>
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
			{/* team 成员构成（展开时） */}
			{expandable && open && product.members && (
				<TableRow className="bg-violet-50/40 hover:bg-violet-50/40">
					<TableCell />
					<TableCell className="text-xs">└</TableCell>
					<TableCell colSpan={5}>
						<div className="flex flex-col gap-1.5 py-1">
							<div className="text-xs font-medium text-violet-800">
								{t('account.team-breakdown')}
							</div>
							{product.members.map((mem) => (
								<div
									key={mem.agent_id}
									className="flex items-center gap-3 text-xs text-muted-foreground"
								>
									<span className="w-32 truncate font-medium text-foreground">
										{mem.name}
									</span>
									<span className="tabular-nums text-blue-700">
										{t('usage.input-tokens')}: {formatNumber(mem.in)}
									</span>
									<span className="tabular-nums text-emerald-700">
										{t('usage.output-tokens')}: {formatNumber(mem.out)}
									</span>
									<span className="tabular-nums">
										{t('usage.total-calls')}: {formatNumber(mem.calls)}
									</span>
								</div>
							))}
						</div>
					</TableCell>
				</TableRow>
			)}
		</>
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
			<Skeleton className="h-56 rounded-xl" />
		</div>
	);
}
