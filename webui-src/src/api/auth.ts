import { client } from './client';

/**
 * Cookie-session auth: login state lives in the backend session, the API
 * only reads who is logged in and ends the session.
 */
export const authApi = {
	/** Current session owner; 401 when logged out. */
	me: () => client.get<{ username: string }>('/auth/me', undefined, { silent: true }),

	/** Ends the cookie session; the caller then redirects to /login. */
	logout: () => client.post<{ ok: boolean }>('/auth/logout', undefined),
};
