"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { cn } from "@/lib/utils";
import { template, text } from "@/config/template";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  citations?: Citation[];
  suggestedResponses?: string[];
}

interface Citation {
  id: string;
  title: string;
  category?: string;
  snippet?: string;
  url?: string;
}

interface KnowledgeChatPanelProps {
  conversationId?: string;
  /** Callback when user wants to copy text to their reply */
  onCopyToReply?: (text: string) => void;
  /** Pre-fill query (e.g., from "Ask AI about this") */
  initialQuery?: string;
  /** Hide the internal header (when using external panel header) */
  hideHeader?: boolean;
}

/**
 * AI Knowledge Chat Panel
 * RAG UPGRADE NEEDED HERE: Currently uses kb.md injection, upgrade to Azure AI Search
 */
export function KnowledgeChatPanel({ 
  conversationId, 
  onCopyToReply,
  initialQuery,
  hideHeader = false,
}: KnowledgeChatPanelProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Handle initial query from "Ask AI about this"
  useEffect(() => {
    if (initialQuery && initialQuery.trim()) {
      setInput(initialQuery);
      // Auto-submit after a brief delay
      const timer = setTimeout(() => {
        handleSubmit(new Event('submit') as unknown as React.FormEvent);
      }, 100);
      return () => clearTimeout(timer);
    }
  }, [initialQuery]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userMessage: Message = {
      id: `user-${Date.now()}`,
      role: "user",
      content: input.trim(),
      timestamp: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setLoading(true);

    try {
      const res = await fetch("/api/agent-assist/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: userMessage.content,
          conversationId,
        }),
      });

      if (!res.ok) throw new Error("Failed to get response");

      const data = await res.json();

      const assistantMessage: Message = {
        id: `assistant-${Date.now()}`,
        role: "assistant",
        content: data.answer,
        timestamp: new Date().toISOString(),
        suggestedResponses: data.suggestedResponses || [],
        citations: data.citations?.map((c: { id?: string; title: string; category?: string; content?: string; snippet?: string; url?: string }, i: number) => ({
          id: c.id || `cite-${i}`,
          title: c.title,
          category: c.category || 'General',
          snippet: c.content || c.snippet,
          url: c.url,
        })),
      };

      setMessages((prev) => [...prev, assistantMessage]);
    } catch (err) {
      const errorMessage: Message = {
        id: `error-${Date.now()}`,
        role: "assistant",
        content: "Sorry, I couldn't process your request. Please try again.",
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, errorMessage]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  };

  const handleCopyToClipboard = useCallback(async (text: string, id: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 2000);
    } catch {
      console.error('Failed to copy to clipboard');
    }
  }, []);

  const handleCopyToReply = useCallback((text: string) => {
    if (onCopyToReply) {
      onCopyToReply(text);
    } else {
      // Fallback to clipboard
      handleCopyToClipboard(text, 'reply');
    }
  }, [onCopyToReply, handleCopyToClipboard]);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      {!hideHeader && (
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold">{text(template.brand.assistantName)}</h2>
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <div className="mb-4 rounded-full bg-primary/10 p-4">
              <svg className="h-8 w-8 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
              </svg>
            </div>
            <p className="text-sm font-medium mb-1">{text(template.welcome.title)}</p>
            {text(template.welcome.description) && <p className="text-xs text-muted-foreground mb-4">{text(template.welcome.description)}</p>}
            
            {/* Suggested questions */}
            {template.quickActions.length > 0 && <div className="w-full space-y-2">
              {template.quickActions.map((action) => (
                <button
                  key={action.id}
                  onClick={() => setInput(text(action.prompt))}
                  className="w-full rounded-lg border p-2 text-start text-xs transition-colors hover:bg-muted/50"
                >
                  {text(action.label)}
                </button>
              ))}
            </div>}
          </div>
        )}

        {messages.map((message) => (
          <ChatMessage 
            key={message.id} 
            message={message} 
            onCopyToReply={handleCopyToReply}
            onCopyToClipboard={handleCopyToClipboard}
            copiedId={copiedId}
          />
        ))}

        {loading && (
          <div className="flex gap-2">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary">
              <svg className="h-4 w-4 text-primary-foreground" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
              </svg>
            </div>
            <div className="rounded-2xl rounded-tl-sm bg-muted px-4 py-3">
              <div className="flex gap-1">
                <span className="h-2 w-2 animate-bounce rounded-full bg-muted-foreground/50 [animation-delay:-0.3s]" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-muted-foreground/50 [animation-delay:-0.15s]" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-muted-foreground/50" />
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <form onSubmit={handleSubmit} className="border-t p-4">
        <div className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask a question..."
            dir="auto"
            disabled={loading}
            className="flex-1 rounded-lg border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={!input.trim() || loading}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
            </svg>
          </button>
        </div>
      </form>
    </div>
  );
}

function ChatMessage({ 
  message, 
  onCopyToReply,
  onCopyToClipboard,
  copiedId
}: { 
  message: Message; 
  onCopyToReply: (text: string) => void;
  onCopyToClipboard: (text: string, id: string) => void;
  copiedId: string | null;
}) {
  const isUser = message.role === "user";
  const [expandedCitation, setExpandedCitation] = useState<string | null>(null);

  return (
    <div className={cn("flex gap-2", isUser ? "justify-end" : "justify-start")}>
      {!isUser && (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary">
          <svg className="h-4 w-4 text-primary-foreground" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
        </div>
      )}

      <div className={cn(
        "max-w-[85%] space-y-2",
        isUser ? "items-end" : "items-start"
      )}>
        <div className={cn(
          "rounded-2xl px-4 py-2",
          isUser 
            ? "rounded-tr-sm bg-primary text-primary-foreground" 
            : "rounded-tl-sm bg-muted"
        )}>
          <p className="text-sm whitespace-pre-wrap">{message.content}</p>
        </div>

        {/* Action buttons for assistant messages */}
        {!isUser && (
          <div className="flex items-center gap-1">
            <button
              onClick={() => onCopyToClipboard(message.content, message.id)}
              className="inline-flex items-center gap-1 rounded px-2 py-1 text-[10px] text-muted-foreground hover:bg-muted/50 transition-colors"
              title="Copy to clipboard"
            >
              {copiedId === message.id ? (
                <>
                  <svg className="h-3 w-3 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                  Copied!
                </>
              ) : (
                <>
                  <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                  </svg>
                  Copy
                </>
              )}
            </button>
            <button
              onClick={() => onCopyToClipboard(message.content, `reply-${message.id}`)}
              className="inline-flex items-center gap-1 rounded px-2 py-1 text-[10px] text-primary hover:bg-primary/10 transition-colors font-medium"
              title="Copy response to clipboard"
            >
              {copiedId === `reply-${message.id}` ? (
                <>
                  <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                  Copied!
                </>
              ) : (
                <>
                  <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                  </svg>
                  Copy
                </>
              )}
            </button>
          </div>
        )}

        {/* Suggested responses */}
        {!isUser && message.suggestedResponses && message.suggestedResponses.length > 0 && (
          <div className="space-y-1">
            <p className="text-[10px] text-muted-foreground font-medium">Suggested customer responses:</p>
            {message.suggestedResponses.map((response, i) => (
              <div 
                key={i}
                className="group relative rounded-lg border border-dashed border-muted-foreground/30 p-2 text-xs hover:border-primary/50 hover:bg-muted/30 transition-colors"
              >
                <p className="pr-16">{response}</p>
                <button
                  onClick={() => onCopyToClipboard(response, `suggestion-${message.id}-${i}`)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 inline-flex items-center gap-1 rounded px-2 py-1 text-[10px] bg-primary/10 text-primary hover:bg-primary/20 transition-colors opacity-0 group-hover:opacity-100"
                >
                  {copiedId === `suggestion-${message.id}-${i}` ? (
                    <>
                      <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                      </svg>
                      Copied!
                    </>
                  ) : (
                    <>
                      <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                      </svg>
                      Copy
                    </>
                  )}
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Citations with expandable snippets */}
        {message.citations && message.citations.length > 0 && (
          <div className="space-y-1">
            <p className="text-[10px] text-muted-foreground font-medium">Sources:</p>
            <div className="space-y-1">
              {message.citations.map((citation) => (
                <div key={citation.id} className="rounded-lg border bg-card/50 overflow-hidden">
                  <button
                    onClick={() => setExpandedCitation(expandedCitation === citation.id ? null : citation.id)}
                    className="w-full flex items-center justify-between gap-2 px-2 py-1.5 text-left hover:bg-muted/50 transition-colors"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <svg className="h-3 w-3 shrink-0 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                      </svg>
                      <span className="text-[11px] font-medium truncate">{citation.title}</span>
                      {citation.category && (
                        <span className="text-[9px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground shrink-0">
                          {citation.category}
                        </span>
                      )}
                    </div>
                    <svg 
                      className={cn(
                        "h-3 w-3 shrink-0 text-muted-foreground transition-transform",
                        expandedCitation === citation.id && "rotate-180"
                      )} 
                      fill="none" 
                      viewBox="0 0 24 24" 
                      stroke="currentColor"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </button>
                  {expandedCitation === citation.id && citation.snippet && (
                    <div className="px-2 pb-2 border-t">
                      <p className="text-[10px] text-muted-foreground mt-1.5 line-clamp-4">
                        {citation.snippet}
                      </p>
                      {citation.url && (
                        <a
                          href={citation.url}
                          className="inline-flex items-center gap-1 mt-1.5 text-[10px] text-primary hover:underline"
                        >
                          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                          </svg>
                          Open full article
                        </a>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {isUser && (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted">
          <svg className="h-4 w-4 text-muted-foreground" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
          </svg>
        </div>
      )}
    </div>
  );
}
