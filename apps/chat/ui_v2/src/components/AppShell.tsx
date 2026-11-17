'use client';

import { useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Header } from '@/components/layout/Header';
import { ChatContainer } from '@/components/chat';
import { VoiceContainer } from '@/components/voice/VoiceContainer';
import { useLocale } from '@/i18n/LocaleProvider';
import { template } from '@/config/template';

export function AppShell() {
  const { locale, t } = useLocale();
  const { chat, voice, phone } = template.features;
  const hasCalls = voice || phone;
  const voiceFirst = template.brand.layout === 'voice-first';
  const CallPanel = chat ? motion.aside : motion.main;
  const [voiceOpen, setVoiceOpen] = useState(template.brand.layout !== 'chat-first' || !chat);
  // Stacked on small screens, a voice-first panel keeps its content height up to the
  // visible area, so the chat below can't squeeze the voice controls out of view.
  const callPanelLayout = !chat ? 'flex-1'
    : voiceFirst ? 'shrink-0 max-h-full md:flex-1 md:max-h-none'
      : 'md:w-[360px] shrink-0 max-h-[50vh] md:max-h-none';

  return (
    <div className="relative z-10 h-screen flex flex-col" data-layout={template.brand.layout}>
      <Header />
      {!chat && <div className="px-6 pt-4 text-center">
        <h2 className="text-xl font-semibold">{template.welcome.title[locale]}</h2>
        {template.welcome.description[locale] && <p className="text-white/70">{template.welcome.description[locale]}</p>}
      </div>}
      <div className="flex-1 min-h-0 flex flex-col md:flex-row gap-3 p-3 overflow-y-auto">
        {/* Voice panel — collapsible */}
        <AnimatePresence initial={false}>
          {hasCalls && voiceOpen && (
            <CallPanel
              key="voice"
              aria-label={t('Call controls')}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
              className={`min-h-0 ${callPanelLayout}`}
            >
              <VoiceContainer onCollapse={chat ? () => setVoiceOpen(false) : undefined} />
            </CallPanel>
          )}
        </AnimatePresence>

        {/* Slim rail to reopen the voice panel */}
        <AnimatePresence initial={false}>
          {hasCalls && !voiceOpen && (
            <motion.button
              key="voice-rail"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
              onClick={() => setVoiceOpen(true)}
              aria-label={t('Open call panel')}
              title={t('Open call panel')}
              className="group flex md:w-[52px] shrink-0 min-h-0 glass rounded-2xl flex-row md:flex-col items-center justify-center gap-3 py-3 text-white/55 hover:text-white overflow-hidden"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11a7 7 0 01-14 0M12 18v3m0-3a4 4 0 01-4-4V7a4 4 0 118 0v7a4 4 0 01-4 4z" />
              </svg>
              <span className="text-[11px] uppercase tracking-[0.2em]">{t(voice ? 'Voice' : 'Call a phone')}</span>
              <svg className="w-4 h-4 opacity-60 rtl:rotate-180 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
              </svg>
            </motion.button>
          )}
        </AnimatePresence>

        {/* Chat panel — fills remaining space */}
        {chat && <motion.main
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1], delay: 0.05 }}
          className={`min-w-0 min-h-[300px] md:min-h-0 ${hasCalls && voiceOpen && voiceFirst ? 'md:w-[40%] shrink-0 flex-1 md:flex-none' : 'flex-1'}`}
        >
          <ChatContainer />
        </motion.main>}
        {!chat && !hasCalls && <main className="flex-1 glass rounded-2xl flex items-center justify-center">
          <p role="status">{t('No channels are enabled.')}</p>
        </main>}
      </div>
    </div>
  );
}
