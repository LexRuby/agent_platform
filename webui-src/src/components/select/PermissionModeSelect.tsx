import { ChevronDown, UserRoundKey } from 'lucide-react';

import type { PermissionMode } from '@/api/types';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuGroup,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useTranslation } from '@/i18n/useI18n.ts';
import { cn } from '@/lib/utils.ts';

const PERMISSION_MODES: PermissionMode[] = [
	'default',
	'accept_edits',
	'explore',
	'bypass',
	'dont_ask',
];

interface Props extends Omit<React.ComponentPropsWithoutRef<typeof Button>, 'onChange' | 'value'> {
	className?: string;
	value?: PermissionMode;
	disabled?: boolean;
	onChange?: (value: PermissionMode) => void;
}

export function PermissionModeSelect({ className, value, disabled, onChange, ...props }: Props) {
	const { t } = useTranslation();

	const displayLabel = value
		? t(`permission-mode.${value}-label`)
		: t('permission-mode.placeholder');

	return (
		<DropdownMenu>
			<DropdownMenuTrigger asChild>
				<Button
					variant="outline"
					size="sm"
					className={cn('justify-between gap-1 font-normal', className)}
					disabled={disabled}
					tooltip={t('permission-mode.trigger-tooltip')}
					{...props}
				>
					<div className="flex flex-row items-center gap-x-2">
						<UserRoundKey />
						<span className="truncate">{displayLabel}</span>
					</div>
					<ChevronDown className="size-3.5 text-muted-foreground" />
				</Button>
			</DropdownMenuTrigger>
			<DropdownMenuContent align="start" className="min-w-64">
				<DropdownMenuGroup>
					<DropdownMenuLabel>{t('permission-mode.label')}</DropdownMenuLabel>
					{PERMISSION_MODES.map((mode) => (
						<Tooltip key={mode}>
							<TooltipTrigger asChild>
								{/* 名称 + 效果说明直接展示（不藏 hover）——
								    2026-09-09 用户反馈：选了 accept_edits 仍被
								    pip install 等执行类命令反复询问，"仅文件
								    操作"的语义边界在选择时完全不可见 */}
								<DropdownMenuItem onSelect={() => onChange?.(mode)}>
									<div className="flex flex-col items-start gap-0.5">
										<span>{t(`permission-mode.${mode}-label`)}</span>
										<span className="text-[11px] leading-tight text-muted-foreground">
											{t(`permission-mode.${mode}-tooltip`)}
										</span>
									</div>
								</DropdownMenuItem>
							</TooltipTrigger>
							<TooltipContent side="right">
								{t(`permission-mode.${mode}-tooltip`)}
							</TooltipContent>
						</Tooltip>
					))}
				</DropdownMenuGroup>
			</DropdownMenuContent>
		</DropdownMenu>
	);
}
