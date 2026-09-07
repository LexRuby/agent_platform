/**
 * 安全的 UUID 生成：裸 IP HTTP 访问（如 http://116.204.102.229:30000）
 * 属于不安全上下文，``crypto.randomUUID`` 为 ``undefined``，直接调用
 * 会抛 TypeError——2026-09-07 用户"点击发送无反应、无网络请求"事故
 * （TextInput.handleSend 构建首个文本块即崩溃，消息发不出）。
 *
 * 安全上下文（HTTPS / localhost）走原生实现；否则回退到
 * ``getRandomValues``（不安全上下文仍可用）拼 RFC 4122 v4。
 */
export function uuid(): string {
	if (typeof crypto !== 'undefined' && crypto.randomUUID) {
		return crypto.randomUUID();
	}
	// 回退：getRandomValues 在不安全上下文依然可用
	const bytes = new Uint8Array(16);
	crypto.getRandomValues(bytes);
	// 版本 4 + 变体位
	bytes[6] = (bytes[6] & 0x0f) | 0x40;
	bytes[8] = (bytes[8] & 0x3f) | 0x80;
	const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
	return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
