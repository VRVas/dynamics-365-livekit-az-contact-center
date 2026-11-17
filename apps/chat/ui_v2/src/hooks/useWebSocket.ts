/**
 * useWebSocket - Custom hook for WebSocket communication during human handover
 * 
 * Manages WebSocket connection to /ws/customer/{session_id} endpoint
 * for real-time chat with human agents
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Message } from '../types';
import { useChatStore } from '../stores/chatStore';
import { API_BASE_URL } from '@/constants';
import { template } from '@/config/template';

// WebSocket message types from server
interface WSIncomingMessage {
  type: 'chat_message' | 'queue_position' | 'agent_assigned' | 'handover_complete' | 'handover_completed' | 'pong' | 'error';
  content?: string;
  sender?: 'customer' | 'agent';      // Some messages use this
  sender_type?: 'customer' | 'agent' | 'system';  // Server uses this
  sender_id?: string;  // Agent ID when agent is assigned
  agent_id?: string;   // Alternative field name
  position?: number;
  timestamp?: string;
  message?: string;
}

// WebSocket message types to server
interface WSOutgoingMessage {
  type: 'chat_message' | 'ping';
  content?: string;
}

interface UseWebSocketOptions {
  sessionId: string;
  enabled: boolean;
  onMessage?: (message: Message) => void;
  onQueuePosition?: (position: number) => void;
  onAgentAssigned?: (agentId: string) => void;
  onHandoverComplete?: () => void;
  onError?: (error: string) => void;
}

interface UseWebSocketReturn {
  isConnected: boolean;
  isConnecting: boolean;
  error: string | null;
  queuePosition: number | null;
  assignedAgent: string | null;
  sendMessage: (content: string) => void;
  connect: () => void;
  disconnect: () => void;
}

// Get WebSocket URL based on environment
const getWebSocketUrl = (sessionId: string): string => {
  // Convert http(s) to ws(s)
  const wsBase = API_BASE_URL.replace(/^http/, 'ws');
  return `${wsBase}/ws/customer/${sessionId}`;
};

export function useWebSocket({
  sessionId,
  enabled,
  onMessage,
  onQueuePosition,
  onAgentAssigned,
  onHandoverComplete,
  onError,
}: UseWebSocketOptions): UseWebSocketReturn {
  enabled = enabled && template.features.chat && template.features.agentConsole && !!API_BASE_URL;
  const [isConnected, setIsConnected] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queuePosition, setQueuePosition] = useState<number | null>(null);
  const [assignedAgent, setAssignedAgent] = useState<string | null>(null);
  
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const handoverCompletedRef = useRef(false);  // Prevent reconnection after completion
  
  const { addMessage, setHandoverState } = useChatStore();

  // Cleanup function
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

  // Start heartbeat to keep connection alive
  const startHeartbeat = useCallback(() => {
    if (heartbeatIntervalRef.current) {
      clearInterval(heartbeatIntervalRef.current);
    }
    
    heartbeatIntervalRef.current = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        const pingMessage: WSOutgoingMessage = { type: 'ping' };
        wsRef.current.send(JSON.stringify(pingMessage));
      }
    }, 30000); // Ping every 30 seconds
  }, []);

  // Handle incoming WebSocket messages
  const handleMessage = useCallback((event: MessageEvent) => {
    try {
      const data: WSIncomingMessage = JSON.parse(event.data);
      
      switch (data.type) {
        case 'chat_message':
          // Check both sender and sender_type (server uses sender_type)
          const isFromAgent = data.sender === 'agent' || data.sender_type === 'agent';
          if (data.content && isFromAgent) {
            console.log('[WebSocket] Received agent message:', data.content.substring(0, 50));
            const newMessage: Message = {
              id: `ws-${Date.now()}`,
              role: 'assistant',
              content: data.content,
              timestamp: data.timestamp || new Date().toISOString(),
              isStreaming: false,
              isFromHuman: true, // Mark as human agent message
            };
            addMessage(newMessage);
            onMessage?.(newMessage);
          }
          break;
          
        case 'queue_position':
          if (data.position !== undefined) {
            setQueuePosition(data.position);
            onQueuePosition?.(data.position);
          }
          break;
          
        case 'agent_assigned':
          // agent_id might come as agent_id or sender_id from WSMessage
          const agentId = data.agent_id || data.sender_id;
          if (agentId) {
            console.log('[WebSocket] Agent assigned:', agentId);
            setAssignedAgent(agentId);
            setQueuePosition(null);
            onAgentAssigned?.(agentId);
          }
          break;
          
        case 'handover_complete':
        case 'handover_completed':
          console.log('[WebSocket] Handover completed, returning to AI mode');
          handoverCompletedRef.current = true;  // Prevent reconnection
          setHandoverState({
            status: 'completed',
            mode: 'ai',
            humanAgentConnected: false,
            isRequested: false,
          });
          setAssignedAgent(null);
          onHandoverComplete?.();
          break;
          
        case 'pong':
          // Heartbeat response, connection is alive
          break;
          
        case 'error':
          const errorMsg = data.message || 'Unknown WebSocket error';
          setError(errorMsg);
          onError?.(errorMsg);
          break;
          
        default:
          console.warn('Unknown WebSocket message type:', data.type);
      }
    } catch (e) {
      console.error('Error parsing WebSocket message:', e);
    }
  }, [addMessage, setHandoverState, onMessage, onQueuePosition, onAgentAssigned, onHandoverComplete, onError]);

  // Connect to WebSocket
  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      return; // Already connected
    }
    
    if (isConnecting) {
      return; // Already connecting
    }
    
    // Reset the completed flag when starting a new connection
    handoverCompletedRef.current = false;
    
    cleanup();
    setIsConnecting(true);
    setError(null);
    
    const wsUrl = getWebSocketUrl(sessionId);
    console.log('Connecting to WebSocket:', wsUrl);
    
    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      
      ws.onopen = () => {
        console.log('WebSocket connected');
        setIsConnected(true);
        setIsConnecting(false);
        setError(null);
        reconnectAttemptsRef.current = 0;
        startHeartbeat();
      };
      
      ws.onmessage = handleMessage;
      
      ws.onerror = (event) => {
        console.error('WebSocket error:', event);
        setError('Connection error');
        setIsConnecting(false);
      };
      
      ws.onclose = (event) => {
        console.log('WebSocket closed:', event.code, event.reason);
        setIsConnected(false);
        setIsConnecting(false);
        
        if (heartbeatIntervalRef.current) {
          clearInterval(heartbeatIntervalRef.current);
          heartbeatIntervalRef.current = null;
        }
        
        // Attempt to reconnect if enabled, not a clean close, and handover wasn't completed
        if (enabled && event.code !== 1000 && reconnectAttemptsRef.current < 5 && !handoverCompletedRef.current) {
          const delay = Math.min(1000 * Math.pow(2, reconnectAttemptsRef.current), 30000);
          console.log(`Reconnecting in ${delay}ms (attempt ${reconnectAttemptsRef.current + 1})`);
          
          reconnectTimeoutRef.current = setTimeout(() => {
            reconnectAttemptsRef.current++;
            connect();
          }, delay);
        }
      };
    } catch (e) {
      console.error('Error creating WebSocket:', e);
      setError('Failed to create connection');
      setIsConnecting(false);
    }
  }, [sessionId, enabled, isConnecting, cleanup, startHeartbeat, handleMessage]);

  // Disconnect from WebSocket
  const disconnect = useCallback(() => {
    cleanup();
    setIsConnected(false);
    setIsConnecting(false);
    setQueuePosition(null);
    setAssignedAgent(null);
    reconnectAttemptsRef.current = 0;
  }, [cleanup]);

  // Send message through WebSocket
  const sendMessage = useCallback((content: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.error('WebSocket not connected');
      setError('Not connected');
      return;
    }
    
    const message: WSOutgoingMessage = {
      type: 'chat_message',
      content,
    };
    
    wsRef.current.send(JSON.stringify(message));
  }, []);

  // Auto-connect when enabled
  useEffect(() => {
    if (enabled && sessionId) {
      connect();
    } else {
      disconnect();
    }
    
    return () => {
      cleanup();
    };
  }, [enabled, sessionId]); // Don't include connect/disconnect to avoid loops

  return {
    isConnected,
    isConnecting,
    error,
    queuePosition,
    assignedAgent,
    sendMessage,
    connect,
    disconnect,
  };
}

export default useWebSocket;
