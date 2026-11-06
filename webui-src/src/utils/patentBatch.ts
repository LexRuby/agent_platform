import type { ContentBlock } from '@agentscope-ai/agentscope/message';

export const PATENT_EXTRACTION_MARKER = 'agentforge-profile: patent-technology-extraction';
export const PATENT_BATCH_SIZE = 500;
export const PATENT_BATCH_MAX_CHARS = 120_000;

type ExtractionPayload = { content: unknown[]; [key: string]: unknown } | unknown[];

function textBlock(content: ContentBlock[]): Extract<ContentBlock, { type: 'text' }> | null {
	const block = content.find((item): item is Extract<ContentBlock, { type: 'text' }> => item.type === 'text');
	return block ?? null;
}

/**
 * Split only the structured extraction payload. Non-JSON messages and small
 * inputs remain untouched so normal agents keep their existing behavior.
 */
export function splitPatentExtractionContent(
	content: ContentBlock[],
	chunkSize = PATENT_BATCH_SIZE,
	maxChars = PATENT_BATCH_MAX_CHARS,
): ContentBlock[][] {
	const block = textBlock(content);
	if (!block || chunkSize < 1) return [content];
	let parsed: ExtractionPayload;
	try {
		parsed = JSON.parse(block.text) as ExtractionPayload;
	} catch {
		return [content];
	}
	const items = Array.isArray(parsed) ? parsed : parsed && Array.isArray(parsed.content) ? parsed.content : null;
	if (!items || items.length <= chunkSize) return [content];

	const otherBlocks = content.filter((item) => item !== block);
	const chunks: ContentBlock[][] = [];
	let start = 0;
	while (start < items.length) {
		let end = Math.min(start + chunkSize, items.length);
		while (end > start + 1) {
			const candidate = items.slice(start, end);
			const payload = Array.isArray(parsed) ? candidate : { ...parsed, content: candidate };
			if (JSON.stringify(payload).length <= maxChars) break;
			end -= 1;
		}
		const slice = items.slice(start, end);
		const payload = Array.isArray(parsed) ? slice : { ...parsed, content: slice };
		chunks.push([
			{
				...block,
				text: JSON.stringify(payload),
			},
			...otherBlocks,
		]);
		start = end;
	}
	return chunks;
}

export function extractionBatchLabel(index: number, total: number): string {
	return `专利技术信息提取批次 ${index + 1}/${total}`;
}
