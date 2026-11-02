import { FolderKanban, MessageSquare, Plus } from 'lucide-react';
import { useEffect, useState } from 'react';

import { workspaceProjectApi, type WorkspaceProject } from '@/api';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
	DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

interface Props {
	onCreate: (workspaceId?: string) => Promise<void>;
	disabled?: boolean;
}

export function CreateSessionDialog({ onCreate, disabled }: Props) {
	const { t } = useTranslation();
	const [open, setOpen] = useState(false);
	const [projects, setProjects] = useState<WorkspaceProject[]>([]);
	const [selected, setSelected] = useState<string | null>(null);
	const [name, setName] = useState('');
	const [pending, setPending] = useState(false);

	useEffect(() => {
		if (!open) return;
		void workspaceProjectApi.list().then(setProjects);
	}, [open]);

	const createProject = async () => {
		if (!name.trim()) return;
		setPending(true);
		try {
			const project = await workspaceProjectApi.create(name.trim());
			setProjects((current) => [...current, project]);
			setSelected(project.workspace_id);
			setName('');
		} finally {
			setPending(false);
		}
	};

	const submit = async () => {
		setPending(true);
		try {
			await onCreate(selected ?? undefined);
			setOpen(false);
		} finally {
			setPending(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={setOpen}>
			<DialogTrigger asChild>
				<Button id="tour-create-session" disabled={disabled}>
					<Plus />
					{t('chat.newSession')}
				</Button>
			</DialogTrigger>
			<DialogContent className="sm:max-w-lg">
				<DialogHeader>
					<DialogTitle>{t('workspaceProject.title')}</DialogTitle>
					<DialogDescription>{t('workspaceProject.description')}</DialogDescription>
				</DialogHeader>
				<div className="grid max-h-72 gap-2 overflow-y-auto py-1">
					<button
						type="button"
						onClick={() => setSelected(null)}
						className={cn(
							'flex items-start gap-3 rounded-md border p-3 text-left hover:bg-accent',
							selected === null && 'border-primary bg-accent',
						)}
					>
						<MessageSquare className="mt-0.5 size-4" />
						<span>
							<span className="block text-sm font-medium">{t('workspaceProject.temporary')}</span>
							<span className="block text-xs text-muted-foreground">{t('workspaceProject.temporaryHint')}</span>
						</span>
					</button>
					{projects.map((project) => (
						<button
							type="button"
							key={project.id}
							onClick={() => setSelected(project.workspace_id)}
							className={cn(
								'flex items-center gap-3 rounded-md border p-3 text-left hover:bg-accent',
								selected === project.workspace_id && 'border-primary bg-accent',
							)}
						>
							<FolderKanban className="size-4" />
							<span className="truncate text-sm font-medium">{project.name}</span>
						</button>
					))}
				</div>
				<div className="flex gap-2">
					<Input value={name} onChange={(event) => setName(event.target.value)} placeholder={t('workspaceProject.namePlaceholder')} />
					<Button type="button" variant="outline" onClick={createProject} disabled={pending || !name.trim()}>
						<Plus /> {t('workspaceProject.create')}
					</Button>
				</div>
				<DialogFooter>
					<Button onClick={submit} disabled={pending}>{t('workspaceProject.start')}</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
