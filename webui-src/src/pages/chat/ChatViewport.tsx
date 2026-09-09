import {
	Archive,
	ArrowLeft,
	BookText,
	ChevronDown,
	Database,
	PanelRight,
	PanelRightClose,
	RotateCw,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

import type {
	ChatModelConfig,
     TeamHistoryEntry,
	PermissionMode,
	SessionKnowledgeConfig,
	TTSModelConfig,
	UpdateSessionRequest,
} from '@/api';
import { sessionApi } from '@/api';
import MCPSvg from '@/assets/images/mcp.svg?react';
import { ChatContent } from '@/components/chat/ChatContent.tsx';
import { SubagentHitlCard } from '@/components/chat/SubagentHitlCard';
import { ArchiveDialog } from '@/components/dialog/ArchiveDialog';
import { CreateCredentialDialog } from '@/components/dialog/CreateCredentialDialog';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { KnowledgeBasePanel } from '@/components/panel/KnowledgeBasePanel';
import { McpPanel } from '@/components/panel/McpPanel';
import { PanelDock, type PanelDescriptor, type PanelKey } from '@/components/panel/PanelDock.tsx';
import { ResourceTabsPanel } from '@/components/panel/ResourceTabsPanel';
import { SkillPanel } from '@/components/panel/SkillPanel';
import { TeamFlowPanel } from '@/components/panel/TeamFlowPanel';
import { KnowledgeBaseParametersPopover } from '@/components/popover/KnowledgeBaseParametersPopover';
import { ModelParametersPopover } from '@/components/popover/ModelParametersPopover';
import { LlmSelect } from '@/components/select/LlmSelect';
import { PermissionModeSelect } from '@/components/select/PermissionModeSelect.tsx';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuCheckboxItem,
	DropdownMenuContent,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
	ResizableHandle,
	ResizablePanel,
	ResizablePanelGroup,
} from '@/components/ui/resizable.tsx';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { useAgents } from '@/hooks/useAgents';
import { useAvailableModels } from '@/hooks/useAvailableModels';
import { useKnowledgeBaseMiddlewareSchema } from '@/hooks/useKnowledgeBaseMiddlewareSchema';
import { useKnowledgeBases } from '@/hooks/useKnowledgeBases';
import { useMessages } from '@/hooks/useMessages';
import { useSessions } from '@/hooks/useSessions';
import { useWorkspace } from '@/hooks/useWorkspace.ts';
import { useWorkspaceStatus } from '@/hooks/useWorkspaceStatus';
import { useTranslation } from '@/i18n/useI18n';
import { uuid } from '@/utils/uuid';

interface ChatViewportProps {
	/**
	 * The agent that owns the session being viewed. May be the
	 * user-facing leader agent or — when drilled into a team member
	 * via the URL's `:memberId` slot — a worker agent.
	 */
	agentId: string | null;
	/**
	 * The session whose messages, model config, permission mode, and
	 * workspace drive every control rendered here.
	 */
	sessionId: string | null;
	/**
	 * Leader navigation target — non-null while the viewport is drilled
	 * into a team member via the URL's ``:memberId`` slot. Renders a
	 * prominent "back to leader" button so the user can always return
	 * to the leader's session (2026-09-08 用户反馈：进入成员迭代后无法返回).
	 */
	leaderNav?: { agentId: string; sessionId: string } | null;
	/**
	 * Optional hook invoked when a team membership change arrives on
	 * this viewport's SSE stream. The outer page owns the session list
	 * that backs the team sidebar, so it must be told to refetch too;
	 * passing this callback wires that signal up.
	 */
	onTeamUpdated?: () => void;
}

/** Maximum number of panels stacked in a single dock column. */
const MAX_PANELS_PER_COLUMN = 2;

/** localStorage key holding the dock layout across page navigations. */
const PANEL_LAYOUT_KEY = 'chat_panel_layout';

/**
 * localStorage key for the chat layout mode (2026-09-07 用户布局重构)：
 * - focused（默认）：完整对话在中间；右侧栏上=团队工作流驾驶舱
 *   （TeamFlowPanel），下=资源面板（MCP/技能/知识库 Tab）
 * - classic：旧布局——TeamFlowPanel 在对话区顶部，右侧 dock 面板
 *   由右上角菜单开关
 */
const LAYOUT_MODE_KEY = 'chat_layout_mode';

type LayoutMode = 'focused' | 'classic';

function loadLayoutMode(): LayoutMode {
	return localStorage.getItem(LAYOUT_MODE_KEY) === 'classic' ? 'classic' : 'focused';
}

// Typed as a full Record so adding a PanelKey without listing it here
// is a compile error rather than a silently unrestorable panel.
const KNOWN_PANELS: Record<PanelKey, true> = {
	mcp: true,
	skill: true,
	knowledge: true,
};

/**
 * Restore the persisted dock layout, dropping anything that is no
 * longer a known panel (keys get renamed/removed across releases).
 *
 * @returns The stored layout, or an empty one when absent or corrupt.
 */
function loadPanelLayout(): PanelKey[][] {
	try {
		const parsed: unknown = JSON.parse(localStorage.getItem(PANEL_LAYOUT_KEY) ?? '[]');
		if (!Array.isArray(parsed)) return [];
		return parsed
			.map((column: unknown) =>
				Array.isArray(column)
					? column.filter((key): key is PanelKey => key in KNOWN_PANELS)
					: [],
			)
			.filter((column) => column.length > 0);
	} catch {
		return [];
	}
}

/**
 * Insert a panel into the dock layout. Scans columns left to right and
 * appends to the first one with spare room; if every column is full a
 * new rightmost column is created. No-op when the panel is already
 * open.
 *
 * @param layout - The current column/panel arrangement.
 * @param key - The panel to open.
 * @returns A new layout array (the input is never mutated).
 */
function openPanelInLayout(layout: PanelKey[][], key: PanelKey): PanelKey[][] {
	if (layout.some((column) => column.includes(key))) return layout;
	const targetIndex = layout.findIndex((column) => column.length < MAX_PANELS_PER_COLUMN);
	if (targetIndex === -1) return [...layout, [key]];
	return layout.map((column, index) => (index === targetIndex ? [...column, key] : column));
}

/**
 * Remove a panel from the dock layout, dropping its column entirely if
 * it becomes empty.
 *
 * @param layout - The current column/panel arrangement.
 * @param key - The panel to close.
 * @returns A new layout array (the input is never mutated).
 */
function closePanelInLayout(layout: PanelKey[][], key: PanelKey): PanelKey[][] {
	return layout
		.map((column) => column.filter((panelKey) => panelKey !== key))
		.filter((column) => column.length > 0);
}

/**
 * The right-hand main panel of the chat page — every UI element that
 * operates on a single `(agentId, sessionId)` pair lives here:
 * model selector, permission mode select, message stream, workspace
 * drawer, and the team sidebar.
 *
 * Self-contained by design. The outer page passes in the
 * `(agentId, sessionId)` it wants displayed (which may be the leader
 * session or a focused team member's session) and this component
 * does the rest — fetching the session view, syncing local UI state
 * with it, and writing changes back to the same session. Switching
 * between leader and member is just a prop change; no internal
 * branching is needed.
 *
 * @param agentId - The agent to operate on. `null` while no agent is
 *   selected yet (renders an empty / disabled state).
 * @param sessionId - The session to operate on. `null` while no
 *   session is selected yet.
 * @returns The right-side main JSX of the chat page.
 */
export function ChatViewport({
	agentId,
	sessionId,
	leaderNav,
	onTeamUpdated,
}: ChatViewportProps) {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const { sessions, refetch: refetchSessions } = useSessions(agentId);
	const { groups } = useAvailableModels();

	const [selectedModel, setSelectedModel] = useState<ChatModelConfig | null>(null);
	const [selectedFallbackModel, setSelectedFallbackModel] = useState<ChatModelConfig | null>(
		null,
	);
	const [selectedTTSModel, setSelectedTTSModel] = useState<TTSModelConfig | null>(null);
	const [selectedKnowledgeConfig, setSelectedKnowledgeConfig] =
		useState<SessionKnowledgeConfig | null>(null);
	const [selectedPermissionMode, setSelectedPermissionMode] = useState<string>('default');
	const [credentialOpen, setCredentialOpen] = useState(false);
	const [credentialRefetchTrigger, setCredentialRefetchTrigger] = useState(0);
	const [configPending, setConfigPending] = useState(false);
	// 任务归档对话框（仅主理人会话显示入口）
	const [archiveOpen, setArchiveOpen] = useState(false);
	// Dock layout: columns laid out left→right, each holding up to 2
	// panels stacked top→bottom. Open order determines placement.
	// Persisted so leaving and returning to /chat keeps the same panels.
	const [panelLayout, setPanelLayout] = useState<PanelKey[][]>(loadPanelLayout);
	// 布局模式（focused/classic），持久化，默认 focused
	const [layoutMode, setLayoutMode] = useState<LayoutMode>(loadLayoutMode);

	useEffect(() => {
		localStorage.setItem(PANEL_LAYOUT_KEY, JSON.stringify(panelLayout));
	}, [panelLayout]);

	useEffect(() => {
		localStorage.setItem(LAYOUT_MODE_KEY, layoutMode);
	}, [layoutMode]);

	// When the viewport agent differs from the outer page's selected
	// agent (i.e. user drilled into a team member), `refetchSessions`
	// only refreshes the member's session list, so we also fire the
	// parent's refetch to keep its copy in sync.
	//
	// Surfacing the team panel here is what makes a team visible at all
	// — `TeamCreate` / `AgentCreate` / `AgentInvite` are agent tools, so
	// the user never opened a dialog that could have opened the panel.
	// `team_updated` also fires on `TeamDelete` and carries no payload,
	// hence checking the refetched list rather than opening blindly.
	const handleTeamUpdated = useCallback(async () => {
		// 团队事件（TeamCreate/AgentCreate/…）到达时刷新会话视图；
		// 团队展示由 TeamFlowPanel 从消息流自解析，不再自动开 dock 面板。
		await refetchSessions();
		onTeamUpdated?.();
	}, [refetchSessions, onTeamUpdated]);

	const {
		msgs,
		loading: messagesLoading,
		phase,
		send,
		onUserConfirm,
		onSubagentConfirm,
		subagentHitl,
		interrupt,
		truncateAt,
		restartFlow,
	} = useMessages(agentId, sessionId, {
		onTeamUpdated: handleTeamUpdated,
	});
	const {
		mcps,
		loading: mcpsLoading,
		addMcps,
		addMcpsFromLibrary,
		removeMcp,
		skills,
		skillsLoading,
		uploadSkill,
		addSkillsFromLibrary,
		removeSkill,
	} = useWorkspace(agentId, sessionId);
	const { knowledgeBases, loading: knowledgeBasesLoading } = useKnowledgeBases();
	const { schema: kbMiddlewareSchema } = useKnowledgeBaseMiddlewareSchema();

	// Toggle a panel open/closed from the top-bar buttons.
	const togglePanel = useCallback((key: PanelKey) => {
		setPanelLayout((layout) =>
			layout.some((column) => column.includes(key))
				? closePanelInLayout(layout, key)
				: openPanelInLayout(layout, key),
		);
	}, []);

	// Close a panel (driven by the panel's own close button).
	const closePanel = useCallback((key: PanelKey) => {
		setPanelLayout((layout) => closePanelInLayout(layout, key));
	}, []);

	const isPanelOpen = useCallback(
		(key: PanelKey) => panelLayout.some((column) => column.includes(key)),
		[panelLayout],
	);

	/**
	 * Persist a knowledge-base attachment change. `null` detaches every
	 * knowledge base from this session, removing the `RAGMiddleware`.
	 *
	 * Declared above `panels` (rather than alongside the other model
	 * handlers below) because `panels` is built inside `useMemo` and
	 * references this handler eagerly — a later `const` would still be
	 * in the temporal dead zone when the memo factory runs on first
	 * render.
	 *
	 * @param config - New attachment, or `null` to detach all.
	 */
	/**
	 * Persist a session config change, applying it locally only once
	 * the server accepts it.
	 *
	 * The backend rejects config writes with 409 while a chat run holds
	 * the session, so an optimistic update would leave the control
	 * showing a value the session does not have. Waiting for the
	 * response keeps the control on its previous value with no rollback
	 * bookkeeping; `client.ts` has already surfaced the error toast by
	 * the time we land in `catch`.
	 *
	 * Declared above `panels` for the same temporal-dead-zone reason as
	 * `handleKnowledgeConfigChange` below.
	 *
	 * @param body - The PATCH body.
	 * @param apply - Mirrors the change into local state on success.
	 */
	const patchConfig = useCallback(
		async (body: UpdateSessionRequest, apply: () => void) => {
			if (!sessionId || !agentId) return;
			setConfigPending(true);
			try {
				await sessionApi.update(sessionId, agentId, body);
				apply();
				await refetchSessions();
			} catch {
				// Toast already shown; local state deliberately untouched.
			} finally {
				setConfigPending(false);
			}
		},
		[sessionId, agentId, refetchSessions],
	);

	const handleKnowledgeConfigChange = useCallback(
		async (config: SessionKnowledgeConfig | null) => {
			await patchConfig({ knowledge_config: config }, () =>
				setSelectedKnowledgeConfig(config),
			);
		},
		[patchConfig],
	);

	// Declared above `panels` — the memo factory reads `view.team`
	// eagerly on first render, so a later `const` would still be in
	// the temporal dead zone.
	const view = sessions.find((v) => v.session.id === sessionId) ?? null;

	// 当前 agent 的记录：判定主理人（大A）/成员（小A）并取显示名。
	// `useAgents` 在此独立拉取全量列表，与外层页面的实例互不影响。
	const { agents } = useAgents();
	const agentRecord = useMemo(
		() => agents.find((a) => a.id === agentId) ?? null,
		[agents, agentId],
	);
	const isLeader = agentRecord?.agent_type === 'leader';
	const leaderName = agentRecord?.data.name ?? '主理人';

	// 归档对话框的成员列表：优先取当前团队的在册成员，回退到主理人
	// 创建时预置的 team_members（用 agents 列表补齐名称）。
	const archiveMembers = useMemo(() => {
		if (view?.team?.members?.length) {
			return view.team.members.map((m) => ({
				id: m.agent.id,
				name: m.agent.data.name,
			}));
		}
		return (agentRecord?.team_members ?? []).map((id) => ({
			id,
			name: agents.find((a) => a.id === id)?.data.name ?? id.slice(0, 8),
		}));
	}, [view, agentRecord, agents]);

	// 团队历史：无活跃团队时（已解散/新会话），从后端补成员在历次
	// 团队任务中的 session_id——"进入会话迭代"要跳到成员的团队会话
	// （有任务上下文，可继续对话介入培养），而不是空白独立会话。
	const [teamHistory, setTeamHistory] = useState<TeamHistoryEntry[]>([]);
	useEffect(() => {
		if (!sessionId) return;
		let cancelled = false;
		sessionApi
			.teamSessions(sessionId)
			.then((res) => {
				if (!cancelled) setTeamHistory(res.teams ?? []);
			})
			.catch(() => {
				// 无团队历史（普通会话）静默
			});
		return () => {
			cancelled = true;
		};
	}, [sessionId]); // 建队/解散由消息流事件触发 refetchSessions → view 变化重渲染

	// 团队实时状态轮询（2026-09-09 用户反馈"任务结束了还显示运行中"）：
	// 在册团队每 4s 拉取主理人+成员运行锁快照，驱动 Badge 区分
	// 「运行中」（任一持锁）与「休息中」（全部空闲）。失败保留上次
	// 快照（不闪烁）；团队不在册/切换会话时停轮询并清空。
	useEffect(() => {
		if (!sessionId || !agentId || !view?.team) {
			setTeamLive(null);
			return;
		}
		let alive = true;
		const load = async () => {
			try {
				const r = await sessionApi.teamLiveStatus(sessionId, agentId);
				if (alive) {
					setTeamLive({
						leaderRunning: r.leader_running,
						members: r.members.map((m) => ({
							agent_id: m.agent_id,
							session_id: m.session_id,
							running: m.running,
						})),
					});
				}
			} catch {
				// 轮询失败静默：保留上次快照
			}
		};
		void load();
		const timer = setInterval(load, 4000);
		return () => {
			alive = false;
			clearInterval(timer);
		};
	}, [sessionId, agentId, view?.team]);


	// 成员职责说明：邀请场景读 invite_description；主理人创建的成员
	// 没有该字段，职责在官方生成的 system_prompt "Your role: ..." 段
	// （2026-09-07 团队实测：创建成员职责全空白）
	const memberRole = useCallback(
		(data: { invite_config?: { invite_description?: string | null } | null; system_prompt?: string | null }): string => {
			const invited = data.invite_config?.invite_description;
			if (invited) return invited;
			const m = /Your role:\s*([^\n]+)/.exec(data.system_prompt ?? '');
			return m ? m[1].trim() : '';
		},
		[],
	);

	// 流程图的成员档案：在册成员带职责（邀请说明）与会话 id，
	// 点击成员卡片可跳到该小A 的会话单独迭代优化。
	// 无活跃团队时（新会话尚未组队、或已解散）回退到主理人
	// 注册时预置的成员名单，保证"创建时选的人"始终可见。
	const flowMembers = useMemo(() => {
		if (view?.team?.members?.length) {
			return view.team.members.map((m) => ({
				id: m.agent.id,
				name: m.agent.data.name,
				description: memberRole(m.agent.data),
				sessionId: m.session_id,
			}));
		}
		const historyByAgent = new Map<string, string>();
		for (const team of teamHistory) {
			for (const m of team.members) {
				if (m.session_id) historyByAgent.set(m.agent_id, m.session_id);
			}
		}
		return (agentRecord?.team_members ?? [])
			.map((id) => {
				const a = agents.find((x) => x.id === id);
				return a
					? {
						id: a.id,
						name: a.data.name,
						description: memberRole(a.data),
						sessionId: historyByAgent.get(a.id) ?? null,
					}
					: null;
			})
			.filter((m): m is NonNullable<typeof m> => m !== null);
	}, [view, agentRecord, agents, teamHistory, memberRole]);

	// 成员跳转：走 /chat/<leaderSessionId>/<memberAgentId>（memberId 槽），
	// 与 TeamPanel 的导航约定一致；成员无会话时退回 /chat/<agentId>。
	const handleOpenFlowMember = useCallback(
		(m: { id: string; sessionId?: string | null }) => {
			if (m.sessionId && sessionId) {
				navigate(`/chat/${agentId}/${sessionId}/${m.id}`);
			} else {
				navigate(`/chat/${m.id}`);
			}
		},
		[navigate, agentId, sessionId],
	);

	const { status: workspaceStatus, refetch: refetchWorkspaceStatus } = useWorkspaceStatus(
		agentId,
		sessionId,
		view?.session.config.cwd ?? null,
	);

	// A finished reply is the one moment the agent may have changed the
	// working tree, and it is why nothing polls for git status. Watching
	// `phase` rather than the REPLY_END event also covers the interrupt
	// timeout, which reaches idle without one.
	const prevPhaseRef = useRef(phase);
	useEffect(() => {
		const wasRunning = prevPhaseRef.current !== 'idle';
		prevPhaseRef.current = phase;
		if (wasRunning && phase === 'idle') void refetchWorkspaceStatus();
	}, [phase, refetchWorkspaceStatus]);

	// 三类资源面板的内容节点：经典布局的 dock 与专注布局的右侧
	// 资源面板共用（JSX 只定义一次，数据变化时同步重建）。
	const resourceContent = useMemo(
		() => ({
			mcp: (
				<McpPanel
					mcps={mcps}
					loading={mcpsLoading}
					onAdd={addMcps}
					onAddFromLibrary={addMcpsFromLibrary}
					onRemove={removeMcp}
				/>
			),
			skill: (
				<SkillPanel
					skills={skills}
					loading={skillsLoading}
					onUpload={uploadSkill}
					onAddFromLibrary={addSkillsFromLibrary}
					onRemove={removeSkill}
				/>
			),
			knowledge: (
				<KnowledgeBasePanel
					knowledgeBases={knowledgeBases}
					loading={knowledgeBasesLoading}
					value={selectedKnowledgeConfig}
					onChange={handleKnowledgeConfigChange}
					disabled={!sessionId}
				/>
			),
		}),
		[
			mcps,
			mcpsLoading,
			addMcps,
			addMcpsFromLibrary,
			removeMcp,
			skills,
			skillsLoading,
			uploadSkill,
			addSkillsFromLibrary,
			removeSkill,
			knowledgeBases,
			knowledgeBasesLoading,
			selectedKnowledgeConfig,
			handleKnowledgeConfigChange,
			sessionId,
		],
	);

	// 知识库参数按钮：dock 面板头与资源面板 Tab 栏共用
	const knowledgeActions = (
		<KnowledgeBaseParametersPopover
			value={selectedKnowledgeConfig}
			schema={kbMiddlewareSchema}
			onChange={handleKnowledgeConfigChange}
			disabled={!sessionId}
		/>
	);

	// Build the panel descriptors with live data. Rebuilt on every
	// data change so the dock always renders the latest state — the
	// dock itself stays free of any data dependency.
	const panels = useMemo<Record<PanelKey, PanelDescriptor>>(
		() => ({
			mcp: {
				title: 'MCP',
				icon: <MCPSvg className="size-4" />,
				content: resourceContent.mcp,
			},
			skill: {
				title: t('panel.skill.title'),
				icon: <BookText className="size-4" />,
				content: resourceContent.skill,
			},
			knowledge: {
				title: (
					<span className="flex items-center gap-x-2">
						{t('panel.knowledge.title')}
						{selectedKnowledgeConfig?.knowledge_base_ids.length ? (
							<Badge variant="outline">
								{selectedKnowledgeConfig.knowledge_base_ids.length}
							</Badge>
						) : null}
					</span>
				),
				icon: <Database className="size-4" />,
				actions: knowledgeActions,
				content: resourceContent.knowledge,
			},
		}),
		[t, resourceContent, knowledgeActions],
	);

	// ChatViewport keeps its own `useSessions(agentId)` instance (the
	// outer page has a separate one). Its built-in fetch only fires on
	// `agentId` change, so when the outer page creates a new session
	// under the same agent, this list doesn't auto-refresh. Without
	// this refetch, `view` would stay `null` for the brand-new session
	// id and every effect below would early-return on `!view`,
	// leaving the model select and friends pinned to whatever the
	// previously-viewed session had configured.
	useEffect(() => {
		if (!sessionId) return;
		if (view) return;
		refetchSessions();
	}, [sessionId, view, refetchSessions]);

	// Reset local UI state when the target session changes. Otherwise
	// the model select (and disabled-state guards on `send`) would
	// show the previous session's model during the in-flight window
	// before `view` repopulates — and an immediate send would post to
	// a session whose backend config doesn't actually have that model.
	useEffect(() => {
		setSelectedModel(null);
		setSelectedFallbackModel(null);
		setSelectedTTSModel(null);
		setSelectedKnowledgeConfig(null);
	}, [sessionId]);

	const selectedModelCard = useMemo(() => {
		if (!selectedModel) return null;
		const items = groups[selectedModel.type];
		if (!items) return null;
		for (const { models } of items) {
			const card = models.find((m) => m.name === selectedModel.model);
			if (card) return card;
		}
		return null;
	}, [groups, selectedModel?.type, selectedModel?.model]);

	/**
	 * Pick the first model the available-models endpoint surfaces, used
	 * as a sensible default when the current session has no model
	 * configured yet.
	 *
	 * @returns The first available `ChatModelConfig`, or `null` when
	 *   no credentials / models are configured.
	 */
	const getFirstAvailableModel = (): ChatModelConfig | null => {
		const firstType = Object.keys(groups)[0];
		if (!firstType) return null;
		const items = groups[firstType];
		if (!items || items.length === 0) return null;
		const firstItem = items[0];
		const firstModel = (firstItem.models as { name?: string; id?: string }[])[0];
		if (!firstModel) return null;
		const modelName = firstModel.name ?? firstModel.id ?? null;
		if (!modelName) return null;
		return {
			type: firstType,
			credential_id: firstItem.credential.id,
			model: modelName,
			parameters: {},
		};
	};

	// Seed tasks + permission from the session snapshot ONCE per
	// Sync selectedModel + selectedFallbackModel from the session
	// record. If the session has no model configured yet, auto-pick
	// the first available one and persist it back so subsequent
	// reasoning has a model to call.
	//
	// Important: skip while `view` is still loading. Otherwise the
	// in-flight window between "agentId changed" and "useSessions
	// returned the new list" looks like "session has no model" and
	// we would racily auto-select + persist the first available
	// model, clobbering whatever the user had configured.
	useEffect(() => {
		if (!view) return;
		const sessionModel = view.session.config.chat_model_config;

		if (sessionModel) {
			setSelectedModel(sessionModel);
		} else {
			const firstModel = getFirstAvailableModel();
			if (firstModel) {
				setSelectedModel(firstModel);
				if (sessionId && agentId) {
					// `silent` because the user did not ask for this write —
					// surfacing a toast for a revoked credential or a network
					// blip they never triggered is pure noise.
					sessionApi
						.update(
							sessionId,
							agentId,
							{ chat_model_config: firstModel },
							{ silent: true },
						)
						.then(() => refetchSessions())
						.catch(() => {});
				}
			} else {
				setSelectedModel(null);
			}
		}

		setSelectedFallbackModel(view.session.config.fallback_chat_model_config ?? null);
		setSelectedTTSModel(view.session.config.tts_model_config ?? null);
		setSelectedKnowledgeConfig(view.session.config.knowledge_config ?? null);
	}, [view, groups, sessionId, agentId]);

	// Sync selectedPermissionMode when the session changes. Same
	// loading-window guard as above — don't reset the displayed mode
	// to "default" while the new session view is still on the wire.
	useEffect(() => {
		if (!view) return;
		const mode = (view.session.state?.permission_context as Record<string, unknown>)
			?.mode as string;
		setSelectedPermissionMode(mode ?? 'default');
	}, [sessionId, view]);

	/**
	 * Persist a model change to the session and refetch so the local
	 * view picks up the new value.
	 *
	 * @param config - New chat model config; `null` is ignored
	 *   because the primary selector does not allow clearing.
	 */
	const handleLlmChange = async (config: ChatModelConfig | null) => {
		if (!config) return;
		await patchConfig({ chat_model_config: config }, () => setSelectedModel(config));
	};

	/**
	 * Persist a parameter change on the currently selected model.
	 *
	 * @param parameters - New parameter map (model-provider specific).
	 */
	const handleParametersChange = async (parameters: Record<string, unknown>) => {
		if (!selectedModel) return;
		const updated = { ...selectedModel, parameters };
		await patchConfig({ chat_model_config: updated }, () => setSelectedModel(updated));
	};

	/**
	 * Persist a fallback-model change. `null` clears the fallback.
	 *
	 * @param config - New fallback config or `null` to clear.
	 */
	const handleFallbackChange = async (config: ChatModelConfig | null) => {
		await patchConfig({ fallback_chat_model_config: config }, () =>
			setSelectedFallbackModel(config),
		);
	};

	/**
	 * Persist a TTS model change. `null` disables TTS.
	 *
	 * @param config - New TTS config or `null` to disable.
	 */
	const handleTTSChange = async (config: TTSModelConfig | null) => {
		await patchConfig({ tts_model_config: config }, () => setSelectedTTSModel(config));
	};

	/**
	 * Persist a permission-mode change.
	 *
	 * @param mode - New permission mode (e.g. `default`, `explore`).
	 */
	/**
	 * Persist a new working directory.
	 *
	 * Nothing local mirrors it — the value is read straight off the
	 * session view, which `patchConfig` refetches on success.
	 *
	 * @param next - Directory relative to the workspace root, or `null`
	 *   for the root itself.
	 */
	const handleCwdChange = async (next: string | null) => {
		// Bypasses `patchConfig`: the dialog shows the failure inline and
		// stays open on it, so the toast would be a duplicate and the
		// swallowed rejection would let the dialog close as if it worked.
		if (!sessionId || !agentId) return;
		setConfigPending(true);
		try {
			await sessionApi.update(sessionId, agentId, { cwd: next }, { silent: true });
			await refetchSessions();
		} finally {
			setConfigPending(false);
		}
	};

	const handlePermissionModeChange = async (mode: string) => {
		await patchConfig({ permission_mode: mode as PermissionMode }, () =>
			setSelectedPermissionMode(mode),
		);
	};

	// ── 会话流程控制（2026-09-08 v3）────────────────────────────
	// 暂停/继续 · 任意位置重新对话 · 流程重启

	/** 重启确认对话框 */
	const [restartOpen, setRestartOpen] = useState(false);
    /** 解散团队确认对话框（用户主动解散的唯一入口）。 */
    const [dissolveOpen, setDissolveOpen] = useState(false);
    /** 团队实时运行快照（2026-09-09：区分「运行中」与「休息中」）。
     *  在册团队时每 4s 轮询 live-status（主理人+成员运行锁）。 */
    const [teamLive, setTeamLive] = useState<{
        leaderRunning: boolean;
        members: { agent_id: string; session_id: string; running: boolean }[];
    } | null>(null);
	/** 截断（从这里重开）确认：待截断的锚点消息 id */
	const [truncateTarget, setTruncateTarget] = useState<string | null>(null);

		/** 节点级 fork 目标（2026-09-08 分支对比培育）。 */
		const [forkTarget, setForkTarget] = useState<{
			msgId: string;
			name: string;
			prompt?: string;
		} | null>(null);
	/** 流程操作进行中（防重复点击） */
	const [flowPending, setFlowPending] = useState(false);

	/** 任意位置重新对话：点消息旁的分叉按钮 → 确认 → 截断。 */
	const handleTruncateAt = useCallback(
		(messageId: string) => setTruncateTarget(messageId),
		[],
	);

	const handleTruncateConfirm = useCallback(async () => {
		if (!truncateTarget) return;
		setFlowPending(true);
		try {
			const res = await truncateAt(truncateTarget);
			if (res) {
				toast.success(
					t('chat.truncateDone', {
						kept: res.kept,
						archived: res.archived,
					}),
				);
				setTruncateTarget(null);
			}
		} finally {
			setFlowPending(false);
		}
	}, [truncateTarget, truncateAt, t]);

		/** 节点级 fork 确认：调后端新建分支（引导语由后端作为新分支
			 *  第一条用户消息自动触发重跑）→ 跳转新分支。
			 *  源会话无在册团队时（已解散）分支退化为普通会话——
			 *  明确警告（2026-09-08 用户困惑："为什么要重新组建团队"）。 */
			const handleForkConfirm = useCallback(async () => {
					if (!forkTarget || !agentId || !sessionId) return;
					setFlowPending(true);
					try {
							const res = await sessionApi.teamFork(
									sessionId,
									agentId,
									forkTarget.msgId,
									forkTarget.prompt,
							);
							if (res) {
									toast.success(
											t('chat.forkDone', { name: forkTarget.name }),
									);
									if (res.auto_started) {
											toast.success(t('chat.forkAutoStarted'));
									}
									if (res.team_missing) {
											toast.warning(t('chat.forkTeamMissing'));
									}
									setForkTarget(null);
									// 跳转新分支会话（团队调度权已移交）
									navigate(`/chat/${agentId}/${res.session_id}`);
									// 立即刷新会话列表：新分支尽快出现在侧栏；刷新落地前
									// chat 页重定向 effect 依赖 freshlyForked 标记放行该 id
									onTeamUpdated?.();
							}
					} finally {
							setFlowPending(false);
					}
			}, [forkTarget, agentId, sessionId, navigate, t, onTeamUpdated]);

		/** 工作流节点 → fork 入口（TeamFlowPanel 回调，带引导语）。 */
		const handleForkNode = useCallback(
				(e: { msgId?: string; from: string }, prompt?: string) => {
						if (e.msgId)
								setForkTarget({ msgId: e.msgId, name: e.from, prompt: prompt });
				},
				[],
		);

		/** 工作流节点 → 覆盖重跑入口：复用消息截断（同"从这里重开"）；
		 *  引导语暂存 sessionStorage，截断重载完成后自动发送。 */
		const handleRerunNode = useCallback(
				(e: { msgId?: string }, prompt?: string) => {
						if (!e.msgId || !agentId || !sessionId) return;
						if (prompt) {
								sessionStorage.setItem(
										`agentforge:auto-prompt:${agentId}:${sessionId}`,
										prompt,
								);
						}
						setTruncateTarget(e.msgId);
				},
				[agentId, sessionId],
		);

	// 待发引导语：覆盖重跑截断重载完成后自动发送（与 fork 跳转共用
	// sessionStorage 键控机制；先读后删保证只发一次）。
	useEffect(() => {
		if (!agentId || !sessionId || messagesLoading) return;
		const key = `agentforge:auto-prompt:${agentId}:${sessionId}`;
		const raw = sessionStorage.getItem(key);
		if (!raw) return;
		sessionStorage.removeItem(key);
		void send([
			{
				id: uuid(),
				type: 'text' as const,
				text: raw,
				created_at: new Date().toISOString(),
			},
		]);
	}, [agentId, sessionId, messagesLoading, send]);

	/** 流程重启：确认后上下文归零（消息历史保留）。 */
	const handleRestartConfirm = useCallback(async () => {
		setFlowPending(true);
		try {
			if (await restartFlow()) {
				toast.success(t('chat.restartDone'));
			}
		} finally {
			setFlowPending(false);
		}
	}, [restartFlow, t]);

	/** 团队暂停：中断 leader + 取消全部成员运行（上下文保留）。 */
	const handlePauseTeam = useCallback(async () => {
		if (!sessionId || !agentId) return;
		setFlowPending(true);
		try {
			const res = await sessionApi.pauseTeamFlow(sessionId, agentId);
			toast.success(
				t('chat.pauseTeamDone', { count: res.cancelled_members }),
			);
		} catch {
			// client.ts 已弹错误 toast
		} finally {
			setFlowPending(false);
		}
	}, [sessionId, agentId, t]);

	/**
	 * 继续（wake）：从当前状态继续推理。团队 leader 被唤醒后自行
	 * 恢复调度成员；普通会话即"继续上次思路"。
	 */
	const handleResumeFlow = useCallback(async () => {
		if (!sessionId || !agentId) return;
		setFlowPending(true);
		try {
			await sessionApi.resumeFlow(sessionId, agentId);
			toast.success(t('chat.resumeDone'));
		} catch {
			// client.ts 已弹错误 toast
		} finally {
			setFlowPending(false);
		}
	}, [sessionId, agentId, t]);

	/**
	 * 解散团队（用户主动，软解散）：取消成员运行 + 解除绑定，培养
	 * 资产全部保留。LLM 的 TeamDelete 已被无条件 DENY——这是唯一
	 * 解散入口（2026-09-09）。
	 */
	const handleDissolveConfirm = useCallback(async () => {
		if (!sessionId || !agentId) return;
		setFlowPending(true);
		try {
			await sessionApi.dissolveTeamFlow(sessionId, agentId);
			toast.success(t('chat.dissolveDone'));
		} catch {
			// client.ts 已弹错误 toast
		} finally {
			setFlowPending(false);
			// 刷新视图（team 解除绑定 → 面板转"已解散"态）
			void refetchSessions();
		}
	}, [sessionId, agentId, t, refetchSessions]);

	return (
		<>
			<main className="flex size-full">
				<ResizablePanelGroup orientation="horizontal">
					<ResizablePanel
						className="flex flex-1 rounded-[22px] bg-card shadow-panel"
						minSize="24rem"
					>
						<div className="flex flex-col flex-1 min-h-0 min-w-0 overflow-x-hidden p-2">
							<div className="flex flex-row gap-x-2 justify-between">
								<div className="flex flex-row items-center gap-x-1">
									<SidebarTrigger className="md:hidden" />
									{/* 成员迭代模式的返回入口（2026-09-08 用户反馈：
									    进入会话迭代后没有返回主理人的路径） */}
									{leaderNav && (
										<Button
											variant="outline"
											size="sm"
											className="gap-1 px-2 text-xs"
											title={t('chat.backToLeaderTooltip')}
											onClick={() =>
												navigate(`/chat/${leaderNav.agentId}/${leaderNav.sessionId}`)
											}
										>
											<ArrowLeft className="size-3.5" />
											<span>{t('chat.backToLeader')}</span>
										</Button>
									)}
								</div>
								<div className="flex flex-row gap-x-1">
									<LlmSelect
										id="tour-llm-select"
										variant="ghost"
										className="font-mono text-muted-foreground hover:text-foreground"
										value={selectedModel}
										onChange={handleLlmChange}
										onAddCredential={() => setCredentialOpen(true)}
										refetchTrigger={credentialRefetchTrigger}
										disabled={configPending}
									/>
									<ModelParametersPopover
										selectedModel={selectedModel}
										modelCard={selectedModelCard}
										onChange={handleParametersChange}
										selectedFallbackModel={selectedFallbackModel}
										onFallbackChange={handleFallbackChange}
										selectedTTSModel={selectedTTSModel}
										onTTSChange={handleTTSChange}
										disabled={configPending}
									/>
									<PermissionModeSelect
										id="tour-permission-mode"
										variant={'ghost'}
										className="font-mono text-muted-foreground hover:text-foreground"
										value={selectedPermissionMode}
										disabled={!sessionId || configPending}
										onChange={handlePermissionModeChange}
									/>
									{isLeader && sessionId && (
										<Button
											variant="ghost"
											size="sm"
											className="gap-1 px-2"
											title={t('dialog-archive.title')}
											onClick={() => setArchiveOpen(true)}
										>
											<Archive className="size-4" />
										</Button>
									)}
									{/* 流程重启（2026-09-08 v3）：上下文归零重新开始，
									    消息历史保留；带确认（防误触清空推理状态） */}
									{sessionId && (
										<Button
											variant="ghost"
											size="sm"
											className="gap-1 px-2"
											title={t('chat.restartTooltip')}
											disabled={phase !== 'idle' || flowPending}
											onClick={() => setRestartOpen(true)}
										>
											<RotateCw className="size-4" />
										</Button>
									)}
									{/* 布局模式一键切换（2026-09-07）：专注 ↔ 经典。
									    专注=完整对话+右侧团队/资源栏；经典=顶部团队
									    面板+右上角菜单 dock。偏好持久化。 */}
									<Button
										variant="outline"
										size="sm"
										className="gap-1 px-2 text-xs"
										title={
											layoutMode === 'focused'
												? t('chat.switchToClassic')
												: t('chat.switchToFocused')
										}
										onClick={() =>
											setLayoutMode((m) => (m === 'focused' ? 'classic' : 'focused'))
										}
									>
										{layoutMode === 'focused' ? (
											<>
												<PanelRightClose className="size-3.5" />
												<span className="hidden md:inline">
													{t('chat.classicLayout')}
												</span>
											</>
										) : (
											<>
												<PanelRight className="size-3.5" />
												<span className="hidden md:inline">
													{t('chat.focusedLayout')}
												</span>
											</>
										)}
									</Button>
									{/* 经典布局才有 dock 菜单；专注布局资源面板常驻 */}
									{layoutMode === 'classic' && (
										<DropdownMenu>
											<DropdownMenuTrigger asChild>
												<Button
													variant="ghost"
													size="sm"
													className="gap-1 px-2"
												>
													<PanelRight />
													<ChevronDown className="size-3 text-muted-foreground" />
												</Button>
											</DropdownMenuTrigger>
											<DropdownMenuContent align="end" className="w-auto">
												<DropdownMenuCheckboxItem
													checked={isPanelOpen('mcp')}
													onCheckedChange={() => togglePanel('mcp')}
													onSelect={(e) => e.preventDefault()}
												>
													<MCPSvg className="size-4" />
													MCP
												</DropdownMenuCheckboxItem>
												<DropdownMenuCheckboxItem
													checked={isPanelOpen('skill')}
													onCheckedChange={() => togglePanel('skill')}
													onSelect={(e) => e.preventDefault()}
												>
													<BookText />
													{t('panel.skill.title')}
												</DropdownMenuCheckboxItem>
												<DropdownMenuCheckboxItem
													checked={isPanelOpen('knowledge')}
													onCheckedChange={() => togglePanel('knowledge')}
													onSelect={(e) => e.preventDefault()}
												>
													<Database />
													{t('panel.knowledge.title')}
												</DropdownMenuCheckboxItem>
											</DropdownMenuContent>
										</DropdownMenu>
									)}
								</div>
							</div>
							<div className="flex flex-1 flex-col min-h-0 overflow-hidden">
								{/* 经典布局：团队驾驶舱在对话区顶部（专注布局只在
								右侧栏渲染，顶部不重复出现） */}
							{layoutMode === 'classic' && isLeader && sessionId ? (
								<TeamFlowPanel
									msgs={msgs}
									leaderName={leaderName}
									members={flowMembers}
									teamActive={!!view?.team}
									teamLive={teamLive ?? undefined}
									onOpenMember={handleOpenFlowMember}
									onForkNode={handleForkNode}
									onRerunNode={handleRerunNode}
									onPauseTeam={handlePauseTeam}
									onResumeTeam={handleResumeFlow}
									onDissolveTeam={() => setDissolveOpen(true)}
									teamBusy={phase !== 'idle' || flowPending}
								/>
							) : null}
								{/* 对话列宽度：填满中间面板（2026-09-07 用户反馈
								"顶栏宽、对话窄，对不上，与右栏之间留空白"）。
								顶栏/消息/输入框同宽对齐；经典布局的顶部团队
								面板同为全宽，纵向完全对齐。 */}
								<div className="flex flex-1 justify-center min-h-0 overflow-hidden relative [--chat-content-w:100%]">
									<ChatContent
									className={'max-w-[var(--chat-content-w)] w-full'}
									msgs={msgs}
									loading={messagesLoading}
									agentId={agentId}
									// 主理会话：团队 hint 消息渲染为紧凑单行
									// （完整内容在右侧团队驾驶舱，避免对话区被
									// 团队过程刷屏）
									compactTeamHints={isLeader}
									sessionId={sessionId}
									cwd={view?.session.config.cwd ?? null}
									onCwdChange={handleCwdChange}
									git={workspaceStatus?.git ?? null}
									onRefreshGit={refetchWorkspaceStatus}
									phase={phase}
									disabled={selectedModel === null}
									onSend={send}
									onUserConfirm={onUserConfirm}
									onInterrupt={interrupt}
									onTruncateAt={handleTruncateAt}
									onResume={handleResumeFlow}
									// cwd={
									// 	{cwd: view?.session.config.cwd, git: {
									// 		branch: 'main',
									// 		deletion: 0,
									// 		addition: 0,
									// 	}}
									// }
									footerSlot={
										subagentHitl.length > 0 ? (
											<SubagentHitlCard
												key={`${subagentHitl[0].worker_session_id}:${subagentHitl[0].reply_id}`}
												entry={subagentHitl[0]}
												onConfirm={(toolCall, confirm, rules) =>
													onSubagentConfirm(
														subagentHitl[0],
														toolCall,
														confirm,
														rules,
													)
												}
											/>
										) : null
									}
									allowedInputTypes={(
										selectedModelCard?.input_types ?? []
									).filter(
										(t) =>
											/^(image|video|audio|text)\/.+/.test(t) ||
											t === 'application/pdf' ||
											t.startsWith('application/vnd.') ||
											t.startsWith('application/msword') ||
											t.startsWith('application/vnd.openxmlformats'),
									)}
									fileProcessor={async (file) => {
										const filePath = (file as File & { path?: string }).path;
										if (filePath) {
											return {
												id: uuid(),
												type: 'data' as const,
												source: {
													type: 'url' as const,
													url: `file://${filePath}`,
													media_type:
														file.type || 'application/octet-stream',
												},
												name: file.name,
												created_at: new Date().toISOString(),
											};
										}
										if (file.type === 'text/plain') {
											const text = await file.text();
											return {
												id: uuid(),
												type: 'text' as const,
												text: `[File: ${file.name}]\n${text}`,
												created_at: new Date().toISOString(),
											};
										}
										const buffer = await file.arrayBuffer();
										const bytes = new Uint8Array(buffer);
										let binary = '';
										for (let i = 0; i < bytes.byteLength; i++) {
											binary += String.fromCharCode(bytes[i]);
										}
										const base64 = btoa(binary);
										return {
											id: uuid(),
											type: 'data' as const,
											source: {
												type: 'base64' as const,
												media_type: file.type || 'application/octet-stream',
												data: base64,
											},
											name: file.name,
											created_at: new Date().toISOString(),
										};
									}}
									/>
								</div>
							</div>
						</div>
					</ResizablePanel>
					{layoutMode === 'focused' ? (
						<>
							<ResizableHandle withHandle className="bg-transparent w-1.5" />
							{/* 专注布局右侧栏：上=团队工作流驾驶舱（无团队时组件
							    自行隐藏），下=资源面板（MCP/技能/知识库） */}
							<ResizablePanel
								minSize="19rem"
								defaultSize="23rem"
								maxSize="40rem"
								className="flex min-h-0 flex-col gap-2 overflow-y-auto"
							>
								{isLeader && sessionId ? (
									<div className="shrink-0">
											<TeamFlowPanel
													msgs={msgs}
													leaderName={leaderName}
													members={flowMembers}
													teamActive={!!view?.team}
													teamLive={teamLive ?? undefined}
													onOpenMember={handleOpenFlowMember}
												onForkNode={handleForkNode}
												onRerunNode={handleRerunNode}
												onPauseTeam={handlePauseTeam}
												onResumeTeam={handleResumeFlow}
												onDissolveTeam={() => setDissolveOpen(true)}
												teamBusy={phase !== 'idle' || flowPending}
											/>
										</div>
									) : null}
								<ResourceTabsPanel
									mcp={resourceContent.mcp}
									skill={resourceContent.skill}
									knowledge={resourceContent.knowledge}
									knowledgeActions={knowledgeActions}
									mcpCount={mcps.length}
									skillCount={skills.length}
									knowledgeCount={
										selectedKnowledgeConfig?.knowledge_base_ids.length ?? 0
									}
								/>
							</ResizablePanel>
						</>
					) : (
						<>
							{panelLayout.length > 0 && (
								<ResizableHandle withHandle className="bg-transparent w-1.5" />
							)}
							<PanelDock
								layout={panelLayout}
								panels={panels}
								onClosePanel={closePanel}
							/>
						</>
					)}
				</ResizablePanelGroup>
			</main>
			<CreateCredentialDialog
				open={credentialOpen}
				onOpenChange={setCredentialOpen}
				onCreated={() => setCredentialRefetchTrigger((n) => n + 1)}
			/>
			{isLeader && sessionId && agentId ? (
				<ArchiveDialog
					open={archiveOpen}
					onOpenChange={setArchiveOpen}
					agentId={agentId}
					sessionId={sessionId}
					leaderName={leaderName}
					members={archiveMembers}
				/>
			) : null}
			{/* 流程控制确认对话框（2026-09-08 v3）：重启 / 从这里重开 */}
			<DeleteDialog
				open={restartOpen}
				onOpenChange={setRestartOpen}
				title={t('chat.restartTitle')}
				description={t('chat.restartDescription')}
				confirmLabel={t('chat.restartConfirm')}
				onConfirm={handleRestartConfirm}
			/>
			{/* 解散团队确认（2026-09-09）：用户主动解散的唯一入口——
			    LLM 的 TeamDelete 已被无条件 DENY。软解散：资产保留 */}
			<DeleteDialog
				open={dissolveOpen}
				onOpenChange={setDissolveOpen}
				title={t('chat.dissolveTitle')}
				description={t('chat.dissolveDescription')}
				confirmLabel={t('chat.dissolveConfirm')}
				onConfirm={handleDissolveConfirm}
			/>
			<DeleteDialog
				open={forkTarget !== null}
				onOpenChange={(open) => {
					if (!open) setForkTarget(null);
				}}
				title={t('chat.forkTitle', { name: forkTarget?.name ?? '' })}
				// 源会话无在册团队（已解散）时替换为警告文案——工作流图
				// 来自消息历史（旧节点仍显示），但团队实际不在册，用户
				// 应在确认前知道分支将退化为普通会话（2026-09-08）
				description={
					view?.team
						? t('chat.forkDescription')
						: t('chat.forkNoTeamDescription')
				}
				confirmLabel={t('chat.forkConfirm')}
				onConfirm={handleForkConfirm}
			/>
			<DeleteDialog
				open={truncateTarget !== null}
				onOpenChange={(open) => {
					if (!open) setTruncateTarget(null);
				}}
				title={t('chat.truncateTitle')}
				description={t('chat.truncateDescription')}
				confirmLabel={t('chat.truncateConfirm')}
				onConfirm={handleTruncateConfirm}
			/>
		</>
	);
}
