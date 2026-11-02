import { ChevronDown, PlusCircle, Ban } from 'lucide-react';
import { useEffect } from 'react';

import type { ChatModelConfig } from '@/api';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuGroup,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuPortal,
	DropdownMenuSeparator,
	DropdownMenuSub,
	DropdownMenuSubContent,
	DropdownMenuSubTrigger,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useAvailableModels } from '@/hooks/useAvailableModels';
import { useTranslation } from '@/i18n/useI18n.ts';
import { cn } from '@/lib/utils';
import { credentialLabel } from '@/utils/common';

interface Props extends Omit<React.ComponentPropsWithoutRef<typeof Button>, 'onChange' | 'value'> {
	value?: ChatModelConfig | null;
	/**
	 * Called when the user selects a model, or — when `allowClear` is true —
	 * clears the selection (in which case `null` is emitted).
	 */
	onChange?: (value: ChatModelConfig | null) => void;
	onAddCredential?: () => void;
	refetchTrigger?: number;
	/** Override the trigger label shown when no model is selected. */
	placeholder?: string;
	/**
	 * When true, append a "clear selection" item to the dropdown that emits
	 * `null` via `onChange`. Used by the fallback selector.
	 */
	allowClear?: boolean;
	/** Override the label of the "clear selection" item. */
	clearLabel?: string;
}

export function LlmSelect({
	value,
	onChange,
	onAddCredential,
	refetchTrigger,
	placeholder,
	allowClear = false,
	clearLabel,
	className,
	...props
}: Props) {
	const { groups, loading, refetch } = useAvailableModels();
	const { t } = useTranslation();
	// Credentials whose model list failed to load come back with an empty
	// `models` array; drop them so they don't render empty submenus
	const groupEntries = Object.entries(groups)
		.map(([type, items]) => [type, items.filter((i) => i.models.length > 0)] as const)
		.filter(([, usable]) => usable.length > 0);
	const hasOptions = groupEntries.length > 0;

	useEffect(() => {
		if (refetchTrigger !== undefined && refetchTrigger > 0) refetch();
	}, [refetchTrigger, refetch]);

	const handleSelect = (type: string, credentialId: string, model: string) => {
		onChange?.({ type, credential_id: credentialId, model, parameters: {} });
	};

	const selectedCard = value
		? groups[value.type]
				?.find((item) => item.credential.id === value.credential_id)
				?.models.find((model) => model.name === value.model)
		: undefined;
	const displayLabel = value?.model
		? (selectedCard?.label || value.model)
		: loading
			? t('llm-select.loading')
			: (placeholder ?? t('llm-select.placeholder'));
	const providerLabel = (type: string) =>
		type === 'ark_credential'
			? '豆包 ARK'
			: type.replace(/_credential$/, '').replaceAll('_', ' ');

	return (
		<DropdownMenu>
			<DropdownMenuTrigger asChild>
				<Button
					variant="outline"
					size="sm"
					className={cn('w-64 justify-between gap-2 font-normal', className)}
					{...props}
				>
					<span className="truncate">{displayLabel}</span>
					<ChevronDown className="size-3.5 text-muted-foreground" />
				</Button>
			</DropdownMenuTrigger>
			<DropdownMenuContent
				align="end"
				className="w-[min(30rem,calc(100vw-2rem))] max-h-[60vh] overflow-y-auto p-1.5"
			>
				{!loading && !hasOptions ? (
					<div className="px-2 py-3 text-center text-sm text-muted-foreground">
						<p className="font-medium">{t('llm-select.empty.title')}</p>
						<p className="text-xs mt-1">{t('llm-select.empty.description')}</p>
					</div>
				) : (
					groupEntries.map(([type, usable], idx) => {
						const isSingle = usable.length === 1;
						return (
							<DropdownMenuGroup key={type}>
								{idx > 0 && <DropdownMenuSeparator />}
								<DropdownMenuLabel>
									{providerLabel(type)}
								</DropdownMenuLabel>
								{isSingle
									? usable[0].models.map((m) => (
											<DropdownMenuItem
												key={m.name}
												onSelect={() =>
													handleSelect(
														type,
														usable[0].credential.id,
														m.name,
													)
												}
											>
												{m.label || m.name}
											</DropdownMenuItem>
										))
									: usable.map(({ credential, models }) => (
											<DropdownMenuSub key={credential.id}>
												<DropdownMenuSubTrigger>
													{credentialLabel(credential)}
												</DropdownMenuSubTrigger>
												<DropdownMenuPortal>
													<DropdownMenuSubContent className="w-96 max-h-[60vh] overflow-y-auto">
														{models.map((m) => (
															<DropdownMenuItem
																key={m.name}
																onSelect={() =>
																	handleSelect(
																		type,
																		credential.id,
																		m.name,
																	)
																}
															>
																{m.label}
															</DropdownMenuItem>
														))}
													</DropdownMenuSubContent>
												</DropdownMenuPortal>
											</DropdownMenuSub>
										))}
							</DropdownMenuGroup>
						);
					})
				)}
				<DropdownMenuSeparator />
				{allowClear && (
					<DropdownMenuItem onSelect={() => onChange?.(null)} disabled={!value}>
						<Ban className="size-4" />
						<span>{clearLabel ?? t('llm-select.clear')}</span>
					</DropdownMenuItem>
				)}
				<DropdownMenuItem onSelect={onAddCredential}>
					<PlusCircle className="size-4" />
					<span>{t('llm-select.addCredential')}</span>
				</DropdownMenuItem>
			</DropdownMenuContent>
		</DropdownMenu>
	);
}
