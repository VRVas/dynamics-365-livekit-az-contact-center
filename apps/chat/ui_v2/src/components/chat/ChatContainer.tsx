'use client';

import { useChat } from '@/hooks/useChat';
import { StarterButtons } from './StarterButtons';
import { MessageList } from './MessageList';
import { ChatInput } from './ChatInput';
import { HandoverBanner } from './HandoverBanner';
import { useLocale } from '@/i18n/LocaleProvider';
import { template } from '@/config/template';

export function ChatContainer() {
  const { t, localize } = useLocale();
  const {
    messages,
    isLoading,
    isStreaming,
    error,
    handover,
    send,
    stopStreaming,
    requestHandover,
    dismissHandoverSuggestion,
  } = useChat();

  const hasMessages = messages.length > 0;
  const showRequestHumanButton = template.features.agentConsole && hasMessages && !handover.isRequested && !handover.humanAgentConnected;

  return (
    <div className="glass rounded-2xl h-full min-h-0 flex flex-col overflow-hidden">
      {/* Scrollable conversation area */}
      <div className="flex-1 min-h-0 overflow-y-auto px-4 sm:px-6 py-6">
        {!hasMessages ? (
          <StarterButtons onSelect={send} disabled={isLoading || isStreaming} />
        ) : (
          <div className="max-w-3xl lg:max-w-5xl xl:max-w-6xl 2xl:max-w-7xl mx-auto">
            <MessageList messages={messages} onFollowUp={send} followUpDisabled={isLoading || isStreaming} />
          </div>
        )}

        {showRequestHumanButton && (
          <div className="max-w-3xl lg:max-w-5xl xl:max-w-6xl 2xl:max-w-7xl mx-auto mb-4 flex justify-center">
            <button
              onClick={() => requestHandover('Customer requested human assistance')}
              className="px-4 py-2 text-sm font-medium glass rounded-full text-white/90 hover:text-white transition-colors flex items-center gap-2"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
              </svg>
              {t('Request human agent')}
            </button>
          </div>
        )}

        <div className="max-w-3xl lg:max-w-5xl xl:max-w-6xl 2xl:max-w-7xl mx-auto">
          {template.features.agentConsole && <HandoverBanner
            handover={handover}
            onRequestHandover={requestHandover}
            onDismiss={dismissHandoverSuggestion}
          />}
          {error && (
            <div className="mb-4 p-4 glass rounded-xl text-red-300 text-sm">
              <strong>{t('Error')}:</strong> {localize(error)}
            </div>
          )}
        </div>
      </div>

      {/* Composer */}
      <div className="border-t border-white/10 p-3">
        <div className="max-w-3xl lg:max-w-5xl xl:max-w-6xl 2xl:max-w-7xl mx-auto">
          <ChatInput onSend={send} disabled={isLoading} isStreaming={isStreaming} onStop={stopStreaming} />
        </div>
      </div>
    </div>
  );
}
