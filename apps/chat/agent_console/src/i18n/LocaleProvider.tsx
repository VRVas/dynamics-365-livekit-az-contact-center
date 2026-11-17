'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore } from 'react';
import { DEFAULT_LOCALE, isSupportedLocale, template, type Locale } from '@/config/template';
import { translate, type MessageKey } from './messages';

const storageKey = 'contact-center-agent-language';
let currentLocale: Locale = DEFAULT_LOCALE;
let loaded = false;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (!loaded) {
    loaded = true;
    try {
      const stored = window.localStorage.getItem(storageKey);
      if (isSupportedLocale(stored)) currentLocale = stored;
    } catch (error) {
      console.warn('Language preference could not be loaded', error);
    }
    notify();
  }
  const onStorage = (event: StorageEvent) => {
    if (event.key === storageKey || event.key === null) {
      currentLocale = isSupportedLocale(event.newValue) ? event.newValue : DEFAULT_LOCALE;
      notify();
    }
  };
  window.addEventListener('storage', onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', onStorage);
  };
}

const LocaleContext = createContext<{
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, values?: Record<string, string | number>) => string;
} | null>(null);

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const locale = useSyncExternalStore(subscribe, () => currentLocale, () => DEFAULT_LOCALE);
  const setLocale = useCallback((next: Locale) => {
    if (!isSupportedLocale(next)) return;
    currentLocale = next;
    notify();
    try {
      window.localStorage.setItem(storageKey, next);
    } catch (error) {
      console.warn('Language preference could not be saved', error);
    }
  }, []);
  useEffect(() => {
    document.documentElement.lang = locale;
    document.documentElement.dir = locale === 'ar' ? 'rtl' : 'ltr';
    document.title = `${template.brand.name[locale]} | ${translate(locale, 'Agent console')}`;
  }, [locale]);
  const value = useMemo(() => ({
    locale,
    setLocale,
    t: (key: MessageKey, values?: Record<string, string | number>) => translate(locale, key, values),
  }), [locale, setLocale]);
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useLocale() {
  const value = useContext(LocaleContext);
  if (!value) throw new Error('useLocale requires LocaleProvider');
  return value;
}
