import { client } from './client';
import type {
	AgentEvent,
	CreateSessionRequest,
	CreateSessionResponse,
	InterruptSessionResponse,
	SessionListResponse,
	SessionRecord,
	UpdateSessionRequest,
	Msg,
} from './types';

export interface MessagesResponse {
	messages: Msg[];
	is_running: boolean;
	has_more: boolean;
}

/**
 * Sessions this tab created and has not opened yet.
 *
 * A session created here cannot have any history, so asking the server
 * for it is a guaranteed-empty round trip — and one that paints a
 * loading state over a conversation the user is about to start.
 *
 * Entries are consumed on first read: once the session has been opened,
 * anything written to it afterwards (a scheduled run, a team member)
 * must be fetched normally.
 */
const freshlyCreated = new Set<string>();

/**
 * Whether `sessionId` was created by this tab and not yet opened.
 * Consumes the flag, so a second call for the same id returns false.
 *
 * @param sessionId - The session about to be opened.
 * @returns True when its history can safely be assumed empty.
 */
export function takeFreshlyCreated(sessionId: string): boolean {
	return freshlyCreated.delete(sessionId);
}

/**
 * Sessions this tab forked (workflow branch rerun) whose id the session
 * list has not re-fetched yet.
 *
 * The chat page redirects a URL session that is missing from the loaded
 * list back to the first list entry. A freshly forked branch is by
 * definition not in the stale list, so the redirect effect must let it
 * through until the refetch lands (2026-09-08: fork navigation was
 * silently rewritten back to the old first session, looking like
 * "nothing happened" after clicking 创建分支).
 */
const freshlyForked = new Set<string>();

/**
 * Whether `sessionId` is a branch this tab forked and the list has not
 * confirmed yet. Non-consuming: the redirect effect polls it on every
 * render until the list contains the session.
 */
export function isFreshlyForked(sessionId: string): boolean {
	return freshlyForked.has(sessionId);
}

/**
 * Drop the fork marker once the session list contains the branch (or the
 * user navigated away from it), so normal redirect semantics resume.
 */
export function clearFreshlyForked(sessionId: string): void {
	freshlyForked.delete(sessionId);
}

/** 团队历史：主理会话 → 历次团队成员 session 映射（含已解散）。 */
export interface TeamHistoryEntry {
        team_id: string;
        name: string;
        dissolved: boolean;
        members: {
                agent_id: string;
                agent_name: string;
                session_id: string | null;
                role: string;
        }[];
}

/** 会话流程操作结果（后端 app/session_flow.py FlowOpResponse）。 */
export interface FlowOpResponse {
	session_id: string;
	kept_messages: number;
	archived_messages: number;
	cancelled_members: number;
}

/** 一次截断归档（被删消息副本，后端 flow-archive 端点）。 */
export interface FlowArchiveEntry {
	truncated_at: string;
	from_message_id: string;
	removed_count: number;
	messages: Msg[];
}

/** 工作流节点级 fork 响应（2026-09-08 分支对比培育）。 */
export interface TeamForkResponse {
	session_id: string;
	parent_session_id: string;
	kept_messages: number;
	team_taken_over: boolean;
	/** 引导语是否已自动触发 chat run（initial_prompt 非空时）。 */
	auto_started?: boolean;
	/** 源会话无在册团队（已解散/记录缺失）——分支未接管团队，
	 * 主理人需重建团队才能分派成员任务。 */
	team_missing?: boolean;
}

export const sessionApi = {
     /** 主理会话的历次团队成员 session 映射（agent_id → 团队会话）。 */
     teamSessions: (leaderSessionId: string) =>
             client.get<{ teams: TeamHistoryEntry[] }>(
                     `/team-sessions/${leaderSessionId}`,
                     undefined,
                     { silent: true },
             ),
	list: (agentId: string) => client.get<SessionListResponse>('/sessions/', { agent_id: agentId }),

	/**
	 * 任意位置重新对话：归档并截断 message_id 之后的消息，上下文
	 * 同步截断（2026-09-08 v3）。
	 *
	 * Backend contract:
	 * - 200 → `FlowOpResponse`（kept/archived 数量）
	 * - 404 → 会话或消息不存在
	 * - 409 → 会话运行中（先暂停再截断）
	 */
	truncate: (
		sessionId: string,
		agentId: string,
		messageId: string,
	) =>
		client.post<FlowOpResponse>(`/sessions/${sessionId}/truncate`, {
			agent_id: agentId,
			message_id: messageId,
		}),

	/**
	 * 工作流节点级 fork：保留当前会话不动，新建分支会话（消息
	 * 截断到锚点、context 同步、共享工作区与团队），团队调度权
	 * 移交新分支（2026-09-08 分支对比培育）。可选 initial_prompt：
	 * 引导语，fork 完成后作为新分支第一条用户消息自动触发重跑。
	 *
	 * Backend contract:
	 * - 201 → `TeamForkResponse`（新分支 session_id 等）
	 * - 404 → 会话或消息不存在
	 * - 409 → 会话运行中（先暂停再 fork）
	 */
	teamFork: async (
		sessionId: string,
		agentId: string,
		messageId: string,
		initialPrompt?: string,
	) => {
		const res = await client.post<TeamForkResponse>(
			`/sessions/${sessionId}/team-fork`,
			{
				agent_id: agentId,
				message_id: messageId,
				initial_prompt: initialPrompt || undefined,
			},
		);
		// 登记新分支：列表 refetch 落地前，chat 页重定向 effect 放行该 id
		freshlyForked.add(res.session_id);
		return res;
	},

	/**
	 * 流程重启：上下文/摘要/回复状态归零，消息历史保留；
	 * 团队 leader 重启时后端先取消全部成员运行。
	 */
	restart: (sessionId: string, agentId: string) =>
		client.post<FlowOpResponse>(`/sessions/${sessionId}/restart`, {
			agent_id: agentId,
		}),

	/**
	 * 团队暂停：中断 leader（官方 interrupt 三态幂等）+ 取消全部
	 * 成员运行，上下文完整保留。
	 */
	pauseTeamFlow: (leaderSessionId: string, agentId: string) =>
		client.post<FlowOpResponse>(
			`/team-flow/${leaderSessionId}/pause`,
			null,
			{ agent_id: agentId },
		),

	/**
	 * 继续：对会话 enqueue 一个 wake 触发（官方 `input: None` 语义，
	 * 从当前状态继续推理）。团队 leader 被唤醒后自行恢复调度。
	 */
	resumeFlow: (sessionId: string, agentId: string) =>
		client.post<FlowOpResponse>(`/team-flow/${sessionId}/resume`, null, {
			agent_id: agentId,
		}),

	/**
	 * 解散团队（用户主动，软解散语义）：LLM 的 TeamDelete 已被无
	 * 条件 DENY——这是唯一解散入口。取消成员运行、解除绑定，全部
	 * 培养资产（成员 agent/会话/产出）保留。
	 */
	dissolveTeamFlow: (leaderSessionId: string, agentId: string) =>
		client.post<FlowOpResponse>(
			`/team-flow/${leaderSessionId}/dissolve`,
			null,
			{ agent_id: agentId },
		),

	/** 查询该会话的截断归档（被删消息副本，历次列表）。 */
	flowArchive: (sessionId: string, agentId: string) =>
		client.get<{ session_id: string; archives: FlowArchiveEntry[] }>(
			`/sessions/${sessionId}/flow-archive`,
			{ agent_id: agentId },
		),

	create: async (body: CreateSessionRequest) => {
		const res = await client.post<CreateSessionResponse>('/sessions/', body);
		freshlyCreated.add(res.session_id);
		return res;
	},

	/**
	 * Update a session's configuration.
	 *
	 * Returns 409 while a chat run holds the session — the agent
	 * snapshots its configuration at run start, so the change could not
	 * apply to the reply in flight. Pass `silent` for automatic writes
	 * the user did not initiate, where a toast would be noise.
	 */
	update: (
		sessionId: string,
		agentId: string,
		body: UpdateSessionRequest,
		options?: { silent?: boolean },
	) =>
		client.patch<SessionRecord>(`/sessions/${sessionId}`, body, { agent_id: agentId }, options),

	delete: (sessionId: string, agentId: string) =>
		client.delete(`/sessions/${sessionId}`, { agent_id: agentId }),

	/**
	 * Request interruption of an in-progress reply (running or parked).
	 *
	 * Backend contract:
	 * - 202 Accepted → returns `InterruptSessionResponse`; the cancel
	 *   signal was broadcast (running) or a wakeup-interrupt was
	 *   enqueued (parked). Idempotent: an idle target is a silent
	 *   no-op at the agent layer.
	 * - 404 Not Found → the session does not exist.
	 */
	interrupt: (sessionId: string, agentId: string) =>
		client.post<InterruptSessionResponse>(`/sessions/${sessionId}/interrupt`, null, {
			agent_id: agentId,
		}),

	messages: (sessionId: string, agentId: string, params?: { before?: string; limit?: number }) =>
		client.get<MessagesResponse>(`/sessions/${sessionId}/messages`, {
			agent_id: agentId,
			...(params?.before != null && { before: params.before }),
			...(params?.limit != null && { limit: String(params.limit) }),
		}),

	/**
	 * Subscribe to a session's live event stream via SSE.
	 *
	 * Opens a long-lived ``GET /sessions/{sid}/stream`` connection and
	 * yields each ``AgentEvent`` as it arrives. The connection stays
	 * open until the caller aborts via the ``signal`` or closes the
	 * generator.
	 *
	 * Uses fetch-based SSE (not native ``EventSource``) so the
	 * ``X-User-ID`` custom header is sent.
	 *
	 * @param sessionId - The session to subscribe to.
	 * @param agentId - The agent that owns the session.
	 * @param signal - Optional abort signal to close the connection.
	 * @returns An async generator yielding ``AgentEvent`` objects.
	 */
	streamEvents: async function* (
		sessionId: string,
		agentId: string,
		signal?: AbortSignal,
	): AsyncGenerator<AgentEvent> {
		const res = await client.stream(`/sessions/${sessionId}/stream`, {
			method: 'GET',
			params: { agent_id: agentId },
			signal,
		});

		const reader = res.body!.getReader();
		const decoder = new TextDecoder();
		let buffer = '';

		try {
			while (true) {
				const { done, value } = await reader.read();
				if (done) break;

				buffer += decoder.decode(value, { stream: true });
				const lines = buffer.split('\n');
				buffer = lines.pop() ?? '';

				for (const line of lines) {
					if (line.startsWith('data: ')) {
						const json = line.slice(6).trim();
						if (json) yield JSON.parse(json) as AgentEvent;
					}
					// SSE comment frames (`:...\n`) are silently skipped
					// (used for heartbeats).
				}
			}
		} finally {
			reader.releaseLock();
		}
	},
};
