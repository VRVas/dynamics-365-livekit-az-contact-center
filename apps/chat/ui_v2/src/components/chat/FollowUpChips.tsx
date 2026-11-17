'use client';

import { motion } from 'framer-motion';
import { useLocale } from '@/i18n/LocaleProvider';

interface FollowUpChipsProps {
  items: string[];
  onSelect: (question: string) => void;
  disabled?: boolean;
}

export function FollowUpChips({ items, onSelect, disabled }: FollowUpChipsProps) {
  const { t } = useLocale();
  if (!items.length) return null;

  return (
    <div className="mt-3">
      <div className="text-[11px] uppercase tracking-wider text-white/35 mb-1.5">{t('Suggested follow-ups')}</div>
      <div className="flex flex-wrap gap-2">
        {items.map((q, i) => (
          <motion.button
            key={i}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.04 * i }}
            whileHover={disabled ? undefined : { y: -2 }}
            whileTap={disabled ? undefined : { scale: 0.97 }}
            onClick={() => !disabled && onSelect(q)}
            disabled={disabled}
            className="followup-chip"
            type="button"
          >
            <svg className="w-3.5 h-3.5 shrink-0 opacity-70" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M21 12c0 4.418-4.03 8-9 8a9.86 9.86 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
            </svg>
            <span className="text-start" dir="auto">{q}</span>
          </motion.button>
        ))}
      </div>
    </div>
  );
}
