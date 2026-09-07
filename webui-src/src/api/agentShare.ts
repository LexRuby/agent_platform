import { client } from './client';
import type { PublicationInfo, ShareInfo } from './types';

/**
 * 智能体共享（账号 → 智能体可见性，2026-09-07 共享 v1）。
 * 后端 RedisAgentSharePolicy 驱动官方 ResourceAccess 链路：
 * 被共享账号在 /agent/ 列表自动看到（editable=false 只读），
 * 这里只负责发布管理的增删查。
 *
 * 版本化发布（2026-09-08 v2）：发布 = 从版本快照复制出独立
 * 对外产品（可重命名）+ 共享——同一智能体的 v2/v3 可分别发布。
 */
export const agentShareApi = {
        /** 我发布的智能体及可见性。 */
        mine: () => client.get<{ shares: ShareInfo[] }>('/agent-share/mine'),

        /** 发布/更新可见性（mode: private | users | public）。 */
        set: (agentId: string, mode: string, users: string[] = []) =>
                client.put<ShareInfo>(`/agent-share/${agentId}`, { mode, users }),

        /** 取消发布（恢复私有）。 */
        remove: (agentId: string) => client.delete(`/agent-share/${agentId}`),

        /** 版本化发布：版本快照 → 独立对外产品（重命名）+ 共享。 */
        publish: (
                agentId: string,
                version: number,
                displayName: string,
                mode: 'users' | 'public',
                users: string[] = [],
        ) =>
                client.post<PublicationInfo>('/agent-share/publish', {
                        agent_id: agentId,
                        version,
                        display_name: displayName,
                        mode,
                        users,
                }),

        /** 我的发布物列表（对外产品 + 溯源信息）。 */
        publications: () =>
                client.get<{ publications: PublicationInfo[] }>('/agent-share/pubs'),
};
