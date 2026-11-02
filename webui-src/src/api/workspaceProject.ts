import { client } from './client';
import type { WorkspaceProject } from './types';

export const workspaceProjectApi = {
	list: () => client.get<WorkspaceProject[]>('/workspace-projects'),
	create: (name: string) =>
		client.post<WorkspaceProject>('/workspace-projects', { name }),
};
