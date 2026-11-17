'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useAgentStore } from '@/stores/agentStore';
import { template } from '@/config/template';
import { API_BASE_URL } from '@/config/endpoints';

interface QueueEntry {
  sessionId: string;
  reason?: string;
  waitingTime: number;
  queuedAt: Date;
}

interface WSIncomingMessage {
  type: 'chat_message' | 'queue_update' | 'session_assigned' | 'session_ended' | 'session_completed' | 'pong' | 'error';
  content?: string;
  sender?: 'customer' | 'agent';      // Some messages use this
  sender_type?: 'customer' | 'agent' | 'system';  // Server uses this
  session_id?: string;
  queue?: Array<{
    session_id: string;
    reason?: string;
    waiting_seconds: number;
    queued_at: string;
  }>;
  message?: string;
  timestamp?: string;
}

interface WSOutgoingMessage {
  type: 'chat_message' | 'accept_session' | 'complete_session' | 'ping';
  session_id?: string;
  content?: string;
}

interface UseAgentWebSocketOptions {
  agentId: string;
  enabled: boolean;
}

interface UseAgentWebSocketReturn {
  isConnected: boolean;
  queue: QueueEntry[];
  acceptSession: (sessionId: string) => void;
  sendMessage: (content: string) => void;
  completeSession: () => void;
}

const getWebSocketUrl = (agentId: string): string => {
  const wsBase = API_BASE_URL.replace(/^http/, 'ws');
  return `${wsBase}/ws/agent/${agentId}`;
};

// Fetch conversation history from API
async function fetchConversationHistory(sessionId: string): Promise<Array<{id: string; content: string; sender: 'customer' | 'agent'; timestamp: Date}>> {
  if (!template.features.chat || !template.features.agentConsole || !API_BASE_URL) return [];
  try {
    const response = await fetch(`${API_BASE_URL}/conversation/${sessionId}`);
    if (!response.ok) {
      console.warn('Failed to fetch conversation history:', response.status);
      return [];
    }
    
    const messages = await response.json();
    
    // Convert API messages to chat format
    return messages.map((msg: any, index: number) => ({
      id: `history-${index}`,
      content: msg.content || '',
      // Map roles: user = customer, assistant = agent (AI acted as agent before handover)
      sender: msg.role === 'user' ? 'customer' : 'agent' as 'customer' | 'agent',
      timestamp: new Date(),
      isHistory: true, // Mark as historical message
    })).filter((msg: any) => msg.content && msg.content.trim());
  } catch (error) {
    console.error('Error fetching conversation history:', error);
    return [];
  }
}

export function useAgentWebSocket({
  agentId,
  enabled,
}: UseAgentWebSocketOptions): UseAgentWebSocketReturn {
  enabled = enabled && template.features.chat && template.features.agentConsole && !!API_BASE_URL;
  const [isConnected, setIsConnected] = useState(false);
  const [queue, setQueue] = useState<QueueEntry[]>([]);
  
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const reconnectAttemptsRef = useRef(0);
  
  const { setConnected, setActiveSession, addMessage, activeSession } = useAgentStore();

  const cleanup = useCallback(() => {
    if (heartbeatIntervalRef.current) {
      clearInterval(heartbeatIntervalRef.current);
      heartbeatIntervalRef.current = null;
    }
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.onopen = null;
      wsRef.current.onmessage = null;
      wsRef.current.onerror = null;
      wsRef.current.onclose = null;
      wsRef.current.close(1000);
      wsRef.current = null;
    }
  }, []);

  const startHeartbeat = useCallback(() => {
    if (heartbeatIntervalRef.current) {
      clearInterval(heartbeatIntervalRef.current);
    }
    heartbeatIntervalRef.current = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        const ping: WSOutgoingMessage = { type: 'ping' };
        wsRef.current.send(JSON.stringify(ping));
      }
    }, 30000);
  }, []);

  const handleMessage = useCallback((event: MessageEvent) => {
    try {
      const data: WSIncomingMessage = JSON.parse(event.data);
      
      switch (data.type) {
        case 'chat_message':
          // Check both sender and sender_type (server uses sender_type)
          const isFromCustomer = data.sender === 'customer' || data.sender_type === 'customer';
          if (data.content && isFromCustomer) {
            console.log('[AgentWS] Received customer message:', data.content.substring(0, 50));
            addMessage({
              id: `msg-${Date.now()}`,
              content: data.content,
              sender: 'customer',
              timestamp: new Date(data.timestamp || Date.now()),
            });
          }
          break;
          
        case 'queue_update':
          // Content might be JSON string with queue data
          let queueData = data.queue;
          if (!queueData && data.content) {
            try {
              const parsed = JSON.parse(data.content);
              queueData = parsed.queue || parsed;
            } catch (e) {
              console.warn('Failed to parse queue content:', e);
            }
          }
          if (Array.isArray(queueData)) {
            console.log('Queue update received:', queueData.length, 'items');
            setQueue(queueData.map((q: any) => ({
              sessionId: q.session_id,
              reason: q.reason,
              waitingTime: q.waiting_seconds || 0,
              queuedAt: new Date(q.queued_at || Date.now()),
            })));
          }
          break;
          
        case 'session_assigned':
          // Content is JSON: {"session_id": "xxx"}
          let sessionId = data.session_id;
          if (!sessionId && data.content) {
            try {
              const parsed = JSON.parse(data.content);
              sessionId = parsed.session_id;
            } catch (e) {
              sessionId = data.content;
            }
          }
          if (sessionId) {
            console.log('Session assigned:', sessionId);
            
            // Fetch conversation history
            fetchConversationHistory(sessionId).then((history) => {
              console.log('Loaded conversation history:', history.length, 'messages');
              setActiveSession({
                sessionId: sessionId,
                startedAt: new Date(),
                messages: history,
              });
            }).catch((err) => {
              console.error('Failed to load history:', err);
              setActiveSession({
                sessionId: sessionId,
                startedAt: new Date(),
                messages: [],
              });
            });
          }
          break;
          
        case 'session_ended':
        case 'session_completed':
          console.log('Session ended/completed');
          setActiveSession(null);
          break;
          
        case 'pong':
          // Heartbeat response
          break;
          
        case 'error':
          console.error('WebSocket error:', data.message);
          break;
      }
    } catch (e) {
      console.error('Error parsing WebSocket message:', e);
    }
  }, [addMessage, setActiveSession]);

  const connect = useCallback(() => {
    if (!enabled || !agentId) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    
    cleanup();
    
    const wsUrl = getWebSocketUrl(agentId);
    console.log('Agent connecting to WebSocket:', wsUrl);
    
    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      
      ws.onopen = () => {
        console.log('Agent WebSocket connected');
        setIsConnected(true);
        setConnected(true);
        reconnectAttemptsRef.current = 0;
        startHeartbeat();
      };
      
      ws.onmessage = handleMessage;
      
      ws.onerror = (event) => {
        console.error('Agent WebSocket error:', event);
      };
      
      ws.onclose = (event) => {
        console.log('Agent WebSocket closed:', event.code);
        setIsConnected(false);
        setConnected(false);
        
        if (heartbeatIntervalRef.current) {
          clearInterval(heartbeatIntervalRef.current);
        }
        
        // Reconnect if enabled
        if (enabled && event.code !== 1000 && reconnectAttemptsRef.current < 5) {
          const delay = Math.min(1000 * Math.pow(2, reconnectAttemptsRef.current), 30000);
          reconnectTimeoutRef.current = setTimeout(() => {
            reconnectAttemptsRef.current++;
            connect();
          }, delay);
        }
      };
    } catch (e) {
      console.error('Error creating WebSocket:', e);
    }
  }, [agentId, enabled, cleanup, startHeartbeat, handleMessage, setConnected]);

  // Accept a session from the queue
  const acceptSession = useCallback((sessionId: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.error('WebSocket not connected');
      return;
    }
    
    const message: WSOutgoingMessage = {
      type: 'accept_session',
      session_id: sessionId,
    };
    wsRef.current.send(JSON.stringify(message));
  }, []);

  // Send a message to the customer
  const sendMessage = useCallback((content: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.error('WebSocket not connected');
      return;
    }
    
    if (!activeSession) {
      console.error('No active session');
      return;
    }
    
    const message: WSOutgoingMessage = {
      type: 'chat_message',
      session_id: activeSession.sessionId,
      content,
    };
    wsRef.current.send(JSON.stringify(message));
    
    // Add to local messages immediately
    addMessage({
      id: `msg-${Date.now()}`,
      content,
      sender: 'agent',
      timestamp: new Date(),
    });
  }, [activeSession, addMessage]);

  // Complete the handover session
  const completeSession = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.error('WebSocket not connected');
      return;
    }
    
    if (!activeSession) {
      console.error('No active session');
      return;
    }
    
    const message: WSOutgoingMessage = {
      type: 'complete_session',
      session_id: activeSession.sessionId,
    };
    wsRef.current.send(JSON.stringify(message));
    
    // Clear active session locally
    setActiveSession(null);
  }, [activeSession, setActiveSession]);

  // Connect on mount, cleanup on unmount
  useEffect(() => {
    if (enabled && agentId) {
      connect();
    }
    
    return () => {
      cleanup();
    };
  }, [enabled, agentId]); // Don't include connect/cleanup to avoid loops

  return {
    isConnected,
    queue,
    acceptSession,
    sendMessage,
    completeSession,
  };
}
