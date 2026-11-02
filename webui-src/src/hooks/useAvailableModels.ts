import { useState, useEffect, useCallback } from 'react';

import { credentialApi, modelApi } from '@/api';
import type { CredentialView, ModelCard } from '@/api';

export interface CredentialWithModels {
	credential: CredentialView;
	models: ModelCard[];
}

/**
 * Fetches all credentials and their available models, grouped by provider type.
 * Provider type is read from `credential.data.type`.
 * Credentials without a `type` field or whose model fetch fails are silently skipped.
 */
export function useAvailableModels() {
	const [groups, setGroups] = useState<Record<string, CredentialWithModels[]>>({});
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState<Error | null>(null);

	const refetch = useCallback(async () => {
		setLoading(true);
		setError(null);
		try {
			const { credentials } = await credentialApi.list();
			const result: Record<string, CredentialWithModels[]> = {};

			await Promise.all(
				credentials.map(async (credential) => {
					const type = credential.data.type as string | undefined;
					if (!type) return;
					if (!result[type]) result[type] = [];
					try {
						const { models } = await modelApi.list(type);
						result[type].push({ credential, models });
					} catch {
						result[type].push({ credential, models: [] });
					}
				}),
			);

			setGroups(result);
		} catch (e) {
			setError(e as Error);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void refetch();
		// Login-triggered provider refresh runs in the background. Follow up
		// shortly so newly enabled models appear without a manual reload.
		const quick = window.setTimeout(() => void refetch(), 2000);
		const settled = window.setTimeout(() => void refetch(), 8000);
		const refreshWhenVisible = () => {
			if (document.visibilityState === 'visible') void refetch();
		};
		window.addEventListener('focus', refreshWhenVisible);
		document.addEventListener('visibilitychange', refreshWhenVisible);
		return () => {
			window.clearTimeout(quick);
			window.clearTimeout(settled);
			window.removeEventListener('focus', refreshWhenVisible);
			document.removeEventListener('visibilitychange', refreshWhenVisible);
		};
	}, [refetch]);

	return { groups, loading, error, refetch };
}
