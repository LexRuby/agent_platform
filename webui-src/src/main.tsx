import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

// 必须最先 import：裸 IP HTTP 访问是不安全上下文，crypto.randomUUID
// 为 undefined——官方包 UserMsg/事件构造全崩（2026-09-07 发送事故）
import './polyfill';
import './index.css';
import './i18n';
import App from './App.tsx';
import { TooltipProvider } from '@/components/ui/tooltip.tsx';

// 部署新版后，已打开的旧页面继续懒加载旧 hash chunk 会 404
// （"Failed to fetch dynamically imported module"，2026-09-04 用户
// 点击技能详情报错）。捕获后自动整页刷新：index.html 响应带
// no-cache，刷新即拿到引用新 chunk 的最新 HTML。sessionStorage
// 标记防止刷新死循环；新页面 load 成功即清除标记，下次部署后
// 失效仍可再触发一次自愈。
const ASSET_RELOAD_FLAG = 'agentforge:asset-reloaded';
const isAssetLoadFailure = (msg: string) =>
        /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i.test(
                msg,
        );

window.addEventListener('unhandledrejection', (e) => {
        const reason = e.reason as { message?: unknown } | null;
        const msg = String(
                (reason && typeof reason === 'object' && 'message' in reason
                        ? reason.message
                        : reason) ?? '',
        );
        if (isAssetLoadFailure(msg) && !sessionStorage.getItem(ASSET_RELOAD_FLAG)) {
                sessionStorage.setItem(ASSET_RELOAD_FLAG, '1');
                window.location.reload();
                return;
        }
        // 资产自愈之外的 rejection 不再静默——2026-09-07 发送事故里
        // UserMsg 的 TypeError 被这里吞掉，输入框清空却无任何报错，
        // 排查被严重拖延。至少留下 console 痕迹。
        console.error('[unhandledrejection]', reason ?? msg);
});
// 资源加载失败（<script> 等）只进捕获阶段的 error 事件
window.addEventListener(
        'error',
        (e) => {
                const target = e.target as HTMLElement | null;
                if (
                        target?.tagName === 'SCRIPT' &&
                        !sessionStorage.getItem(ASSET_RELOAD_FLAG)
                ) {
                        sessionStorage.setItem(ASSET_RELOAD_FLAG, '1');
                        window.location.reload();
                }
        },
        true,
);
window.addEventListener('load', () => sessionStorage.removeItem(ASSET_RELOAD_FLAG));

createRoot(document.getElementById('root')!).render(
        <StrictMode>
                <TooltipProvider>
                        <App />
                </TooltipProvider>
        </StrictMode>,
);
