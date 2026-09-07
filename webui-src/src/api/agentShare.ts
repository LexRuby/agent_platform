import { client } from './client';
import type { ShareInfo } from './types';

/**
 * 智能体共享（账号 → 智能体可见性，2026-09-07 共享 v1）。
 * 后端 RedisAgentSharePolicy 驱动官方 ResourceAccess 链路：
 * 被共享账号在 /agent/ 列表自动看到（editable=false 只读），
 * 这里只负责发布管理的增删查。
 */
export const agentShareApi = {
	/** 我发布的智能体及可见性。 */
	mine: () => client.get<{ shares: ShareInfo[] }>('/agent-share/mine'),

	/** 发布/更新可见性（mode: private | users | public）。 */
	set: (agentId: string, mode: string, users: string[] = []) =>
		client.put<ShareInfo>(`/agent-share/${agentId}`, { mode, users }),

	/** 取消发布（恢复私有）。 */
	remove: (agentId: string) => client.delete(`/agent-share/${agentId}`),
};
