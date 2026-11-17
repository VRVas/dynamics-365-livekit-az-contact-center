'use client';

import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { VOICE_BASE_URL, VOICES } from '@/constants';
import { useRealtimeVoice } from '@/hooks/useRealtimeVoice';
import { useLocale } from '@/i18n/LocaleProvider';
import type { MessageKey } from '@/i18n/messages';
import { VoiceOrb } from './VoiceOrb';

const VOICE_NAMES: MessageKey[] = ['Nourah', 'Abdullah', 'Sara', 'Abdulaziz', 'Fadi', 'Yassin', 'Youssef'];

export function BrowserVoice() {
  const { locale, t, localize } = useLocale();
  const { status, muted, speaking, error, warning, levelRef, start, stop, toggleMute, setLanguage } = useRealtimeVoice();
  useEffect(() => setLanguage(locale), [locale, setLanguage]);
  const active = status === 'listening' || status === 'connecting';
  const [voice, setVoice] = useState('');
  const statusText = status === 'idle' ? t('Tap to talk')
    : status === 'connecting' ? t('Connecting…')
      : status === 'error' ? t('Unavailable') : speaking ? t('Speaking') : t('Listening');

  return (
    <section aria-label={t('Voice')} className="flex w-full flex-col items-center gap-5">
      <h2 className="text-[11px] uppercase tracking-[0.2em] text-white/60">{t('Voice')}</h2>
      <div className="w-full max-w-[260px]">
        <select value={voice} onChange={(event) => setVoice(event.target.value)} disabled={active}
          aria-label={t('Voice selection')} className="w-full glass-strong rounded-full px-3 py-1.5 text-sm outline-none disabled:opacity-50">
          <option value="" className="bg-zinc-900">{t('Default voice')}</option>
          {VOICES.map((value, index) => <option key={value} value={value} className="bg-zinc-900">{t(VOICE_NAMES[index])}</option>)}
        </select>
        <p className="text-[10px] text-white/50 mt-1">{t('Restart the conversation to change the voice.')}</p>
      </div>
      <motion.button onClick={active ? stop : () => start(voice, locale)} aria-label={active ? t('Stop') : t('Start talking')}
        disabled={!VOICE_BASE_URL}
        className="relative rounded-full outline-none focus-visible:ring-2 focus-visible:ring-white disabled:opacity-50" whileTap={{ scale: 0.96 }}>
        <VoiceOrb levelRef={levelRef} size={180} />
        {!active && <span className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <span className="glass-strong rounded-full px-3 py-1 text-xs">{t('Tap to talk')}</span>
        </span>}
      </motion.button>
      <div role="status" className="glass rounded-full px-4 py-1.5 flex items-center gap-2 text-sm">
        <span className={`w-2 h-2 rounded-full ${active ? 'bg-emerald-400 animate-pulse' : 'bg-white/40'}`} />
        {statusText}
      </div>
      <div className="flex items-center gap-2">
        <button onClick={active ? stop : () => start(voice, locale)} disabled={!VOICE_BASE_URL}
          className={`px-4 py-1.5 rounded-full text-sm font-medium disabled:opacity-50 ${active ? 'bg-red-500/80 hover:bg-red-500' : 'btn-gradient'}`}>
          {active ? t('End') : t('Start')}
        </button>
        {active && <button onClick={toggleMute} aria-pressed={muted} className="px-3 py-1.5 rounded-full text-sm border border-white/25">
          {muted ? t('Muted') : t('Mute')}
        </button>}
      </div>
      {!VOICE_BASE_URL && <p role="status" className="text-xs text-white/70">{t('Voice service is not configured.')}</p>}
      {error && <div role="alert" className="text-xs text-red-300 max-w-[92%]" dir="auto">
        {locale === 'ar' && localize(error) === error ? t('Voice service unavailable. Please try again.') : localize(error)}
      </div>}
      {warning && !error && <div role="status" className="text-xs text-amber-200 max-w-[92%]" dir="auto">
        {locale === 'ar' && localize(warning) === warning ? t('Some optional voice settings were not applied. The conversation can continue.') : localize(warning)}
      </div>}
    </section>
  );
}
