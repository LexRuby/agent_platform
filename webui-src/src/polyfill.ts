/**
 * crypto.randomUUID 全局 polyfill（必须在所有模块之前 import）。
 *
 * 裸 IP 的 HTTP 访问（如 http://116.204.102.229:30000）不是安全
 * 上下文，浏览器不提供 ``crypto.randomUUID``（undefined）。官方
 * npm 包（@agentscope-ai/agentscope 的 UserMsg / 事件构造等）与
 * 我们自己的代码都有调用点——2026-09-07 用户"点发送无反应"事故：
 * ``UserMsg()`` 在 ``useMessages.send`` 里抛 TypeError，被
 * ``main.tsx`` 的 unhandledrejection 监听器静默吞掉 → 输入框清空、
 * 无 POST /chat、无 console 错误。
 *
 * 修复：模块加载阶段（任何用户交互之前）补齐 randomUUID——
 * ``getRandomValues`` 在不安全上下文依然可用，用它拼 RFC 4122 v4。
 * 安全上下文（HTTPS/localhost）不受影响，走原生实现。
 */
if (typeof crypto !== 'undefined' && !crypto.randomUUID) {
	const bytes = new Uint8Array(16);
	Object.defineProperty(crypto, 'randomUUID', {
		value: () => {
			crypto.getRandomValues(bytes);
			// 版本 4 + 变体位（RFC 4122）
			bytes[6] = (bytes[6] & 0x0f) | 0x40;
			bytes[8] = (bytes[8] & 0x3f) | 0x80;
			const h = Array.from(bytes, (b) =>
				b.toString(16).padStart(2, '0'),
			).join('');
			return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
		},
		writable: true,
		configurable: true,
	});
}
