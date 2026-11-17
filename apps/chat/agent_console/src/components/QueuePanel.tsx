'use client';

import { AnimatePresence, motion } from 'framer-motion';
import { useLocale } from '@/i18n/LocaleProvider';
import type { MessageKey } from '@/i18n/messages';

interface QueueEntry {
  sessionId: string;
  reason?: string;
  waitingTime: number;
  queuedAt: Date;
}

interface QueuePanelProps {
  queue: QueueEntry[];
  onAccept: (sessionId: string) => void;
  activeSessionId?: string;
}

function formatWaitTime(seconds: number, t: (key: MessageKey, values?: Record<string, string | number>) => string): string {
  if (seconds < 60) return t('{seconds}s', { seconds });
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return t('{minutes}m {seconds}s', { minutes: mins, seconds: secs });
}

// Wait-time urgency: green < 30s, amber < 90s, red beyond.
function urgency(seconds: number) {
  if (seconds < 30) return { ring: 'border-green-500/30', text: 'text-green-400', dot: 'bg-green-400' };
  if (seconds < 90) return { ring: 'border-amber-500/30', text: 'text-amber-400', dot: 'bg-amber-400' };
  return { ring: 'border-red-600/50', text: 'text-red-400', dot: 'bg-red-500' };
}

export default function QueuePanel({ queue, onAccept, activeSessionId }: QueuePanelProps) {
  const { t } = useLocale();
  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-4 py-4 border-b border-white/5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h2 className="font-semibold text-white tracking-wide text-sm uppercase">{t('Waiting queue')}</h2>
          </div>
          <motion.span
            key={queue.length}
            initial={{ scale: 0.6, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            className={`min-w-[26px] text-center text-xs font-bold px-2 py-1 rounded-full ${
              queue.length > 0 ? 'btn-brand text-white shadow-glow-brand-sm' : 'bg-ink-700 text-white/40'
            }`}
          >
            {queue.length}
          </motion.span>
        </div>
      </div>

      {/* Queue list */}
      <div className="flex-1 overflow-y-auto px-3 py-3">
        {queue.length === 0 ? (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="h-full flex flex-col items-center justify-center text-center text-white/30 px-6"
          >
            <div className="w-14 h-14 rounded-2xl bg-ink-800/60 border border-white/5 flex items-center justify-center mb-3">
              <svg className="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
              </svg>
            </div>
            <p className="text-sm font-medium text-white/50">{t('All clear')}</p>
            <p className="text-xs mt-1">{t('No conversations are waiting.')}</p>
          </motion.div>
        ) : (
          <div className="space-y-2.5">
            <AnimatePresence initial={false}>
              {queue.map((entry, index) => {
                const u = urgency(entry.waitingTime);
                const isActive = activeSessionId === entry.sessionId;
                return (
                  <motion.div
                    key={entry.sessionId}
                    layout
                    initial={{ opacity: 0, x: -16, scale: 0.97 }}
                    animate={{ opacity: 1, x: 0, scale: 1 }}
                    exit={{ opacity: 0, x: 16, scale: 0.95 }}
                    transition={{ duration: 0.25, delay: index * 0.04 }}
                    className={`rounded-xl p-3.5 bg-ink-800/60 border ${u.ring} ${
                      isActive ? 'ring-1 ring-brand-600/60' : ''
                    } hover:bg-ink-750/80 transition-colors`}
                  >
                    <div className="flex items-start justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className="w-6 h-6 rounded-lg bg-ink-700 border border-white/5 flex items-center justify-center text-[11px] font-bold text-white/60">
                          {index + 1}
                        </span>
                        <span className="font-medium text-white text-sm">
                          #{entry.sessionId.slice(-6)}
                        </span>
                      </div>
                      <span className={`flex items-center gap-1.5 text-xs font-semibold ${u.text}`}>
                        <span className={`w-1.5 h-1.5 rounded-full ${u.dot} animate-pulse`} />
                        {formatWaitTime(entry.waitingTime, t)}
                      </span>
                    </div>

                    {entry.reason && (
                      <p dir="auto" className="text-xs text-white/50 mb-3 line-clamp-2 leading-relaxed">{entry.reason}</p>
                    )}

                    <motion.button
                      whileHover={{ scale: activeSessionId ? 1 : 1.02 }}
                      whileTap={{ scale: activeSessionId ? 1 : 0.98 }}
                      onClick={() => onAccept(entry.sessionId)}
                      disabled={!!activeSessionId}
                      className={`w-full py-2 rounded-lg text-sm font-semibold transition ${
                        activeSessionId
                          ? 'bg-ink-700 text-white/30 cursor-not-allowed'
                          : 'btn-brand text-white shadow-glow-brand-sm'
                      }`}
                    >
                      {activeSessionId ? t('Busy') : t('Accept handover')}
                    </motion.button>
                  </motion.div>
                );
              })}
            </AnimatePresence>
          </div>
        )}
      </div>

      {/* Footer stats */}
      <div className="px-4 py-3 border-t border-white/5 bg-ink-900/40">
        <div className="flex items-center justify-between text-xs">
          <span className="text-white/40 uppercase tracking-wider">{t('Average wait')}</span>
          <span className="font-mono font-medium text-white/70 tabular-nums">
            {queue.length > 0
              ? formatWaitTime(Math.round(queue.reduce((acc, e) => acc + e.waitingTime, 0) / queue.length), t)
              : '\u2014'}
          </span>
        </div>
      </div>
    </div>
  );
}
