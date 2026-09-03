/**
 * Agent 版本封板 API：freeze / unfreeze / save-version / restore。
 *
 * 对应后端 app/agent_version.py。冻结中的 agent PATCH 会被后端
 * 403 拦截（自我迭代停止）；restore 是显式授权操作，冻结中也可执行。
 */
import { client } from './client';

export interface VersionBrief {
    version: number;
    created_at: string;
    label: string;
}

export interface AgentVersionStatus {
    agent_id: string;
    frozen: boolean;
    current_version: number | null;
    latest_version: number | null;
    versions: VersionBrief[];
}

export interface VersionDetail extends VersionBrief {
    data: Record<string, unknown>;
}

export const agentVersionApi = {
    /** 冻结封板：当前配置固化为版本号，拦截后续修改。 */
    freeze: (agentId: string, label?: string) =>
        client.post<AgentVersionStatus>(
            `/agent/${agentId}/freeze`,
            { label: label ?? '' },
            undefined,
            { silent: true },
        ),
    /** 解冻：开放模式，恢复可编辑。 */
    unfreeze: (agentId: string) =>
        client.post<AgentVersionStatus>(
            `/agent/${agentId}/unfreeze`,
            undefined,
            undefined,
            { silent: true },
        ),
    /** 保存当前配置为新版本（开放模式下迭代满意后手动存版）。 */
    saveVersion: (agentId: string, label?: string) =>
        client.post<AgentVersionStatus>(
            `/agent/${agentId}/save-version`,
            { label: label ?? '' },
            undefined,
            { silent: true },
        ),
    /** 版本列表（不含快照正文）。 */
    list: (agentId: string) =>
        client.get<AgentVersionStatus>(`/agent/${agentId}/versions`),
    /** 恢复到历史版本（显式授权，冻结中也可执行）。 */
    restore: (agentId: string, version: number) =>
        client.post<AgentVersionStatus>(
            `/agent/${agentId}/versions/${version}/restore`,
            undefined,
            undefined,
            { silent: true },
        ),
};
