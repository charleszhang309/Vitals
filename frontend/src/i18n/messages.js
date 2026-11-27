import chinese from './zh-CN.js';
import english from './en-US.js';
import serverEnglish, { translateServerPattern } from './server-en-US.js';

export const languageKey = 'vitals.language';
const catalogs = { 'zh-CN': chinese, 'en-US': english };

export function validPreference(value) {
  return Object.hasOwn(catalogs, value) ? value : 'auto';
}

export function resolveLocale(preference, languages = globalThis.navigator?.languages ?? []) {
  const explicit = validPreference(preference);
  if (explicit !== 'auto') return explicit;
  for (const language of languages) {
    if (/^zh(?:-|$)/i.test(language)) return 'zh-CN';
    if (/^en(?:-|$)/i.test(language)) return 'en-US';
  }
  return 'en-US';
}

export function storedLanguage() {
  try {
    return validPreference(globalThis.localStorage?.getItem(languageKey));
  } catch {
    return 'auto';
  }
}

export function translate(locale, key, params) {
  if (typeof key !== 'string') return key;
  const catalog = catalogs[locale] ?? chinese;
  const fallback = locale === 'en-US' && !Object.hasOwn(catalog, key)
    ? (Object.hasOwn(serverEnglish, key) ? serverEnglish[key] : translateServerPattern(key)) : key;
  const message = Object.hasOwn(catalog, key) ? catalog[key] : fallback;
  return params ? message.replace(/\{(\w+)\}/g, (match, name) => Object.hasOwn(params, name) ? String(params[name]) : match) : message;
}

let locale = resolveLocale(storedLanguage());

export function setLocale(value) { locale = value; }
export function getLocale() { return locale; }
export function t(key, params) { return translate(locale, key, params); }

const formatters = new Map();

function byLocale(Formatter, options) {
  function formatter() {
    const key = JSON.stringify([Formatter.name, locale, options]);
    if (!formatters.has(key)) formatters.set(key, new Formatter(locale, options));
    return formatters.get(key);
  }
  return {
    format: (value) => formatter().format(value),
    formatToParts: (value) => formatter().formatToParts(value),
  };
}

export function localizedDate(options) { return byLocale(Intl.DateTimeFormat, options); }
export function localizedNumber(options) { return byLocale(Intl.NumberFormat, options); }
