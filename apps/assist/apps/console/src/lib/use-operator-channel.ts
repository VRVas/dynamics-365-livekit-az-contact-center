'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { getOrchestratorAgentWsUrl } from './orchestrator-service';

function stableOperatorId(): string {
  if (typeof window === 'undefined') return 'supervisor';
  let id = sessionStorage.getItem('moacc-operator-id');
  if (!id) {
    id = 'supervisor-' + Math.random().toString(36).slice(2, 8);
    sessionStorage.setItem('moacc-operator-id', id);
  }
  return id;
}

/**
 * Keeps a persistent operator websocket to the orchestrator open and exposes a sender that
 * delivers an operator message to the customer on the given chat conversation. The first send
 * for a conversation also claims the session (accept_session) so the customer is told a human
 * joined; subsequent sends just relay chat_message.
 */
export function useOperatorChannel(conversationId?: string) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const claimedRef = useRef<Set<string>>(new Set());
  const idRef = useRef<string>('');

  useEffect(() => {
    idRef.current = stableOperatorId();
    let stopped = false;
    let heartbeat: ReturnType<typeof setInterval> | undefined;

    const connect = () => {
      if (stopped) return;
      try {
        const ws = new WebSocket(getOrchestratorAgentWsUrl(idRef.current));
        wsRef.current = ws;
        ws.onopen = () => {
          setConnected(true);
          heartbeat = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
          }, 30000);
        };
        ws.onclose = () => {
          setConnected(false);
          if (heartbeat) clearInterval(heartbeat);
          if (!stopped) setTimeout(connect, 2500);
        };
        ws.onerror = () => {};
        // The supervisor console doesn't consume the operator queue here; ignore inbound frames.
        ws.onmessage = () => {};
      } catch {
        if (!stopped) setTimeout(connect, 2500);
      }
    };
    connect();

    return () => {
      stopped = true;
      if (heartbeat) clearInterval(heartbeat);
      wsRef.current?.close();
    };
  }, []);

  const sendToCustomer = useCallback(
    (text: string): boolean => {
      const ws = wsRef.current;
      const cid = conversationId;
      const body = text.trim();
      if (!ws || ws.readyState !== WebSocket.OPEN || !cid || !body) return false;
      if (!claimedRef.current.has(cid)) {
        ws.send(JSON.stringify({ type: 'accept_session', session_id: cid }));
        claimedRef.current.add(cid);
      }
      ws.send(JSON.stringify({ type: 'chat_message', session_id: cid, content: body }));
      return true;
    },
    [conversationId]
  );

  return { connected, sendToCustomer };
}
