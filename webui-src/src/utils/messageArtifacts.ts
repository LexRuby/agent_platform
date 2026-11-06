const ARTIFACT_EXTENSIONS = /\.(?:html?|json|md|csv|xlsx?|docx?|pdf)$/i;

/** Extract safe, relative deliverable names mentioned in assistant text. */
export function extractMessageArtifacts(text: string): string[] {
	const candidates = text.match(/[\w\u3400-\u9fff./-]+\.(?:html?|json|md|csv|xlsx?|docx?|pdf)\b/gi) ?? [];
	const seen = new Set<string>();
	for (const candidate of candidates) {
		const path = candidate.replace(/^\.\//, '').replace(/[.,;:：，。；、)）\]}]+$/, '');
		if (
			path &&
			path.length <= 240 &&
			!path.startsWith('/') &&
			!path.includes('..') &&
			ARTIFACT_EXTENSIONS.test(path)
		) {
			seen.add(path);
		}
	}
	return [...seen];
}
