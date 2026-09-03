/**
 * 大A/小A 叠加层 API：成员推荐 + 团队封档。
 *
 * 对应后端 app/leader_team.py 与 app/team_archive.py。
 */
import { client } from './client';

export interface MemberCandidate {
	id: string;
	name: string;
	description?: string;
}

export interface MemberRecommendation {
	id: string;
	name: string;
	description: string;
	reason: string;
}

export interface RecommendMembersResponse {
	recommendations: MemberRecommendation[];
	fallback: boolean;
	reason: string | null;
}

export interface NewAgentDraft {
	name: string;
	description: string;
	system_prompt: string;
}

export interface SummarizeResponse {
	summary: string;
	workflow_steps: string[];
	new_agents: NewAgentDraft[];
	fallback: boolean;
	reason: string | null;
}

export interface TeamArchive {
	id: string;
	name: string;
	summary: string;
	workflow_steps: string[];
	team_members: MemberCandidate[];
	new_agents: { id: string; name: string; description: string }[];
	created_at: string;
	source_agent_id: string | null;
	source_session_id: string | null;
}

export const leaderTeamApi = {
	recommendMembers: (body: {
		system_prompt?: string;
		task_topic?: string;
		members: MemberCandidate[];
	}) =>
		client.post<RecommendMembersResponse>(
			'/agent/recommend-members',
			body,
			undefined,
			{ silent: true },
		),
};

export const teamArchiveApi = {
	summarize: (agentId: string, sessionId: string) =>
		client.post<SummarizeResponse>(
			'/team-archive/summarize',
			{ agent_id: agentId, session_id: sessionId },
			undefined,
			{ silent: true },
		),
	create: (body: {
		name: string;
		summary: string;
		workflow_steps: string[];
		team_members: MemberCandidate[];
		new_agents: NewAgentDraft[];
		source_agent_id?: string | null;
		source_session_id?: string | null;
	}) =>
		client.post<TeamArchive>('/team-archive', body, undefined, { silent: true }),
	list: () => client.get<TeamArchive[]>('/team-archive'),
	get: (id: string) => client.get<TeamArchive>(`/team-archive/${id}`),
};
