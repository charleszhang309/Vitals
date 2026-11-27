import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useState } from 'react';
import { languageKey, resolveLocale, setLocale, storedLanguage, translate, validPreference } from './messages.js';

const I18nContext = createContext(null);

export function I18nProvider({ children }) {
  const [language, setPreference] = useState(storedLanguage);
  const [locale, setLanguageLocale] = useState(() => resolveLocale(language));

  const setLanguage = useCallback((value) => {
    const preference = validPreference(value);
    const resolved = resolveLocale(preference);
    try { window.localStorage.setItem(languageKey, preference); } catch { /* Storage may be disabled. */ }
    setLocale(resolved);
    setPreference(preference);
    setLanguageLocale(resolved);
  }, []);

  useEffect(() => {
    function syncSystem() {
      if (language !== 'auto') return;
      const resolved = resolveLocale(language);
      setLocale(resolved);
      setLanguageLocale(resolved);
    }
    window.addEventListener('languagechange', syncSystem);
    return () => window.removeEventListener('languagechange', syncSystem);
  }, [language]);

  useLayoutEffect(() => { document.documentElement.lang = locale; }, [locale]);
  useEffect(() => {
    const invoke = window.__TAURI__?.core?.invoke;
    if (typeof invoke === 'function') {
      invoke('set_desktop_language', { language: language === 'en-US' ? 'en' : language }).catch(() => {});
    }
  }, [language]);

  const value = useMemo(() => ({ language, locale, setLanguage, t: (key, params) => translate(locale, key, params) }),
    [language, locale, setLanguage]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() { return useContext(I18nContext); }
