'use client';

import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Brand, LanguageButton } from './Brand';
import { useLocale } from '@/i18n/LocaleProvider';

interface HeaderProps {
  agentName: string;
  isConnected: boolean;
  onLogout: () => void;
}

export default function Header({ agentName, isConnected, onLogout }: HeaderProps) {
  const { locale, t } = useLocale();
  const [now, setNow] = useState('');

  useEffect(() => {
    const tick = () =>
      setNow(new Date().toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [locale]);

  return (
    <motion.header
      initial={{ y: -24, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ duration: 0.4, ease: 'easeOut' }}
      className="relative z-20 glass border-b border-white/5 px-6 py-3 flex flex-wrap gap-3 items-center justify-between"
    >
      <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-transparent via-brand-600 to-transparent" />

      {/* Brand */}
      <Brand />

      {/* Status cluster */}
      <div className="flex flex-wrap items-center gap-3">
        <LanguageButton />
        <div className="hidden sm:flex flex-col items-end leading-none">
          <span className="font-mono text-sm text-white/80 tabular-nums">{now || '--:--:--'}</span>
          <span className="text-[10px] text-white/30 uppercase tracking-widest">{t('Local')}</span>
        </div>

        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-ink-800/70 border border-white/5">
          <span className="relative flex h-2.5 w-2.5">
            {isConnected && (
              <span className="absolute inline-flex h-full w-full rounded-full bg-green-500/60 animate-ping" />
            )}
            <span
              className={`relative inline-flex rounded-full h-2.5 w-2.5 ${
                isConnected ? 'bg-green-400 shadow-glow-green' : 'bg-brand-500'
              }`}
            />
          </span>
          <span className="text-xs text-white/70">{isConnected ? t('Online') : t('Offline')}</span>
        </div>

        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-full bg-gradient-to-br from-brand-500 to-brand-700 flex items-center justify-center text-sm font-bold shadow-glow-brand-sm">
            {agentName.charAt(0).toUpperCase()}
          </div>
          <div className="hidden md:block leading-tight">
            <p className="text-sm font-medium text-white">{agentName}</p>
            <p className="text-[10px] text-green-400/80 uppercase tracking-wider">{t('Available')}</p>
          </div>
        </div>

        <button
          onClick={onLogout}
          className="px-3.5 py-2 rounded-lg bg-white/5 hover:bg-brand-600/20 border border-white/5 hover:border-brand-600/40 text-sm text-white/70 hover:text-white transition"
        >
          {t('Sign out')}
        </button>
      </div>
    </motion.header>
  );
}
