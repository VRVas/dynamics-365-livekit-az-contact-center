'use client';

import { useState, useRef, KeyboardEvent } from 'react';
import { motion } from 'framer-motion';
import { useLocale } from '@/i18n/LocaleProvider';

interface ChatInputProps {
  onSend: (message: string) => void;
  disabled?: boolean;
  isStreaming?: boolean;
  onStop?: () => void;
}

export function ChatInput({ onSend, disabled, isStreaming, onStop }: ChatInputProps) {
  const { t } = useLocale();
  const [input, setInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = () => {
    if (!input.trim() || disabled) return;
    onSend(input.trim());
    setInput('');

    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  // Auto-resize textarea
  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const textarea = e.target;
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px';
  };

  const canSend = !!input.trim() && !disabled;

  return (
    <div className="w-full">
      <div className="glass-strong flex items-end gap-2 rounded-[1.4rem] p-2 ps-4 focus-within:ring-1 focus-within:ring-emerald-400">
        {/* Textarea */}
        <textarea
          ref={textareaRef}
          value={input}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={t('Type a message…')}
          aria-label={t('Type a message…')}
          dir="auto"
          disabled={disabled}
          rows={1}
          className="flex-1 bg-transparent text-white placeholder-white/40 resize-none outline-none text-sm leading-relaxed max-h-[150px] py-2"
        />

        {/* Send/Stop button */}
        {isStreaming ? (
          <motion.button
            whileTap={{ scale: 0.9 }}
            onClick={onStop}
            className="p-2.5 rounded-2xl bg-red-500/90 hover:bg-red-500 transition-colors"
            title={t('Stop generating')}
            aria-label={t('Stop generating')}
          >
            <svg className="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24">
              <rect x="6" y="6" width="12" height="12" rx="2" />
            </svg>
          </motion.button>
        ) : (
          <motion.button
            whileHover={canSend ? { scale: 1.06 } : undefined}
            whileTap={canSend ? { scale: 0.9 } : undefined}
            onClick={handleSend}
            disabled={!canSend}
            className={`p-2.5 rounded-2xl transition-colors ${
              canSend ? 'btn-gradient' : 'bg-white/10 text-white/40 cursor-not-allowed'
            }`}
            title={t('Send message')}
            aria-label={t('Send message')}
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
            </svg>
          </motion.button>
        )}
      </div>

      {/* Footer text */}
      <p className="text-center text-[11px] text-white/30 mt-2">
        {t('Check the cited official sources for current requirements.')}
      </p>
    </div>
  );
}
