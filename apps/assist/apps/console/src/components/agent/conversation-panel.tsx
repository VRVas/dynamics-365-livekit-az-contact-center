"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { cn } from "@/lib/utils";
import { 
  startConnection, 
  onNewMessage, 
  onInsightsUpdated,
  joinConversation,
  leaveConversation,
  isConnected,
  type MessageEvent,
} from "@/lib/signalr-service";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useOperatorChannel } from "@/lib/use-operator-channel";
import { template } from "@/config/template";

interface Message {
  id: string;
  conversationId: string;
  content: string;
  sender: "customer" | "ai_agent" | "operator";
  timestamp: string;
  metadata?: Record<string, unknown>;
}

interface ConversationPanelProps {
  conversationId?: string;
  onConversationChange?: (conversationId: string) => void;
  /** Callback when user clicks "Ask AI about this" on a message */
  onAskAI?: (messageContent: string) => void;
  /** Callback when insights should be refreshed */
  onInsightsRefresh?: () => void;
  /** Polling interval for messages in ms (default: 1000) */
  pollingInterval?: number;
  /** Hide the internal header (when using external panel header) */
  hideHeader?: boolean;
  /** Optional operator reply draft (e.g. seeded from "Ask AI" / suggested actions). */
  replyDraft?: string;
  /** Notified when the operator composer changes or consumes the draft. */
  onReplyDraftChange?: (value: string) => void;
}

export function ConversationPanel(props: ConversationPanelProps) {
  return template.features.agentAssist ? <EnabledConversationPanel {...props} /> : null;
}

function EnabledConversationPanel({
  conversationId, 
  onConversationChange, 
  onAskAI, 
  onInsightsRefresh,
  pollingInterval = 1000,
  hideHeader = false,
  replyDraft,
  onReplyDraftChange,
}: ConversationPanelProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signalRStatus, setSignalRStatus] = useState<'disconnected' | 'connecting' | 'connected'>('disconnected');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const prevConversationIdRef = useRef<string | undefined>(undefined);

  // Add new message handler (memoized)
  const addMessage = useCallback((message: Message) => {
    setMessages((prev) => {
      // Avoid duplicates
      if (prev.some(m => m.id === message.id)) return prev;
      return [...prev, message];
    });
    if (message.conversationId !== conversationId) {
      onConversationChange?.(message.conversationId);
    }
  }, [conversationId, onConversationChange]);

  // SignalR connection and subscription
  useEffect(() => {
    if (!conversationId) return;

    let mounted = true;
    let unsubscribeMessage: (() => void) | null = null;
    let unsubscribeInsights: (() => void) | null = null;

    const setupSignalR = async () => {
      try {
        setSignalRStatus('connecting');
        await startConnection();
        
        if (!mounted) return;
        
        // Leave previous conversation group if any
        if (prevConversationIdRef.current && prevConversationIdRef.current !== conversationId) {
          try {
            await leaveConversation(prevConversationIdRef.current);
          } catch (e) {
            console.warn('[SignalR] Failed to leave previous conversation:', e);
          }
        }
        
        // Join new conversation group
        await joinConversation(conversationId);
        prevConversationIdRef.current = conversationId;
        
        setSignalRStatus('connected');
        console.log(`[SignalR] Subscribed to conversation ${conversationId}`);
      } catch (err) {
        console.warn('[SignalR] Connection failed:', err);
        if (mounted) setSignalRStatus('disconnected');
      }
    };

    // Handle incoming messages from SignalR
    const handleNewMessage = (event: MessageEvent) => {
      if (event.conversationId === conversationId) {
        addMessage({
          id: event.message.id,
          conversationId: event.conversationId,
          content: event.message.content,
          sender: event.message.sender,
          timestamp: event.message.timestamp,
        });
        // Trigger insights refresh when new message arrives
        onInsightsRefresh?.();
      }
    };

    // Handle insights updates
    const handleInsightsUpdated = () => {
      // Trigger insights panel refresh
      onInsightsRefresh?.();
    };

    // Subscribe to events
    unsubscribeMessage = onNewMessage(handleNewMessage);
    unsubscribeInsights = onInsightsUpdated(handleInsightsUpdated);
    
    setupSignalR();

    return () => {
      mounted = false;
      unsubscribeMessage?.();
      unsubscribeInsights?.();
      if (conversationId && isConnected()) {
        leaveConversation(conversationId).catch(error => console.warn('[SignalR] Failed to leave conversation:', error));
      }
    };
  }, [conversationId, addMessage, onInsightsRefresh]);

  // Fetch messages when conversationId changes + poll every interval
  useEffect(() => {
    if (!conversationId) {
      setMessages([]);
      return;
    }

    let mounted = true;
    let isFirstLoad = true;

    const fetchMessages = async () => {
      // Only show loading spinner on first fetch
      if (isFirstLoad) {
        setLoading(true);
        setError(null);
      }
      
      try {
        const res = await fetch(`/api/conversations/${conversationId}/messages`);
        if (!res.ok) throw new Error("Failed to fetch messages");
        const data = await res.json();
        
        if (!mounted) return;
        
        // Map API response to component interface
        const mappedMessages = (data.messages || []).map((msg: { 
          messageId: string; 
          conversationId: string; 
          text: string; 
          speaker: string; 
          timestamp: string;
          channel?: string;
        }) => ({
          id: msg.messageId,
          conversationId: msg.conversationId,
          content: msg.text,
          sender: msg.speaker as "customer" | "ai_agent" | "operator",
          timestamp: msg.timestamp,
        }));
        
        setMessages(mappedMessages);
        if (isFirstLoad) setError(null);
      } catch (err) {
        if (mounted && isFirstLoad) {
          setError(err instanceof Error ? err.message : "Unknown error");
        }
      } finally {
        if (mounted) {
          setLoading(false);
          isFirstLoad = false;
        }
      }
    };

    fetchMessages();
    
    // Poll for new messages
    const pollInterval = setInterval(fetchMessages, pollingInterval);

    return () => {
      mounted = false;
      clearInterval(pollInterval);
    };
  }, [conversationId, pollingInterval]);

  // Track whether the supervisor is pinned to the bottom so the 1s poll doesn't
  // yank them away while they scroll back through earlier messages.
  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  // Auto-scroll to bottom only when already near the bottom.
  useEffect(() => {
    if (nearBottomRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages]);

  // When switching conversations, snap back to the bottom.
  useEffect(() => {
    nearBottomRef.current = true;
  }, [conversationId]);

  const formatTime = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString([], { 
      hour: "2-digit", 
      minute: "2-digit" 
    });
  };

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      {!hideHeader && (
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div>
            <h2 className="text-sm font-semibold">Conversation</h2>
            {conversationId && (
              <p className="text-xs text-muted-foreground">ID: {conversationId}</p>
            )}
          </div>
          {conversationId && (
            <span className={cn(
              "flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs",
              signalRStatus === 'connected' 
                ? "bg-green-500/10 text-green-600" 
                : signalRStatus === 'connecting'
                ? "bg-yellow-500/10 text-yellow-600"
                : "bg-muted text-muted-foreground"
            )}>
              <span className={cn(
                "h-1.5 w-1.5 rounded-full",
                signalRStatus === 'connected' 
                  ? "bg-green-500 animate-pulse" 
                  : signalRStatus === 'connecting'
                  ? "bg-yellow-500 animate-pulse"
                  : "bg-muted-foreground"
              )} />
              {signalRStatus === 'connected' ? 'Live' : signalRStatus === 'connecting' ? 'Connecting...' : 'Offline'}
            </span>
          )}
        </div>
      )}

      {/* Messages */}
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto p-4 space-y-4"
      >
        {loading && (
          <div className="flex items-center justify-center py-8">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        )}

        {error && (
          <div className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {!loading && !error && messages.length === 0 && (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <div className="mb-3 rounded-full bg-muted p-3">
              <svg className="h-6 w-6 text-muted-foreground" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
            </div>
            <p className="text-sm font-medium">No messages yet</p>
            <p className="text-xs text-muted-foreground">
              Messages will appear here in real-time
            </p>
          </div>
        )}

        {messages.map((message) => (
          <MessageBubble 
            key={message.id} 
            message={message} 
            formatTime={formatTime}
            onAskAI={onAskAI}
          />
        ))}

        <div ref={messagesEndRef} />
      </div>

      {conversationId && (
        SHOW_OPERATOR_COMPOSER ? (
          <OperatorComposer
            conversationId={conversationId}
            draft={replyDraft}
            onDraftChange={onReplyDraftChange}
          />
        ) : (
          <DraftReplyCard draft={replyDraft} onClear={() => onReplyDraftChange?.("")} />
        )
      )}
    </div>
  );
}

/* eslint-disable @typescript-eslint/no-explicit-any */
const MD_COMPONENTS = {
  p: ({ children }: any) => <p className="mb-1.5 leading-relaxed last:mb-0">{children}</p>,
  ul: ({ children }: any) => <ul className="mb-1.5 list-disc space-y-0.5 pl-4">{children}</ul>,
  ol: ({ children }: any) => <ol className="mb-1.5 list-decimal space-y-0.5 pl-4">{children}</ol>,
  li: ({ children }: any) => <li>{children}</li>,
  strong: ({ children }: any) => <strong className="font-semibold">{children}</strong>,
  em: ({ children }: any) => <em className="italic">{children}</em>,
  a: ({ href, children }: any) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2 hover:opacity-80">{children}</a>
  ),
  code: ({ children }: any) => <code className="rounded bg-black/25 px-1 py-0.5 font-mono text-[0.8em]">{children}</code>,
  pre: ({ children }: any) => <pre className="my-1.5 overflow-x-auto rounded-md bg-black/30 p-2 font-mono text-[0.8em]">{children}</pre>,
  h1: ({ children }: any) => <h3 className="mb-1 mt-1 text-sm font-semibold">{children}</h3>,
  h2: ({ children }: any) => <h3 className="mb-1 mt-1 text-sm font-semibold">{children}</h3>,
  h3: ({ children }: any) => <h4 className="mb-1 mt-1 text-[13px] font-semibold">{children}</h4>,
  blockquote: ({ children }: any) => <blockquote className="my-1.5 border-l-2 border-white/25 pl-2 italic opacity-90">{children}</blockquote>,
  hr: () => <hr className="my-2 border-white/15" />,
  table: ({ children }: any) => (
    <div className="my-1.5 overflow-x-auto"><table className="w-full border-collapse text-[12px]">{children}</table></div>
  ),
  th: ({ children }: any) => <th className="border border-white/15 bg-white/10 px-2 py-1 text-left font-semibold">{children}</th>,
  td: ({ children }: any) => <td className="border border-white/10 px-2 py-1">{children}</td>,
};

function MessageBubble({ 
  message, 
  formatTime,
  onAskAI
}: { 
  message: Message; 
  formatTime: (ts: string) => string;
  onAskAI?: (messageContent: string) => void;
}) {
  const [showActions, setShowActions] = useState(false);
  const isCustomer = message.sender === "customer";
  const isAiAgent = message.sender === "ai_agent";
  const isOperator = message.sender === "operator";
  const isAgent = isAiAgent || isOperator; // Either AI or human agent

  // Get avatar info based on sender
  const getAvatar = () => {
    if (isCustomer) return { bg: "bg-blue-500", label: "C" };
    if (isAiAgent) return { bg: "bg-violet-500", label: "AI" };
    if (isOperator) return { bg: "bg-red-500", label: "OP" };
    return { bg: "bg-muted", label: "?" };
  };

  // Get bubble styling based on sender
  const getBubbleStyle = () => {
    if (isCustomer) return "rounded-tl-sm bg-muted";
    if (isAiAgent) return "rounded-tr-sm bg-violet-600 text-white";
    if (isOperator) return "rounded-tr-sm bg-gradient-to-br from-rose-600 to-red-600 text-white";
    return "bg-muted";
  };

  const avatar = getAvatar();

  return (
    <div 
      className={cn("group flex gap-2", isCustomer ? "justify-start" : "justify-end")}
      onMouseEnter={() => setShowActions(true)}
      onMouseLeave={() => setShowActions(false)}
    >
      {isCustomer && (
        <div className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-medium text-white",
          avatar.bg
        )}>
          {avatar.label}
        </div>
      )}
      
      <div className="flex flex-col gap-1">
        <div className={cn("max-w-[75%] rounded-2xl px-4 py-2", getBubbleStyle())}>
          <div className="prose-bubble text-sm">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
              {message.content}
            </ReactMarkdown>
          </div>
          <p className={cn(
            "mt-1 text-[10px]",
            isCustomer ? "text-muted-foreground" : "text-white/70"
          )}>
            {formatTime(message.timestamp)}
          </p>
        </div>
        
        {/* Ask AI button - only for customer messages */}
        {isCustomer && onAskAI && (
          <button
            onClick={() => onAskAI(message.content)}
            className={cn(
              "inline-flex items-center gap-1.5 self-start rounded-lg px-2 py-1 text-[10px] font-medium transition-all",
              "text-primary hover:bg-primary/10",
              showActions ? "opacity-100" : "opacity-0"
            )}
          >
            <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
            </svg>
            Ask AI about this
          </button>
        )}
      </div>

      {isAgent && (
        <div className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-medium text-white",
          avatar.bg
        )}>
          {avatar.label}
        </div>
      )}
    </div>
  );
}

// Operator reply (messaging the customer) is a later feature; keep the composer code below but hide
// its UI for now and show a standalone copyable "suggested reply" card instead.
const SHOW_OPERATOR_COMPOSER: boolean = false;

function DraftReplyCard({ draft, onClear }: { draft?: string; onClear?: () => void }) {
  const [copied, setCopied] = useState(false);
  if (!draft) return null;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(draft);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable */
    }
  };
  return (
    <div className="border-t bg-muted/30 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">Suggested reply</span>
        <div className="flex items-center gap-3">
          <button
            onClick={copy}
            className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
            </svg>
            {copied ? "Copied" : "Copy"}
          </button>
          <button onClick={onClear} className="text-xs text-muted-foreground transition-colors hover:text-foreground">
            Clear
          </button>
        </div>
      </div>
      <div className="whitespace-pre-wrap rounded-lg border bg-background p-2 text-sm">{draft}</div>
    </div>
  );
}

function OperatorComposer({
  conversationId,
  draft,
  onDraftChange,
}: {
  conversationId: string;
  draft?: string;
  onDraftChange?: (value: string) => void;
}) {
  const { connected, sendToCustomer } = useOperatorChannel(conversationId);
  const [text, setText] = useState(draft ?? "");

  // Seed/replace from an external draft (e.g. an "Ask AI" suggestion or a next-best-action).
  useEffect(() => {
    if (draft !== undefined) setText(draft);
  }, [draft]);

  const send = () => {
    const body = text.trim();
    if (!body) return;
    if (sendToCustomer(body)) {
      setText("");
      onDraftChange?.("");
    }
  };

  return (
    <div className="border-t bg-background/60 p-3">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">Reply as operator</span>
        <span className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <span className={cn("h-1.5 w-1.5 rounded-full", connected ? "bg-emerald-500" : "bg-muted-foreground/50")} />
          {connected ? "Live" : "Connecting\u2026"}
        </span>
      </div>
      <div className="flex items-end gap-2">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={1}
          placeholder="Message the customer..."
          className="max-h-32 min-h-[40px] flex-1 resize-none rounded-xl border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-red-500/40"
        />
        <button
          onClick={send}
          disabled={!text.trim() || !connected}
          className="inline-flex h-10 items-center gap-1.5 rounded-xl bg-gradient-to-br from-rose-600 to-red-600 px-3.5 text-sm font-medium text-white shadow-sm transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
          </svg>
          Send
        </button>
      </div>
    </div>
  );
}
