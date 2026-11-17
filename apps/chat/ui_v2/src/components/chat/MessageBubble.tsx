'use client';

import { useState } from 'react';
import { motion } from 'framer-motion';
import { Message } from '@/types';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useLocale } from '@/i18n/LocaleProvider';
import { InteractiveTable } from './InteractiveTable';
import { CodeBlock } from './CodeBlock';
import { FollowUpChips } from './FollowUpChips';
import { parseMessage } from '@/utils/parseMessage';
import { template } from '@/config/template';

interface MessageBubbleProps {
  message: Message;
  onFollowUp?: (question: string) => void;
  followUpDisabled?: boolean;
}

/* eslint-disable @typescript-eslint/no-explicit-any */
const MD_COMPONENTS = {
  p: ({ children }: any) => <p className="mb-2 last:mb-0">{children}</p>,
  ul: ({ children }: any) => <ul className="md-ul mb-2 space-y-1">{children}</ul>,
  ol: ({ children }: any) => <ol className="md-ol mb-2 space-y-1">{children}</ol>,
  li: ({ children }: any) => <li className="ms-1">{children}</li>,
  strong: ({ children }: any) => <strong className="font-semibold">{children}</strong>,
  em: ({ children }: any) => <em className="italic">{children}</em>,
  del: ({ children }: any) => <del className="opacity-60">{children}</del>,
  a: ({ href, children }: any) => (
    <a href={href} className="text-cyan-300 underline decoration-cyan-300/40 hover:decoration-cyan-300 transition" target="_blank" rel="noopener noreferrer">
      <bdi>{children}</bdi>
    </a>
  ),
  h1: ({ children }: any) => <h1 className="text-lg font-bold mb-2 mt-1">{children}</h1>,
  h2: ({ children }: any) => <h2 className="text-base font-bold mb-2 mt-1">{children}</h2>,
  h3: ({ children }: any) => <h3 className="text-sm font-bold mb-1 mt-1">{children}</h3>,
  blockquote: ({ children }: any) => <blockquote className="md-quote">{children}</blockquote>,
  hr: () => <hr className="my-3 border-white/10" />,
  input: ({ checked }: any) => (
    <input type="checkbox" checked={!!checked} readOnly className="md-task me-1.5 align-middle" />
  ),
  table: ({ node }: any) => <InteractiveTable node={node} />,
  pre: ({ children }: any) => <>{children}</>,
  code: ({ className, children }: any) => {
    const text = String(children ?? '');
    const langMatch = /language-([\w-]+)/.exec(className || '');
    const isBlock = !!langMatch || text.includes('\n');
    if (isBlock) return <CodeBlock code={text.replace(/\n$/, '')} lang={langMatch?.[1]} />;
    return <code dir="ltr" className="px-1.5 py-0.5 rounded bg-white/10 text-[0.85em] font-mono text-emerald-200">{children}</code>;
  },
};

export function MessageBubble({ message, onFollowUp, followUpDisabled }: MessageBubbleProps) {
  const { locale, t, localize } = useLocale();
  const isUser = message.role === 'user';
  const isStreaming = message.isStreaming;
  const isHuman = message.isFromHuman;
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  const { body, followUps } = isUser ? { body: message.content, followUps: [] } : parseMessage(message.content);
  const showFollowUps = !isUser && !isStreaming && followUps.length > 0 && !!onFollowUp;

  const copyMessage = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopyFailed(false);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch (error) {
      console.error('Message copy failed', error);
      setCopyFailed(true);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 14, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: 'spring', stiffness: 380, damping: 30 }}
      className={`group flex w-full mb-4 ${isUser ? 'justify-end' : 'justify-start'}`}
    >
      <div className={`relative max-w-[88%] sm:max-w-[78%] ${isUser ? '' : 'w-full sm:w-auto'}`}>
        <div
          className={`px-4 py-3 rounded-3xl ${
            isUser
              ? 'text-white rounded-ee-md shadow-lg'
              : isHuman
              ? 'rounded-es-md text-white/95 border border-emerald-400/30'
              : 'glass text-white/95 rounded-es-md'
          }`}
          style={
            isUser
              ? { background: 'var(--brand-primary)', color: 'var(--brand-on-primary)' }
              : isHuman
              ? { background: 'linear-gradient(180deg, rgba(16,185,129,0.16), rgba(255,255,255,0.05))' }
              : undefined
          }
        >
          {/* Sender row for assistant / human */}
          {!isUser && (
            <div className="flex items-center gap-2 mb-2 text-xs">
              {isHuman ? (
                <>
                  <span className="flex h-[18px] w-[18px] items-center justify-center rounded-md bg-emerald-500/80 text-[10px] font-bold">
                    {locale === 'ar' ? 'م' : 'OP'}
                  </span>
                  <span className="text-emerald-300/90">{t('Human agent')}</span>
                </>
              ) : (
                <>
                  <span aria-hidden className="h-2 w-2 rounded-full bg-emerald-300" />
                  <span className="text-white/60">{template.brand.assistantName[locale]}</span>
                </>
              )}
            </div>
          )}

          {/* Content */}
          <div dir="auto" className={`md-body text-sm leading-relaxed ${isStreaming ? 'streaming-cursor' : ''}`}>
            {body ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                {message.role === 'system' ? localize(body) : body}
              </ReactMarkdown>
            ) : (
              isStreaming && (
                <span className="flex items-center gap-1.5 py-1">
                  <span className="typing-dot w-2 h-2 bg-white/50 rounded-full" />
                  <span className="typing-dot w-2 h-2 bg-white/50 rounded-full" />
                  <span className="typing-dot w-2 h-2 bg-white/50 rounded-full" />
                </span>
              )
            )}
          </div>

          {/* Follow-up chips */}
          {showFollowUps && <FollowUpChips items={followUps} onSelect={onFollowUp!} disabled={followUpDisabled} />}

          {/* Timestamp */}
          <div className={`text-[11px] mt-2 ${isUser ? 'opacity-65' : 'text-white/35'}`}>
            {(message.timestamp instanceof Date ? message.timestamp : new Date(message.timestamp)).toLocaleTimeString(locale, {
              hour: '2-digit',
              minute: '2-digit',
            })}
          </div>
        </div>

        {/* Hover copy action (assistant messages) */}
        {!isUser && body && !isStreaming && (
          <button
            onClick={copyMessage}
            type="button"
            aria-label={t('Copy message')}
            className="absolute -top-2 -end-2 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity rounded-full p-1.5 glass text-white/70 hover:text-white"
          >
            {copied ? (
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
              </svg>
            ) : (
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
            )}
            {copyFailed && <p role="alert" className="text-xs text-red-300">{t('Copy failed')}</p>}
          </button>
        )}
      </div>
    </motion.div>
  );
}
