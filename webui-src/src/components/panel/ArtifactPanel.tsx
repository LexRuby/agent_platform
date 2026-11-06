import { Download, File, Folder, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

import type { DirectoryEntry } from '@/api';
import { workspaceApi } from '@/api/workspace';
import { PanelEmpty } from '@/components/panel/PanelEmpty';
import { Button } from '@/components/ui/button';

interface ArtifactPanelProps {
	agentId: string | null;
	sessionId: string | null;
}

interface ArtifactFile extends DirectoryEntry {
	path: string;
}

const MAX_DEPTH = 3;
const MAX_FILES = 100;

/** Lists files in the current conversation/project workspace without exposing its host path. */
export function ArtifactPanel({ agentId, sessionId }: ArtifactPanelProps) {
	const [files, setFiles] = useState<ArtifactFile[]>([]);
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const [downloading, setDownloading] = useState<string | null>(null);

	const load = useCallback(async () => {
		if (!agentId || !sessionId) {
			setFiles([]);
			return;
		}
		setLoading(true);
		setError(null);
		try {
			const result: ArtifactFile[] = [];
			const visit = async (path: string, depth: number): Promise<void> => {
				if (result.length >= MAX_FILES) return;
				const listing = await workspaceApi.directories(agentId, sessionId, path);
				for (const entry of listing.entries) {
					const entryPath = path ? `${path}/${entry.name}` : entry.name;
					if (entry.is_dir) {
						if (depth < MAX_DEPTH) await visit(entryPath, depth + 1);
					} else {
						result.push({ ...entry, path: entryPath });
						if (result.length >= MAX_FILES) return;
					}
				}
			};
			await visit('', 0);
			setFiles(result);
		} catch {
			setFiles([]);
			setError('当前工作区暂时无法读取');
		} finally {
			setLoading(false);
		}
	}, [agentId, sessionId]);

	useEffect(() => { void load(); }, [load]);

	const download = async (path: string) => {
		if (!agentId || !sessionId) return;
		setDownloading(path);
		try {
			await workspaceApi.download(agentId, sessionId, path);
		} catch {
			setError(`无法下载 ${path}`);
		} finally {
			setDownloading(null);
		}
	};

	if (!agentId || !sessionId) return <PanelEmpty icon={Folder} title="打开会话后查看产物" />;
	if (loading) return <div className="p-3 text-xs text-muted-foreground">正在读取工作区…</div>;
	if (error && files.length === 0) {
		return (
			<div className="flex flex-col items-center gap-2 p-3 text-center text-xs text-muted-foreground">
				<span>{error}</span>
				<Button variant="outline" size="xs" onClick={() => void load()}>
					<RefreshCw className="size-3" /> 重试
				</Button>
			</div>
		);
	}
	if (files.length === 0) return <PanelEmpty icon={File} title="当前工作区暂无文件" />;

	return (
		<div className="flex flex-col gap-1">
			<div className="mb-1 flex items-center justify-between px-1 text-[11px] text-muted-foreground">
				<span>当前工作区文件</span>
				<Button variant="ghost" size="icon-xs" title="刷新" onClick={() => void load()}>
					<RefreshCw className="size-3" />
				</Button>
			</div>
			{files.map((file) => (
				<div key={file.path} className="flex min-w-0 items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-muted">
					{file.path.includes('/') ? <Folder className="size-3.5 shrink-0 text-muted-foreground" /> : <File className="size-3.5 shrink-0 text-muted-foreground" />}
					<span className="min-w-0 flex-1 truncate text-xs" title={file.path}>{file.path}</span>
					<Button
						variant="ghost"
						size="icon-xs"
						title={`下载 ${file.path}`}
						disabled={downloading === file.path}
						onClick={() => void download(file.path)}
					>
						<Download className="size-3.5" />
					</Button>
				</div>
			))}
			{error && <span className="px-1 text-[11px] text-destructive">{error}</span>}
		</div>
	);
}
