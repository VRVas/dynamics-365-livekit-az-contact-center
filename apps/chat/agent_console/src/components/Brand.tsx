'use client';

import Image from 'next/image';
import { template } from '@/config/template';
import { useLocale } from '@/i18n/LocaleProvider';

export function Brand() {
  const { locale, t } = useLocale();
  return (
    <div className="flex min-w-0 items-center gap-3">
      {template.brand.logoUrl && <Image src={template.brand.logoUrl} alt={template.brand.name[locale]}
        width={146} height={46} unoptimized className="h-auto max-h-12 w-28 object-contain" />}
      <div className="min-w-0 leading-tight">
        <h1 className="font-bold text-base text-white">{template.brand.name[locale]}</h1>
        <p className="text-white/60 text-xs">{t('Agent console')}</p>
      </div>
    </div>
  );
}

export function LanguageButton() {
  const { locale, setLocale, t } = useLocale();
  const next = template.locale.supported.find((value) => value !== locale);
  return next ? (
    <button type="button" onClick={() => setLocale(next)} aria-label={t('Change language')} lang={next}
      className="rounded-lg border border-white/25 px-3 py-2 text-sm text-white hover:bg-white/10">
      {next === 'ar' ? 'العربية' : 'English'}
    </button>
  ) : null;
}

export function ConsoleUnavailable() {
  const { locale, t } = useLocale();
  return (
    <main className="min-h-screen flex flex-col items-center justify-center gap-5 p-6 text-center">
      <Brand />
      <LanguageButton />
      <h2 className="text-xl">{template.welcome.title[locale]}</h2>
      {template.welcome.description[locale] && <p>{template.welcome.description[locale]}</p>}
      <p role="status">{t(template.features.chat ? 'Agent console is not enabled.' : 'Chat is not enabled.')}</p>
    </main>
  );
}
