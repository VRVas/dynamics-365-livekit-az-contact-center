'use client';

import { useCallback, useRef, useEffect, useState } from 'react';
import { useChatStore } from '@/stores/chatStore';
import { sendMessage, requestHandover as apiRequestHandover, completeHandover as apiCompleteHandover } from '@/services/api';
import { useWebSocket } from './useWebSocket';
import { useLocale } from '@/i18n/LocaleProvider';
import { template } from '@/config/template';

export function useChat() {
  const { locale } = useLocale();
  const {
    messages,
    addMessage,
    updateMessage,
    isLoading,
    setIsLoading,
    isStreaming,
    setIsStreaming,
    conversationId,
    initSession,
    error,
    setError,
    handover,
    setHandover,
    selectedProfileId,
    lockProfile,
  } = useChatStore();

  const abortControllerRef = useRef<AbortController | null>(null);
  const streamingMessageIdRef = useRef<string | null>(null);
  const accumulatedContentRef = useRef<string>('');
  
  // Track if we're in human agent mode OR waiting for agent
  const isHumanMode = template.features.agentConsole && handover.mode === 'human' && handover.status === 'active';
  const isWaitingForAgent = template.features.agentConsole && (handover.status === 'requested' || handover.isRequested === true);
  const shouldConnectWS = isHumanMode || isWaitingForAgent;
  
  // WebSocket for human agent communication
  const {
    isConnected: wsConnected,
    isConnecting: wsConnecting,
    error: wsError,
    queuePosition,
    assignedAgent,
    sendMessage: wsSendMessage,
    connect: wsConnect,
    disconnect: wsDisconnect,
  } = useWebSocket({
    sessionId: conversationId || '',
    enabled: shouldConnectWS,
    onQueuePosition: (position) => {
      setHandover({ queuePosition: position });
    },
    onAgentAssigned: (agentId) => {
      setHandover({ 
        status: 'active',
        mode: 'human',
        humanAgentId: agentId,
        humanAgentConnected: true,
        queuePosition: undefined,
      });
      // Add system message about agent connection
      addMessage({
        role: 'system',
        content: `👤 A human agent has joined the conversation.`,
      });
    },
    onHandoverComplete: () => {
      setHandover({
        status: 'completed',
        mode: 'ai',
        humanAgentConnected: false,
        humanAgentId: undefined,
      });
      addMessage({
        role: 'system',
        content: '🤖 You are now back with the AI assistant.',
      });
    },
    onError: (errorMsg) => {
      setError(`WebSocket: ${errorMsg}`);
    },
  });

  // Update handover state with WebSocket info
  useEffect(() => {
    if (queuePosition !== null) {
      setHandover({ queuePosition });
    }
  }, [queuePosition, setHandover]);

  const send = useCallback(
    async (content: string) => {
      console.log('[useChat] send called with:', content);
      console.log('[useChat] Current state:', {
        isHumanMode,
        wsConnected,
        handoverStatus: handover.status,
        handoverMode: handover.mode,
      });
      
      if (!content.trim() || isLoading || isStreaming) {
        console.log('[useChat] Blocked - empty/loading/streaming');
        return;
      }

      // Initialize session if needed
      const sessionId = initSession();
      console.log('[useChat] Session ID:', sessionId);
      
      // Lock the profile on first message
      lockProfile();
      
      // Add user message
      addMessage({ role: 'user', content });

      // If handover was completed, reset to none so we're back with AI
      // Check this FIRST before checking WebSocket to avoid routing issues
      if (handover.status === 'completed') {
        console.log('[useChat] Resetting completed handover status before AI call');
        setHandover({
          status: 'none',
          mode: 'ai',
          isRequested: false,
          humanAgentConnected: false,
        });
        // Don't return - continue to AI streaming below
      }
      // Only use WebSocket if in active human mode (not completed)
      else if (isHumanMode && wsConnected) {
        console.log('[useChat] Sending via WebSocket to human agent');
        wsSendMessage(content);
        return;
      }

      // Otherwise, use AI streaming
      // Reset state
      setError(null);
      setIsLoading(true);
      accumulatedContentRef.current = '';

      // Create placeholder for assistant message
      const assistantMessageId = addMessage({
        role: 'assistant',
        content: '',
        isStreaming: true,
      });
      streamingMessageIdRef.current = assistantMessageId;
      console.log('[useChat] Created assistant message:', assistantMessageId);

      try {
        setIsStreaming(true);
        console.log('[useChat] Calling sendMessage with profile:', selectedProfileId);
        
        await sendMessage({
          conversationId: sessionId,
          message: content,
          profileId: template.features.profiles ? selectedProfileId : undefined,
          locale,
          onToken: (token) => {
            accumulatedContentRef.current += token;
            updateMessage(
              streamingMessageIdRef.current!,
              accumulatedContentRef.current
            );
          },
          onComplete: () => {
            setIsStreaming(false);
            setIsLoading(false);
            streamingMessageIdRef.current = null;
          },
          onError: (errorMsg) => {
            setError(errorMsg);
            setIsStreaming(false);
            setIsLoading(false);
            // Update message with error
            updateMessage(
              streamingMessageIdRef.current!,
              accumulatedContentRef.current || 'Sorry, an error occurred. Please try again.'
            );
            streamingMessageIdRef.current = null;
          },
          onHandoverSuggested: (suggestion) => {
            if (!template.features.agentConsole) return;
            console.log('[useChat] Handover suggested:', suggestion);
            setHandover({
              status: 'suggested',
              suggestedReason: suggestion.reason,
              suggestedKeywords: suggestion.keywords,
            });
          },
          onHandoverRequested: () => {
            if (template.features.agentConsole) setHandover({ status: 'requested', isRequested: true, requestedAt: new Date() });
          },
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
        setIsStreaming(false);
        setIsLoading(false);
      }
    },
    [
      isLoading,
      isStreaming,
      isHumanMode,
      wsConnected,
      wsSendMessage,
      initSession,
      addMessage,
      setError,
      setIsLoading,
      setIsStreaming,
      updateMessage,
      setHandover,
      handover.status,
      handover.mode,
      selectedProfileId,
      lockProfile,
      locale,
    ]
  );

  const stopStreaming = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsStreaming(false);
    setIsLoading(false);
  }, [setIsStreaming, setIsLoading]);

  const requestHandover = useCallback(
    async (reason?: string) => {
      if (!template.features.agentConsole) return;
      const sessionId = conversationId;
      if (!sessionId) return;

      try {
        const result = await apiRequestHandover(sessionId, reason || handover.suggestedReason);
        if (result.success) {
          setHandover({
            status: 'requested',
            isRequested: true,
            requestedAt: new Date(),
          });
          // Add system message about handover
          addMessage({
            role: 'system',
            content: '🔄 Connecting you with a human agent. Please wait...',
          });
          
          // Connect WebSocket for human agent communication
          // The WebSocket will be enabled once status becomes 'active'
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to request handover');
      }
    },
    [conversationId, handover.suggestedReason, setHandover, addMessage, setError]
  );

  const dismissHandoverSuggestion = useCallback(() => {
    setHandover({
      status: 'none',
      suggestedReason: undefined,
      suggestedKeywords: undefined,
    });
  }, [setHandover]);

  return {
    messages,
    isLoading,
    isStreaming,
    error,
    conversationId,
    handover,
    // WebSocket state
    isHumanMode,
    wsConnected,
    wsConnecting,
    wsError,
    queuePosition,
    assignedAgent,
    // Actions
    send,
    stopStreaming,
    requestHandover,
    dismissHandoverSuggestion,
  };
}
