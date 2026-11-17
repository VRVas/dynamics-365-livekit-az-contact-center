'use client';

import { useState } from 'react';
import { VOICE_BASE_URL } from '@/constants';
import { template } from '@/config/template';
import { useLocale } from '@/i18n/LocaleProvider';
import type { MessageKey } from '@/i18n/messages';

type CallMessage = { key: MessageKey; values?: Record<string, string | number> } | { detail: string };
const dialingCode = template.locale.dialingCountryCode;
const initialCountryCode = dialingCode ? `+${dialingCode.replace(/\D/g, '')}` : '';

export function PhoneDialer() {
  const { locale, t, localize } = useLocale();
  const [custCc, setCustCc] = useState(initialCountryCode);
  const [phone, setPhone] = useState('');
  const [opCc, setOpCc] = useState(initialCountryCode);
  const [operator, setOperator] = useState('');
  const [callMsg, setCallMsg] = useState<CallMessage | null>(null);
  const [dialing, setDialing] = useState(false);

  const placeCall = async () => {
    if (!template.features.phone || !VOICE_BASE_URL || !phone.trim() || dialing) return;
    const number = phone.trim().startsWith('+') ? phone.replace(/[^\d+]/g, '') : `${custCc}${phone.replace(/\D/g, '')}`;
    const operatorNumber = operator.trim()
      ? (operator.trim().startsWith('+') ? operator.replace(/[^\d+]/g, '') : `${opCc}${operator.replace(/\D/g, '')}`)
      : undefined;
    if (!/^\+[1-9]\d{6,14}$/.test(number) || (operatorNumber && !/^\+[1-9]\d{6,14}$/.test(operatorNumber))) {
      setCallMsg({ key: 'Enter a valid international phone number.' });
      return;
    }
    setDialing(true);
    setCallMsg(null);
    try {
      const response = await fetch(`${VOICE_BASE_URL}/call`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ number, operator_number: operatorNumber, language: locale }),
      });
      if (!response.ok) {
        const detail = await response.text();
        console.error('Phone call rejected', { status: response.status, detail });
        setCallMsg(detail ? { detail } : { key: 'Call failed (HTTP {status}).', values: { status: response.status } });
      } else {
        setCallMsg(operatorNumber
          ? { key: 'Calling {number}. Human assistance will use {operator}.', values: { number, operator: operatorNumber } }
          : { key: 'Calling {number}. Please answer your phone.', values: { number } });
      }
    } catch (error) {
      console.error('Phone call request failed', error);
      setCallMsg({ key: 'Could not reach the voice backend.' });
    } finally {
      setDialing(false);
    }
  };

  return (
    <section aria-label={t('Call a phone')} className="w-full max-w-[260px] text-start">
      <h2 className="text-xs text-white/70 mb-2">{t('Call a phone')}</h2>
      <details className="mb-3 text-[11px] text-white/60">
        <summary className="cursor-pointer">{t('Call information')}</summary>
        <p className="mt-1">{t('Calls placed here can transfer to the operator number you provide.')}</p>
        <p className="mt-1">{t('Transfer is unavailable for browser conversations and direct inbound calls.')}</p>
      </details>
      <label htmlFor="customer-phone" className="block text-xs text-white/65 mb-1">{t('Customer number')}</label>
      <div className="glass-strong flex items-center gap-1 rounded-full p-2 mb-2.5" dir="ltr">
        <input value={custCc} onChange={(event) => setCustCc(event.target.value)} aria-label={t('Customer country code')}
          placeholder={t('Country code')} inputMode="tel" className="w-20 shrink-0 bg-transparent text-xs outline-none" />
        <input id="customer-phone" value={phone} onChange={(event) => setPhone(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') void placeCall(); }}
          placeholder={t('Phone number')} inputMode="tel" dir="ltr" className="flex-1 min-w-0 bg-transparent text-sm outline-none" />
      </div>
      <label htmlFor="operator-phone" className="block text-xs text-white/65 mb-1">{t('Operator transfer number')}</label>
      <div className="glass-strong flex items-center gap-1 rounded-full p-2 mb-2.5" dir="ltr">
        <input value={opCc} onChange={(event) => setOpCc(event.target.value)} aria-label={t('Operator country code')}
          placeholder={t('Country code')} inputMode="tel" className="w-20 shrink-0 bg-transparent text-xs outline-none" />
        <input id="operator-phone" value={operator} onChange={(event) => setOperator(event.target.value)}
          placeholder={t('Optional transfer number')} inputMode="tel" dir="ltr" className="flex-1 min-w-0 bg-transparent text-sm outline-none" />
      </div>
      <button onClick={placeCall} disabled={!VOICE_BASE_URL || !phone.trim() || dialing}
        className="w-full px-3.5 py-2 rounded-full text-sm btn-gradient disabled:opacity-50">
        {dialing ? t('Connecting…') : t('Place call')}
      </button>
      {!VOICE_BASE_URL && <p role="status" className="text-xs text-white/70 mt-2">{t('Voice service is not configured.')}</p>}
      {callMsg && <div role="status" className="text-xs text-white/70 mt-2" dir="auto">
        {'key' in callMsg ? t(callMsg.key, callMsg.values)
          : locale === 'ar' && localize(callMsg.detail) === callMsg.detail
            ? t('Voice service unavailable. Please try again.') : localize(callMsg.detail)}
      </div>}
    </section>
  );
}
