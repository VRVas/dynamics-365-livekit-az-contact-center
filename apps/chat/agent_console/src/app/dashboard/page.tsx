'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { motion } from 'framer-motion';
import { useAgentStore } from '@/stores/agentStore';
import { useAgentWebSocket } from '@/hooks/useAgentWebSocket';
import QueuePanel from '@/components/QueuePanel';
import ChatPanel from '@/components/ChatPanel';
import Header from '@/components/Header';
import { ConsoleUnavailable } from '@/components/Brand';
import { template } from '@/config/template';
import { API_BASE_URL } from '@/config/endpoints';
import { useLocale } from '@/i18n/LocaleProvider';

export default function DashboardPage() {
  return template.features.chat && template.features.agentConsole ? <ChatDashboard /> : <ConsoleUnavailable />;
}

function ChatDashboard() {
  const { locale, t } = useLocale();
  const router = useRouter();
  const { isLoggedIn, agentId, agentName, activeSession, logout } = useAgentStore();
  const [mounted, setMounted] = useState(false);

  // WebSocket connection for agent
  const {
    isConnected,
    queue,
    acceptSession,
    sendMessage,
    completeSession,
  } = useAgentWebSocket({
    agentId: agentId || '',
    enabled: isLoggedIn && !!agentId,
  });

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (mounted && !isLoggedIn) {
      router.push('/');
    }
  }, [mounted, isLoggedIn, router]);

  if (!mounted || !isLoggedIn) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-ink-950">
        <div className="relative">
          <div className="absolute inset-0 rounded-full border border-brand-600/40 animate-ring-expand" />
          <div className="animate-spin rounded-full h-8 w-8 border-2 border-ink-700 border-t-brand-500" />
        </div>
      </div>
    );
  }

  const handleLogout = () => {
    logout();
    router.push('/');
  };

  return (
    <div className="min-h-screen bg-ink-950 flex flex-col" data-layout={template.brand.layout}>
      {/* Header */}
      <Header
        agentName={agentName || t('Agent')}
        isConnected={isConnected}
        onLogout={handleLogout}
      />
      {!API_BASE_URL && <p role="status" className="px-6 py-2 text-sm text-amber-200">{t('Chat service is not configured.')}</p>}

      {/* Main content */}
      <div className="flex-1 flex flex-col md:flex-row min-h-0 p-3 gap-3">
        {/* Queue panel — left */}
        <motion.aside
          initial={{ x: -24, opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          transition={{ duration: 0.4, ease: 'easeOut' }}
          className={`w-full md:w-72 lg:w-80 xl:w-96 shrink-0 glass rounded-2xl overflow-hidden flex flex-col ${template.brand.layout === 'chat-first' ? 'order-2' : ''}`}
        >
          <QueuePanel
            queue={queue}
            onAccept={acceptSession}
            activeSessionId={activeSession?.sessionId}
          />
        </motion.aside>

        {/* Chat panel — right */}
        <motion.main
          initial={{ opacity: 0, scale: 0.99 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.4, ease: 'easeOut', delay: 0.05 }}
          className="flex-1 min-h-[400px] min-w-0 glass rounded-2xl overflow-hidden flex flex-col"
        >
          {activeSession ? (
            <ChatPanel
              session={activeSession}
              onSendMessage={sendMessage}
              onComplete={completeSession}
            />
          ) : (
            <div className="flex-1 flex items-center justify-center cc-grid">
              <div className="text-center text-white/40 px-6">
                <div className="relative mx-auto mb-5 w-20 h-20">
                  <span className="absolute inset-0 rounded-2xl border border-brand-600/30 animate-ring-expand" />
                  <div className="relative w-20 h-20 rounded-2xl bg-ink-800/70 border border-white/5 flex items-center justify-center">
                    <svg className="w-9 h-9 text-brand-500/80" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                    </svg>
                  </div>
                </div>
                <h2 className="text-xl font-semibold text-white/90">{template.welcome.title[locale]}</h2>
                {template.welcome.description[locale] && <p className="mt-2">{template.welcome.description[locale]}</p>}
                <p className="text-lg font-medium text-white/70 mt-3">{t('No active conversation')}</p>
                <p className="text-sm mt-1 text-white/40">{t('Accept a conversation from the queue to begin.')}</p>
              </div>
            </div>
          )}
        </motion.main>
      </div>
    </div>
  );
}
