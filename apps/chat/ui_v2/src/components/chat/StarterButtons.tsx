'use client';

import { motion, type Variants } from 'framer-motion';
import { template } from '@/config/template';
import { useLocale } from '@/i18n/LocaleProvider';

interface StarterButtonsProps {
  onSelect: (message: string) => void;
  disabled?: boolean;
}

const container: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.05, delayChildren: 0.1 } },
};
const item: Variants = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: { type: 'spring', stiffness: 300, damping: 26 } },
};

export function StarterButtons({ onSelect, disabled }: StarterButtonsProps) {
  const { locale } = useLocale();
  return (
    <div className="flex flex-col items-center justify-center min-h-full py-10 px-4">
      {/* Hero */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
        className="text-center mb-10"
      >
        <h2 className="text-3xl sm:text-4xl font-semibold mb-3">
          <span className="text-gradient">{template.welcome.title[locale]}</span>
        </h2>
        {template.welcome.description[locale] && (
          <p className="text-white/70 max-w-md mx-auto">{template.welcome.description[locale]}</p>
        )}
      </motion.div>

      {/* Starter cards */}
      {template.quickActions.length > 0 && <motion.div
        variants={container}
        initial="hidden"
        animate="show"
        className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 max-w-4xl w-full"
      >
        {template.quickActions.map((starter) => (
          <motion.button
            key={starter.id}
            variants={item}
            whileHover={disabled ? undefined : { y: -3, scale: 1.015 }}
            whileTap={disabled ? undefined : { scale: 0.98 }}
            onClick={() => !disabled && onSelect(starter.prompt[locale])}
            disabled={disabled}
            className="glass text-start px-4 py-3.5 rounded-2xl text-sm font-medium text-white/85 hover:text-white transition-colors disabled:opacity-50"
          >
            {starter.label[locale]}
          </motion.button>
        ))}
      </motion.div>}
    </div>
  );
}
