'use client';

import { template } from '@/config/template';
import { useLocale } from '@/i18n/LocaleProvider';
import { BrowserVoice } from './BrowserVoice';
import { PhoneDialer } from './PhoneDialer';

export function VoiceContainer({ onCollapse }: { onCollapse?: () => void }) {
  const { t } = useLocale();
  if (!template.features.voice && !template.features.phone) return null;

  return (
    <div className="glass rounded-2xl h-full min-h-0 flex flex-col items-center gap-5 p-5 pt-10 text-center overflow-y-auto relative">
      {onCollapse && <button onClick={onCollapse} aria-label={t('Collapse call panel')} title={t('Collapse')}
        className="absolute top-3 end-3 z-10 p-1.5 rounded-lg text-white/60 hover:text-white hover:bg-white/10">
        <svg className="w-4 h-4 rtl:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
        </svg>
      </button>}
      {template.features.voice && <BrowserVoice />}
      {template.features.phone && <PhoneDialer />}
    </div>
  );
}
