'use client';

import { useState } from 'react';
import { useLocale } from '@/i18n/LocaleProvider';

export function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const { t } = useLocale();
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setFailed(false);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch (error) {
      console.error('Code copy failed', error);
      setFailed(true);
    }
  };

  return (
    <div className="code-card my-3">
      <div className="code-card-head">
        <span className="code-lang">{lang || t('text')}</span>
        <button onClick={copy} className="code-copy" type="button">
          {copied ? (
            <>
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
              </svg>
              {t('Copied')}
            </>
          ) : (
            <>
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
              {t('Copy')}
            </>
          )}
        </button>
      </div>
      {failed && <p role="alert" className="text-xs text-red-300">{t('Copy failed')}</p>}
      <pre className="code-card-body" dir="ltr">
        <code>{code}</code>
      </pre>
    </div>
  );
}
