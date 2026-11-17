'use client';

import { HandoverState } from '@/types';
import { useLocale } from '@/i18n/LocaleProvider';
import { template } from '@/config/template';

interface HandoverBannerProps {
  handover: HandoverState;
  onRequestHandover: () => void;
  onDismiss: () => void;
}

export function HandoverBanner({ handover, onRequestHandover, onDismiss }: HandoverBannerProps) {
  const { t } = useLocale();
  if (!template.features.agentConsole) return null;
  let heading: string;
  let detail: string;
  let dismiss = false;
  let offer = false;
  if (handover.status === 'completed') {
    heading = t('Agent disconnected');
    detail = t('You are now chatting with the assistant again.');
    dismiss = true;
  } else if (handover.humanAgentConnected) {
    heading = t('Connected with {name}', { name: handover.humanAgentName || t('Human agent') });
    detail = t('You are now chatting with a live agent.');
  } else if (handover.isRequested) {
    heading = t('Connecting you with a human agent…');
    detail = handover.queuePosition !== undefined
      ? t('Queue position: {position}', { position: handover.queuePosition })
      : t('Your position in queue will appear shortly.');
  } else if (handover.suggestedReason) {
    heading = t('Would you like to speak with a human agent?');
    detail = handover.suggestedReason;
    offer = true;
  } else {
    return null;
  }
  return (
    <div role="status" className="mx-4 mb-4 p-4 glass rounded-xl border border-emerald-500/30">
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <p className="text-sm font-medium text-emerald-200">{heading}</p>
          <p className="text-xs text-white/65 mt-1" dir="auto">{detail}</p>
          {offer && <div className="flex flex-wrap gap-2 mt-3">
            <button onClick={onRequestHandover} className="btn-gradient rounded-lg px-3 py-1.5 text-xs">{t('Yes, connect me')}</button>
            <button onClick={onDismiss} className="rounded-lg px-3 py-1.5 text-xs hover:bg-white/10">{t('No, continue with the assistant')}</button>
          </div>}
        </div>
        {dismiss && <button onClick={onDismiss} aria-label={t('Dismiss')} className="p-1 text-white/70">×</button>}
      </div>
    </div>
  );
}
