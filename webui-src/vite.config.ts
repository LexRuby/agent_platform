import path from 'path';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import svgr from 'vite-plugin-svgr';

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || 'http://localhost:3000';
const backendPathPatterns = [
	'^/api(?:/|$)',
	'^/auth(?:/|$)',
	'^/login(?:/|$)',
	'^/agent(?:/|$)',
	'^/agent-capabilities(?:/|$)',
	'^/agent-observations(?:/|$)',
	'^/agent-share(?:/|$)',
	'^/chat/$',
	'^/credential(?:/|$)',
	'^/embedding-model(?:/|$)',
	'^/health(?:/|$)',
	'^/knowledge_bases(?:/|$)',
	'^/mcp(?:/|$)',
	'^/mcp-tools(?:/|$)',
	'^/model(?:/|$)',
	'^/schedule(?:/|$)',
	'^/sessions(?:/|$)',
	'^/skill(?:/|$)',
	'^/team-archive(?:/|$)',
	'^/team-flow(?:/|$)',
	'^/tts-model(?:/|$)',
	'^/usage(?:/|$)',
	'^/workspace(?:/|$)',
	'^/workspace-projects(?:/|$)',
	'^/hub(?:/|$)',
	'^/channels(?:/|$)',
];

export default defineConfig({
	plugins: [react(), tailwindcss(), svgr()],
	server: {
		proxy: Object.fromEntries(
			backendPathPatterns.map((path) => [path, {
				target: apiProxyTarget,
				changeOrigin: true,
			}]),
		),
	},
	resolve: {
		alias: {
			'@': path.resolve(__dirname, './src'),
			'next/navigation': path.resolve(__dirname, './src/lib/next-navigation-shim.ts'),
		},
	},
	optimizeDeps: {
		include: ['mime-types'],
	},
});
