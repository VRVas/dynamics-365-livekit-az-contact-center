'use client';

import { useState } from 'react';
import { motion } from 'framer-motion';
import { useAgentStore } from '@/stores/agentStore';
import { useRouter } from 'next/navigation';
import { template } from '@/config/template';
import { Brand, ConsoleUnavailable, LanguageButton } from '@/components/Brand';
import { useLocale } from '@/i18n/LocaleProvider';

export default function LoginPage() {
  return template.features.chat && template.features.agentConsole ? <LoginForm /> : <ConsoleUnavailable />;
}

function LoginForm() {
  const { locale, t } = useLocale();
  const [agentId, setAgentId] = useState('');
  const [agentName, setAgentName] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const { login } = useAgentStore();
  const router = useRouter();

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (!agentId.trim() || !agentName.trim()) {
      setError(t('Please enter both Agent ID and Name'));
      return;
    }

    setIsLoading(true);
    try {
      login(agentId.trim(), agentName.trim());
      router.push('/dashboard');
    } catch (err) {
      setError(t('Login failed. Please try again.'));
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center overflow-hidden bg-ink-950">
      {/* animated command-center backdrop */}
      <div className="absolute inset-0 cc-grid opacity-60" />
      <div className="absolute -top-40 -right-40 w-[40rem] h-[40rem] rounded-full bg-brand-600/20 blur-3xl" />
      <div className="absolute -bottom-48 -left-40 w-[36rem] h-[36rem] rounded-full bg-brand-700/10 blur-3xl" />

      <motion.div
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.5, ease: 'easeOut' }}
        className="relative glass rounded-3xl shadow-panel p-8 w-full max-w-md mx-4"
      >
        <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-transparent via-brand-600 to-transparent rounded-t-3xl" />

        {/* Header */}
        <div className="flex items-center justify-between gap-3 mb-6">
          <Brand />
          <LanguageButton />
        </div>
        <h2 className="text-xl font-semibold mb-3">{template.welcome.title[locale]}</h2>
        {template.welcome.description[locale] && <p className="text-white/60 mb-4">{template.welcome.description[locale]}</p>}

        {/* Login form */}
        <form onSubmit={handleLogin} className="space-y-4">
          <div>
            <label htmlFor="agentId" className="block text-xs font-medium text-white/50 mb-1.5 uppercase tracking-wider">
              {t('Agent ID')}
            </label>
            <input
              id="agentId"
              type="text"
              value={agentId}
              onChange={(e) => setAgentId(e.target.value)}
              placeholder={t('Enter your agent ID')}
              dir="auto"
              className="w-full px-4 py-3 rounded-xl bg-ink-800/80 border border-white/10 text-white placeholder-white/25 focus:ring-2 focus:ring-brand-600/50 focus:border-transparent outline-none transition"
            />
          </div>

          <div>
            <label htmlFor="agentName" className="block text-xs font-medium text-white/50 mb-1.5 uppercase tracking-wider">
              {t('Your name')}
            </label>
            <input
              id="agentName"
              type="text"
              value={agentName}
              onChange={(e) => setAgentName(e.target.value)}
              placeholder={t('Enter your name')}
              dir="auto"
              className="w-full px-4 py-3 rounded-xl bg-ink-800/80 border border-white/10 text-white placeholder-white/25 focus:ring-2 focus:ring-brand-600/50 focus:border-transparent outline-none transition"
            />
          </div>

          {error && (
            <motion.div
              initial={{ opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              role="alert"
              className="glass-brand text-brand-200 px-4 py-2.5 rounded-lg text-sm"
            >
              {error}
            </motion.div>
          )}

          <motion.button
            whileHover={{ scale: isLoading ? 1 : 1.02 }}
            whileTap={{ scale: isLoading ? 1 : 0.98 }}
            type="submit"
            disabled={isLoading}
            className="w-full btn-brand text-white font-semibold py-3 px-4 rounded-xl shadow-glow-brand-sm transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isLoading ? t('Signing in…') : t('Enter console')}
          </motion.button>
        </form>

        <p className="text-center text-white/25 text-xs mt-6 uppercase tracking-wider">
          {t('Agent support')}
        </p>
      </motion.div>
    </div>
  );
}
