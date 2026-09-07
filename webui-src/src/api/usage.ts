import { client } from './client';
import type { UsageSummary } from './types';

/**
 * Token 用量统计：消费计量 v1（2026-09-07）。
 * 后端按登录账号（X-User-ID，服务端会话注入）隔离数据，
 * 这里只负责拉取汇总——模型 / 大A小A / 输入输出四个维度。
 */
export const usageApi = {
	/** 账号用量汇总（近 N 天，1–365）。 */
	summary: (days: number) =>
		client.get<UsageSummary>('/usage/summary', { days: String(days) }),
};
