'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { AnimatePresence, motion } from 'framer-motion';
import { template, type Locale } from '@/config/template';
import { useLocale } from '@/i18n/LocaleProvider';

interface ChatMessage {
  id: string;
  content: string;
  sender: 'customer' | 'agent';
  timestamp: Date;
  isHistory?: boolean;
}

interface ActiveSession {
  sessionId: string;
  customerId?: string;
  startedAt: Date;
  messages: ChatMessage[];
}

interface ChatPanelProps {
  session: ActiveSession;
  onSendMessage: (content: string) => void;
  onComplete: () => void;
}

function formatTime(date: Date, locale: Locale): string {
  return new Date(date).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
}

const MD_COMPONENTS = {
  p: ({ children }: any) => <p className="mb-2 last:mb-0 leading-relaxed">{children}</p>,
  ul: ({ children }: any) => <ul className="list-disc list-inside mb-2 space-y-1">{children}</ul>,
  ol: ({ children }: any) => <ol className="list-decimal list-inside mb-2 space-y-1">{children}</ol>,
  li: ({ children }: any) => <li className="ms-1">{children}</li>,
  strong: ({ children }: any) => <strong className="font-semibold">{children}</strong>,
  em: ({ children }: any) => <em className="italic">{children}</em>,
  code: ({ children }: any) => <code className="bg-black/30 px-1.5 py-0.5 rounded text-[0.78em] font-mono">{children}</code>,
  pre: ({ children }: any) => <pre className="bg-black/40 p-2.5 rounded-lg my-2 overflow-x-auto text-xs font-mono border border-white/10">{children}</pre>,
  a: ({ href, children }: any) => <a href={href} className="text-brand-300 underline hover:text-brand-200" target="_blank" rel="noopener noreferrer">{children}</a>,
  h1: ({ children }: any) => <h1 className="text-lg font-bold mb-2">{children}</h1>,
  h2: ({ children }: any) => <h2 className="text-base font-bold mb-2">{children}</h2>,
  h3: ({ children }: any) => <h3 className="text-sm font-bold mb-1">{children}</h3>,
  blockquote: ({ children }: any) => <blockquote className="border-s-2 border-brand-600/60 ps-3 italic my-2 text-white/70">{children}</blockquote>,
};

export default function ChatPanel({ session, onSendMessage, onComplete }: ChatPanelProps) {
  const { locale, t } = useLocale();
  const [input, setInput] = useState('');
  const [confirmComplete, setConfirmComplete] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const nearBottomRef = useRef(true);

  // Live session timer (ticks independent of message polling).
  useEffect(() => {
    const tick = () => setElapsed(Math.round((Date.now() - new Date(session.startedAt).getTime()) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [session.startedAt]);

  // Track whether the operator is pinned to the bottom so polling doesn't yank
  // them away while they read earlier history.
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  useEffect(() => {
    if (nearBottomRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [session.messages]);

  useEffect(() => {
    nearBottomRef.current = true;
    inputRef.current?.focus();
  }, [session.sessionId]);

  const handleSend = () => {
    if (!input.trim()) return;
    nearBottomRef.current = true;
    onSendMessage(input.trim());
    setInput('');
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const durationMins = Math.floor(elapsed / 60);
  const durationSecs = elapsed % 60;

  return (
    <div className="flex flex-col h-full">
      {/* Session header */}
      <div className="px-6 py-3.5 glass border-b border-white/5 flex flex-wrap gap-3 items-center justify-between">
        <div className="flex items-center gap-3.5">
          <div className="relative w-10 h-10 rounded-xl bg-gradient-to-br from-ink-700 to-ink-800 border border-white/10 flex items-center justify-center">
            <svg className="w-5 h-5 text-brand-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
            </svg>
            <span className="absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full bg-green-400 border-2 border-ink-900 shadow-glow-green" />
          </div>
          <div>
            <h3 className="font-semibold text-white">{t('Conversation #{id}', { id: session.sessionId.slice(-6) })}</h3>
            <p className="text-xs text-white/40 font-mono tabular-nums">
              {t('Live for {minutes}m {seconds}s', { minutes: durationMins, seconds: durationSecs.toString().padStart(2, '0') })}
            </p>
          </div>
        </div>

        <AnimatePresence mode="wait">
          {confirmComplete ? (
            <motion.div
              key="confirm"
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="flex items-center gap-2"
            >
              <span className="text-xs text-white/60 me-1">{t('End this handover?')}</span>
              <button
                onClick={() => setConfirmComplete(false)}
                className="px-3 py-2 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-sm text-white/70 transition"
              >
                {t('Cancel')}
              </button>
              <button
                onClick={onComplete}
                className="px-4 py-2 rounded-lg bg-green-600 hover:bg-green-500 text-white text-sm font-medium transition flex items-center gap-2"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                {t('Confirm')}
              </button>
            </motion.div>
          ) : (
            <motion.button
              key="complete"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
              onClick={() => setConfirmComplete(true)}
              className="px-4 py-2 rounded-lg bg-white/5 hover:bg-green-600/20 border border-white/10 hover:border-green-500/40 text-white/80 hover:text-white text-sm font-medium transition flex items-center gap-2"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              {t('Complete handover')}
            </motion.button>
          )}
        </AnimatePresence>
      </div>

      {/* Messages */}
      <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-6 py-6 cc-grid">
        {session.messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center text-white/30">
            <div className="w-12 h-12 rounded-2xl btn-brand flex items-center justify-center mb-3 opacity-80">
              <svg className="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
            </div>
            <p className="text-sm font-medium text-white/50">{t("You're connected")}</p>
            <p className="text-xs mt-1">{t('Send a message to begin.')}</p>
          </div>
        ) : (
          <div className="space-y-3">
            {session.messages.some((m) => m.isHistory) && (
              <div className="flex items-center gap-3 mb-4">
                <div className="flex-1 border-t border-white/5" />
                <span className="text-[11px] text-white/40 bg-ink-800/70 border border-white/5 px-3 py-1 rounded-full uppercase tracking-wider">
                  {t('Conversation with {name}', { name: template.brand.assistantName[locale] })}
                </span>
                <div className="flex-1 border-t border-white/5" />
              </div>
            )}

            {session.messages.map((message, index) => {
              const isAgent = message.sender === 'agent';
              const isLastHistory =
                message.isHistory &&
                (index === session.messages.length - 1 || !session.messages[index + 1]?.isHistory);

              return (
                <div key={message.id}>
                  <motion.div
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.22 }}
                    className={`flex ${isAgent ? 'justify-end' : 'justify-start'}`}
                  >
                    <div
                      className={`max-w-[72%] rounded-2xl px-4 py-2.5 text-sm shadow-panel ${
                        isAgent
                          ? message.isHistory
                            ? 'bg-ink-700/70 text-white/70 border border-white/5 rounded-ee-md'
                            : 'btn-brand text-white rounded-ee-md'
                          : 'bg-ink-800/80 text-white/90 border border-white/5 rounded-es-md'
                      }`}
                    >
                      {message.isHistory && isAgent && (
                        <p className="text-[10px] text-white/40 mb-1 flex items-center gap-1 uppercase tracking-wider">
                          {template.brand.assistantName[locale]}
                        </p>
                      )}
                      <div dir="auto" className="md-body">
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                          {message.content}
                        </ReactMarkdown>
                      </div>
                      <p className={`text-[10px] mt-1 ${isAgent && !message.isHistory ? 'opacity-60' : 'text-white/30'}`}>
                        {message.isHistory ? t('Before handover') : formatTime(message.timestamp, locale)}
                      </p>
                    </div>
                  </motion.div>

                  {isLastHistory && (
                    <div className="flex items-center gap-3 my-4">
                      <div className="flex-1 border-t border-brand-600/30" />
                      <span className="text-[11px] text-brand-300/90 bg-brand-950/40 border border-brand-600/30 px-3 py-1 rounded-full uppercase tracking-wider">
                        {t('You joined')}
                      </span>
                      <div className="flex-1 border-t border-brand-600/30" />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Composer */}
      <div className="p-4 glass border-t border-white/5">
        {template.quickActions.length > 0 && <div role="group" aria-label={t('Suggested replies')} className="flex flex-wrap gap-2 mb-3">
          {template.quickActions.map((action) => <button type="button" key={action.id}
            onClick={() => { setInput(action.prompt[locale]); inputRef.current?.focus(); }}
            className="rounded-full border border-white/20 px-3 py-1 text-xs text-white/80 hover:bg-white/10">
            {action.label[locale]}
          </button>)}
        </div>}
        <div className="flex items-center gap-3">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t('Type a reply…')}
            aria-label={t('Type a reply…')}
            dir="auto"
            className="flex-1 min-w-0 px-4 py-3 rounded-xl bg-ink-800/80 border border-white/10 text-white placeholder-white/30 focus:ring-2 focus:ring-brand-600/50 focus:border-transparent outline-none transition"
          />
          <motion.button
            whileHover={{ scale: input.trim() ? 1.05 : 1 }}
            whileTap={{ scale: input.trim() ? 0.95 : 1 }}
            onClick={handleSend}
            aria-label={t('Send message')}
            disabled={!input.trim()}
            className={`p-3 rounded-xl transition ${
              input.trim() ? 'btn-brand text-white shadow-glow-brand-sm' : 'bg-ink-700 text-white/30 cursor-not-allowed'
            }`}
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
            </svg>
          </motion.button>
        </div>
      </div>
    </div>
  );
}
