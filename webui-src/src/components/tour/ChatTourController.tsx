import { useOnborda } from 'onborda';
import { useEffect, useRef } from 'react';

import { CHAT_TOUR_NAME } from './chatTourSteps';

interface Props {
	agentsCount: number;
	sessionsCount: number;
	/**
	 * True once the agent / session lists have finished their first load.
	 * Until then counts are 0-for-unknown, and starting the tour would
	 * misclassify every returning user as a first-time visitor.
	 */
	ready?: boolean;
	/** Force-open the sidebar so #tour-create-session exists in the DOM. */
	onEnsureSidebarOpen?: () => void;
}

const TOUR_DONE_KEY = 'chat_tour_done';
const FORCE_TOUR_KEY = 'force_tour';

/** Tour steps are anchored to these selectors; all must exist in the DOM. */
const REQUIRED_TARGETS = [
	'#tour-create-agent',
	'#tour-create-session',
	'#tour-llm-select',
	'#tour-permission-mode',
	'#tour-chat-input',
];

const markDone = () => localStorage.setItem(TOUR_DONE_KEY, '1');

export const ChatTourController = ({ agentsCount, sessionsCount, ready, onEnsureSidebarOpen }: Props) => {
	const { currentStep, currentTour, setCurrentStep, startOnborda, closeOnborda } =
		useOnborda();
	const startCountsRef = useRef({ agents: agentsCount, sessions: sessionsCount });
	const startedRef = useRef(false);

	// Auto-start on mount — but only for genuine first-time visitors.
	// 2026-09-03 incident: returning users (agents/sessions already present)
	// got the onborda spotlight overlay on every fresh deploy, which swallows
	// all clicks — the chat send button included — until the tour finished.
	useEffect(() => {
		if (startedRef.current) return;
		if (ready === false) return; // lists not loaded yet — wait
		const force = sessionStorage.getItem(FORCE_TOUR_KEY) === '1';
		const done = localStorage.getItem(TOUR_DONE_KEY) === '1';
		if (force) sessionStorage.removeItem(FORCE_TOUR_KEY);
		if (!force && done) return;
		if (!force && (agentsCount > 0 || sessionsCount > 0)) {
			// Returning user: silently mark the tour done instead of
			// trapping them behind the spotlight overlay.
			markDone();
			return;
		}
		// Every step must have its target mounted, otherwise the tour
		// stalls on a missing element with the overlay still blocking.
		const missing = REQUIRED_TARGETS.filter(
			(sel) => !document.querySelector(sel),
		);
		if (missing.length > 0 && !force) {
			markDone();
			return;
		}
		startedRef.current = true;
		onEnsureSidebarOpen?.();
		// Defer one tick so target elements are mounted.
		const id = window.setTimeout(() => startOnborda(CHAT_TOUR_NAME), 300);
		return () => window.clearTimeout(id);
	}, [ready, agentsCount, sessionsCount, onEnsureSidebarOpen, startOnborda]);

	// Escape closes the tour at any step: an always-available exit means the
	// overlay can never permanently lock the page.
	useEffect(() => {
		const onKey = (e: KeyboardEvent) => {
			if (e.key !== 'Escape') return;
			if (currentTour !== CHAT_TOUR_NAME) return;
			markDone();
			closeOnborda();
		};
		window.addEventListener('keydown', onKey);
		return () => window.removeEventListener('keydown', onKey);
	}, [currentTour, closeOnborda]);

	// Snapshot the agents/sessions count when entering each step so we can
	// detect "user just created one" rather than "they already had some."
	useEffect(() => {
		if (currentTour !== CHAT_TOUR_NAME) return;
		if (currentStep === 0) startCountsRef.current.agents = agentsCount;
		if (currentStep === 1) startCountsRef.current.sessions = sessionsCount;
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [currentStep, currentTour]);

	// Step 0 → 1: agent created
	useEffect(() => {
		if (currentTour !== CHAT_TOUR_NAME || currentStep !== 0) return;
		if (agentsCount > startCountsRef.current.agents) setCurrentStep(1);
	}, [agentsCount, currentStep, currentTour, setCurrentStep]);

	// Step 1 → 2: session created
	useEffect(() => {
		if (currentTour !== CHAT_TOUR_NAME || currentStep !== 1) return;
		if (sessionsCount > startCountsRef.current.sessions) setCurrentStep(2);
	}, [sessionsCount, currentStep, currentTour, setCurrentStep]);

	return null;
};
