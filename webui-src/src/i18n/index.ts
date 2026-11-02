import i18n from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { initReactI18next } from 'react-i18next';

import enTranslations from './locales/en.json';
import zhTranslations from './locales/zh.json';

export const LANGUAGE_PREFERENCE_KEY = 'agentforge-language-selected';
const hasExplicitLanguage = localStorage.getItem(LANGUAGE_PREFERENCE_KEY) === '1';

// Older builds cached browser-detected or accidentally toggled English in
// i18nextLng without recording user intent. Migrate those clients to the
// product default once; later explicit choices remain persistent.
if (!hasExplicitLanguage) localStorage.setItem('i18nextLng', 'zh');

i18n.use(LanguageDetector)
	.use(initReactI18next)
	.init({
		resources: {
			en: { translation: enTranslations },
			zh: { translation: zhTranslations },
		},
		// 产品面向中文用户：默认中文，仅当用户用侧边栏语言按钮显式
		// 切换过（localStorage 缓存 en）才用英文。不再跟随 navigator——
		// 英文系统/无头浏览器首访会把整个界面渲染成英文，违反"全中文
		// 界面"的产品规范（2026-09-04 E2E 发现 TeamFlowPanel 渲染成
		// "Leader / No role descrip"）。
		fallbackLng: 'zh',
		lng: hasExplicitLanguage ? undefined : 'zh',
		interpolation: {
			escapeValue: false,
		},
		detection: {
			order: ['localStorage'],
			caches: ['localStorage'],
		},
	});

export default i18n;
