'use client';

import Image from 'next/image';
import { useLocale } from '@/i18n/LocaleProvider';
import { template } from '@/config/template';
import { ProfileSelector } from '@/components/chat/ProfileSelector';

export function Header() {
  const { locale, setLocale, t } = useLocale();
  const nextLocale = template.locale.supported.find((value) => value !== locale);

  return (
    <header className="px-3 pt-3">
      <div className="brand-header rounded-2xl px-4 py-2.5 flex flex-wrap items-center justify-between gap-3">
        {/* Brand */}
        <div className="flex min-w-0 items-center gap-3">
          {template.brand.logoUrl && <div className="shrink-0">
            <Image
              src={template.brand.logoUrl}
              alt={template.brand.name[locale]}
              width={146}
              height={46}
              unoptimized
              className="h-auto w-32 object-contain sm:w-[146px]"
            />
          </div>}
          <div className="min-w-0 leading-relaxed">
            <h1 className="text-sm font-semibold text-slate-950 sm:text-base">{template.brand.name[locale]}</h1>
            <p className="text-[11px] text-slate-800">{template.brand.assistantName[locale]}</p>
          </div>
        </div>

        {/* Right: profile + new conversation */}
        <div className="brand-header-actions ms-auto flex max-w-full flex-wrap items-center gap-2 rounded-xl border border-white/10 bg-slate-950/80 p-1.5">
          {nextLocale && <button
            type="button"
            onClick={() => setLocale(nextLocale)}
            aria-label={t('Change language')}
            lang={nextLocale}
            className="shrink-0 rounded-lg border border-white/25 px-3 py-1.5 text-sm text-white hover:bg-white/10"
          >
            {nextLocale === 'ar' ? 'العربية' : 'English'}
          </button>}
          {template.features.chat && template.features.profiles && <ProfileSelector />}
          {template.features.chat && <button
            onClick={() => window.location.reload()}
            className="shrink-0 p-2 rounded-xl text-white/85 hover:text-white hover:bg-white/10 transition-colors"
            title={t('New conversation')}
            aria-label={t('New conversation')}
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
          </button>}
        </div>
      </div>
    </header>
  );
}
