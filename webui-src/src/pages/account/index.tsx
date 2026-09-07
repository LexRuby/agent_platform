import { CircleUserRound } from 'lucide-react';
import { useEffect, useState } from 'react';

import { authApi } from '@/api';
import { Separator } from '@/components/ui/separator';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useTranslation } from '@/i18n/useI18n';

import { ShareManagement } from './ShareManagement';
import { UsageOverview } from './UsageOverview';

/**
 * 账户中心（2026-09-08）：登录账号的消费与发布管理一站式入口。
 *
 * 回答用户诉求"一个账户界面，直观看到我的消费情况 + 管理我发布的
 * 大小A 的可见性"：
 * - 消费概览：产品维度用量（大A及team 整体 / 独立小A）+ 模型拆分
 * - 发布管理：我封装/发布的大A、小A 的可见性控制 + 共享给我
 */
export function AccountPage() {
	const { t } = useTranslation();
	const [username, setUsername] = useState<string>('');

	useEffect(() => {
		void authApi
			.me()
			.then((res) => setUsername(res.username))
			.catch(() => setUsername(''));
	}, []);

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				{/* 页头：当前账号 */}
				<div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
					<div className="flex items-center gap-3">
						<span className="flex size-11 items-center justify-center rounded-full bg-primary/10">
							<CircleUserRound className="size-6 text-primary" />
						</span>
						<div>
							<div className="text-2xl font-semibold">
								{t('account.title')}
							</div>
							<div className="mt-1 text-sm text-muted-foreground">
								{username
									? t('account.subtitle', { user: username })
									: t('account.subtitle-loading')}
							</div>
						</div>
					</div>
				</div>
				<Separator />

				{/* 主体：消费概览 / 发布管理 双 Tab */}
				<Tabs defaultValue="usage" className="flex min-h-0 flex-1 flex-col gap-4 px-6 pt-4">
					<TabsList className="w-fit">
						<TabsTrigger value="usage">{t('account.tab-usage')}</TabsTrigger>
						<TabsTrigger value="share">{t('account.tab-share')}</TabsTrigger>
					</TabsList>
					<TabsContent value="usage" className="min-h-0 flex-1 overflow-y-auto pb-6">
						<UsageOverview />
					</TabsContent>
					<TabsContent value="share" className="min-h-0 flex-1 overflow-y-auto pb-6">
						<ShareManagement />
					</TabsContent>
				</Tabs>
			</main>
		</div>
	);
}
