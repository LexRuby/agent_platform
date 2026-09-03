import { LogOut, UserRound } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { authApi } from '@/api';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuSeparator,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { SidebarMenuButton } from '@/components/ui/sidebar';
import { useTranslation } from '@/i18n/useI18n';

/**
 * Sidebar footer account menu: shows who is logged in (the SaaS tenant
 * boundary) and offers logout. Before this existed the only way to switch
 * accounts was to let the session cookie expire, and two accounts in one
 * browser looked like "one shared space".
 */
export function UserMenu() {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const [username, setUsername] = useState<string | null>(null);

	useEffect(() => {
		let cancelled = false;
		authApi
			.me()
			.then((res) => {
				if (!cancelled) setUsername(res.username);
			})
			.catch(() => {
				// 401 → not logged in; leave null, button still offers logout
			});
		return () => {
			cancelled = true;
		};
	}, []);

	const handleLogout = async () => {
		try {
			await authApi.logout();
		} finally {
			// Even if the request fails, drop local state and bounce to login.
			localStorage.removeItem('username');
			navigate('/login', { replace: true });
			window.location.href = '/login';
		}
	};

	return (
		<DropdownMenu>
			<DropdownMenuTrigger asChild>
				<SidebarMenuButton
					tooltip={{
						children: username ?? t('user-menu.account'),
						hidden: false,
					}}
					className="justify-center"
				>
					<UserRound />
				</SidebarMenuButton>
			</DropdownMenuTrigger>
			<DropdownMenuContent side="right" align="end" className="w-48">
				<DropdownMenuLabel className="font-normal">
					<div className="text-xs text-muted-foreground">
						{t('user-menu.current')}
					</div>
					<div className="text-sm font-medium truncate">
						{username ?? t('user-menu.unknown')}
					</div>
				</DropdownMenuLabel>
				<DropdownMenuSeparator />
				<DropdownMenuItem onClick={() => void handleLogout()}>
					<LogOut />
					{t('user-menu.logout')}
				</DropdownMenuItem>
			</DropdownMenuContent>
		</DropdownMenu>
	);
}
