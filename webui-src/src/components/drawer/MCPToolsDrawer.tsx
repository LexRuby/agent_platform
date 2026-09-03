import { useEffect, useState } from 'react';

import { mcpApi } from '@/api';
import type { MCPToolsResponse, MCPView } from '@/api';
import { Alert, AlertDescription } from '@/components/ui/alert.tsx';
import { Badge } from '@/components/ui/badge.tsx';
import {
	Drawer,
	DrawerContent,
	DrawerDescription,
	DrawerHeader,
	DrawerTitle,
} from '@/components/ui/drawer.tsx';
import { Separator } from '@/components/ui/separator.tsx';
import { Spinner } from '@/components/ui/spinner.tsx';
import { useTranslation } from '@/i18n/useI18n';
import { formatApiErrorForAlert } from '@/lib/api-error';

interface Props {
	/** The MCP to inspect, or `null` to keep the drawer closed. */
	mcp: MCPView | null;
	onOpenChange: (open: boolean) => void;
}

/** One tool card: name, description, and the parameter schema. */
function ToolCard({ name, description, schema }: {
	name: string;
	description: string;
	schema: Record<string, unknown> | null;
}) {
	const { t } = useTranslation();
	const required = (schema?.required as string[] | undefined) ?? [];
	const properties = (schema?.properties as Record<
		string,
		{ type?: string; description?: string }
	> | null) ?? null;

	return (
		<div className="flex flex-col gap-y-2 rounded-lg border bg-surface-muted/40 p-3">
			<div className="flex items-center gap-x-2">
				<code className="text-xs font-semibold">{name}</code>
				{required.length > 0 && (
					<Badge variant="secondary" className="text-[10px] px-1.5 py-0">
						{t('mcp-tools.requiredCount', { count: required.length })}
					</Badge>
				)}
			</div>
			{description && (
				<p className="text-xs leading-relaxed text-muted-foreground whitespace-pre-wrap">
					{description}
				</p>
			)}
			{properties && Object.keys(properties).length > 0 && (
				<div className="flex flex-col gap-y-1">
					<span className="text-[10px] uppercase tracking-wide text-text-tertiary">
						{t('mcp-tools.parametersLabel')}
					</span>
					{Object.entries(properties).map(([key, prop]) => (
						<div
							key={key}
							className="flex items-baseline gap-x-2 text-xs font-mono"
						>
							<code className="shrink-0">
								{required.includes(key) ? `${key}*` : key}
							</code>
							{prop.type && (
								<span className="text-[10px] text-text-tertiary shrink-0">
									{prop.type}
								</span>
							)}
							{prop.description && (
								<span className="text-muted-foreground min-w-0 break-words">
									{prop.description}
								</span>
							)}
						</div>
					))}
				</div>
			)}
		</div>
	);
}

/**
 * Opens one installed MCP and shows the tools its server actually exposes —
 * name, description, and parameters — so "what can this MCP do for me" is a
 * click away instead of a guess.
 */
export function MCPToolsDrawer({ mcp, onOpenChange }: Props) {
	const { t } = useTranslation();
	const [data, setData] = useState<MCPToolsResponse | null>(null);
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState('');

	// Fetch on open; cancel on close / switch so a slow server cannot write
	// its answer into the next MCP's drawer.
	useEffect(() => {
		if (!mcp) return;
		let cancelled = false;
		setLoading(true);
		setError('');
		setData(null);
		mcpApi
			.tools(mcp.id)
			.then((res) => {
				if (!cancelled) setData(res);
			})
			.catch((e) => {
				if (!cancelled) setError(formatApiErrorForAlert(e));
			})
			.finally(() => {
				if (!cancelled) setLoading(false);
			});
		return () => {
			cancelled = true;
		};
	}, [mcp]);

	return (
		<Drawer direction="right" open={mcp !== null} onOpenChange={onOpenChange}>
			<DrawerContent className="data-[vaul-drawer-direction=right]:sm:max-w-2xl">
				<DrawerHeader className="shrink-0 gap-2">
					<div className="flex items-center gap-x-2 min-w-0">
						<DrawerTitle className="truncate">
							{mcp?.display_name || mcp?.name}
						</DrawerTitle>
						{data && (
							<Badge variant="secondary" className="text-[10px] px-1.5 py-0 shrink-0">
								{t('mcp-tools.toolCount', { count: data.tools.length })}
							</Badge>
						)}
					</div>
					<DrawerDescription>
						{mcp?.description || t('mcp-tools.descriptionFallback')}
					</DrawerDescription>
					{data && (
						<p className="text-xs text-muted-foreground font-mono">
							{t('mcp-tools.serverLabel')} {data.server}
						</p>
					)}
				</DrawerHeader>

				<Separator />

				<div className="flex-1 min-h-0 overflow-y-auto scroll-fade px-4 py-4">
					{loading ? (
						<div className="flex justify-center py-10">
							<Spinner />
						</div>
					) : error ? (
						<Alert variant="destructive">
							<AlertDescription>{error}</AlertDescription>
						</Alert>
					) : data && data.tools.length > 0 ? (
						<div className="flex flex-col gap-y-3">
							{data.tools.map((tool) => (
								<ToolCard
									key={tool.name}
									name={tool.name}
									description={tool.description}
									schema={tool.input_schema}
								/>
							))}
						</div>
					) : (
						<p className="text-sm text-muted-foreground">
							{t('mcp-tools.emptyTools')}
						</p>
					)}
				</div>
			</DrawerContent>
		</Drawer>
	);
}
