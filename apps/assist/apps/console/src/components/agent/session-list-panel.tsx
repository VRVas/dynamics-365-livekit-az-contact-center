"use client";

import { useState, useEffect, useCallback } from "react";
import { cn } from "@/lib/utils";
import { template } from "@/config/template";

interface Session {
  id: string;
  sessionId: string;
  externalSessionId?: string;
  status: "active" | "completed" | "abandoned";
  humanAttentionNeeded: boolean;
  humanAttentionRequestedAt?: string;
  escalationReason?: string;
  channel: "chat" | "voice" | "email" | "other";
  customerMessageCount: number;
  aiAgentMessageCount: number;
  operatorMessageCount: number;
  createdAt: string;
  updatedAt: string;
  completedAt?: string;
}

interface SessionListPanelProps {
  selectedSessionId?: string;
  onSessionSelect: (sessionId: string) => void;
  pollingInterval?: number; // in ms, default 1000
  hideHeader?: boolean;
  onAttentionCountChange?: (count: number) => void;
}

export function SessionListPanel(props: SessionListPanelProps) {
  return template.features.agentAssist ? <EnabledSessionListPanel {...props} /> : null;
}

function EnabledSessionListPanel({
  selectedSessionId,
  onSessionSelect,
  pollingInterval = 1000,
  hideHeader = false,
  onAttentionCountChange,
}: SessionListPanelProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "active" | "attention">("all");
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);

  // Report attention count to parent
  useEffect(() => {
    const count = sessions.filter(s => s.humanAttentionNeeded && s.status === "active").length;
    onAttentionCountChange?.(count);
  }, [sessions, onAttentionCountChange]);

  const fetchSessions = useCallback(async () => {
    try {
      const response = await fetch("/api/sessions");
      if (!response.ok) {
        throw new Error("Failed to fetch sessions");
      }
      const data = await response.json();
      setSessions(data.sessions || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial fetch
  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  // Polling for real-time updates
  useEffect(() => {
    const interval = setInterval(fetchSessions, pollingInterval);
    return () => clearInterval(interval);
  }, [fetchSessions, pollingInterval]);

  // Filter sessions
  const filteredSessions = sessions.filter((session) => {
    if (filter === "active") return session.status === "active";
    if (filter === "attention") return session.humanAttentionNeeded && session.status === "active";
    return true;
  });

  // ---- multi-select + delete ----
  const sidOf = (s: Session) => s.sessionId || s.id;

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allVisibleSelected =
    filteredSessions.length > 0 && filteredSessions.every((s) => selectedIds.has(sidOf(s)));

  const toggleSelectAll = () => {
    setSelectedIds((prev) => {
      if (filteredSessions.length > 0 && filteredSessions.every((s) => prev.has(sidOf(s)))) {
        return new Set();
      }
      return new Set(filteredSessions.map(sidOf));
    });
  };

  const exitSelectMode = () => {
    setSelectMode(false);
    setSelectedIds(new Set());
  };

  const handleDeleteSelected = async () => {
    if (selectedIds.size === 0) return;
    const ids = [...selectedIds];
    const ok = window.confirm(
      `Delete ${ids.length} session${ids.length > 1 ? "s" : ""}? This permanently removes the conversation, its messages, and insights.`
    );
    if (!ok) return;
    setDeleting(true);
    try {
      await Promise.all(
        ids.map((id) =>
          fetch(`/api/sessions/${id}`, { method: "DELETE" }).catch((e) =>
            console.error("Failed to delete session", id, e)
          )
        )
      );
      // Optimistically drop them, then refresh from the server.
      setSessions((prev) => prev.filter((s) => !selectedIds.has(sidOf(s))));
      exitSelectMode();
      await fetchSessions();
    } finally {
      setDeleting(false);
    }
  };

  // Get channel icon
  const getChannelIcon = (channel: Session["channel"]) => {
    switch (channel) {
      case "chat":
        return "💬";
      case "voice":
        return "📞";
      case "email":
        return "📧";
      default:
        return "📋";
    }
  };

  // Get status badge
  const getStatusBadge = (session: Session) => {
    if (session.humanAttentionNeeded && session.status === "active") {
      return (
        <span className="flex items-center gap-1 rounded-full bg-red-500/10 px-2 py-0.5 text-xs font-medium text-red-600">
          <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
          Needs Attention
        </span>
      );
    }
    if (session.status === "active") {
      return (
        <span className="flex items-center gap-1 rounded-full bg-green-500/10 px-2 py-0.5 text-xs font-medium text-green-600">
          <span className="h-1.5 w-1.5 rounded-full bg-green-500" />
          Active
        </span>
      );
    }
    if (session.status === "completed") {
      return (
        <span className="rounded-full bg-gray-500/10 px-2 py-0.5 text-xs font-medium text-gray-500">
          Completed
        </span>
      );
    }
    return (
      <span className="rounded-full bg-yellow-500/10 px-2 py-0.5 text-xs font-medium text-yellow-600">
        Abandoned
      </span>
    );
  };

  // Format time ago
  const formatTimeAgo = (dateString: string) => {
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    
    if (diffMins < 1) return "Just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d ago`;
  };

  return (
    <div className="flex h-full flex-col">
      {/* Filter Tabs */}
      <div className="flex border-b px-2 py-2">
        <button
          onClick={() => setFilter("all")}
          className={cn(
            "flex-1 rounded-md px-2 py-1 text-xs font-medium transition-colors",
            filter === "all"
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-muted"
          )}
        >
          All ({sessions.length})
        </button>
        <button
          onClick={() => setFilter("active")}
          className={cn(
            "flex-1 rounded-md px-2 py-1 text-xs font-medium transition-colors",
            filter === "active"
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-muted"
          )}
        >
          Active ({sessions.filter(s => s.status === "active").length})
        </button>
        <button
          onClick={() => setFilter("attention")}
          className={cn(
            "flex-1 rounded-md px-2 py-1 text-xs font-medium transition-colors",
            filter === "attention"
              ? "bg-red-500 text-white"
              : "text-muted-foreground hover:bg-muted"
          )}
        >
          🚨 ({sessions.filter(s => s.humanAttentionNeeded && s.status === "active").length})
        </button>
      </div>

      {/* Select / delete toolbar */}
      <div className="flex items-center justify-between gap-2 border-b px-3 py-1.5">
        {selectMode ? (
          <>
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={allVisibleSelected}
                onChange={toggleSelectAll}
                className="h-3.5 w-3.5 accent-red-500"
              />
              {selectedIds.size} selected
            </label>
            <div className="flex items-center gap-1.5">
              <button
                onClick={handleDeleteSelected}
                disabled={selectedIds.size === 0 || deleting}
                className="rounded-md bg-red-500 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {deleting ? "Deleting…" : `Delete (${selectedIds.size})`}
              </button>
              <button
                onClick={exitSelectMode}
                disabled={deleting}
                className="rounded-md px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
              >
                Cancel
              </button>
            </div>
          </>
        ) : (
          <button
            onClick={() => setSelectMode(true)}
            className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
            </svg>
            Select
          </button>
        )}
      </div>

      {/* Session List */}
      <div className="flex-1 overflow-y-auto">
        {loading && sessions.length === 0 ? (
          <div className="flex items-center justify-center p-8 text-sm text-muted-foreground">
            Loading sessions...
          </div>
        ) : error ? (
          <div className="flex items-center justify-center p-8 text-sm text-red-500">
            {error}
          </div>
        ) : filteredSessions.length === 0 ? (
          <div className="flex items-center justify-center p-8 text-sm text-muted-foreground">
            No sessions found
          </div>
        ) : (
          <div className="divide-y">
            {filteredSessions.map((session) => {
              const id = session.sessionId || session.id;
              return (
              <div
                key={session.id || session.sessionId}
                onClick={() => (selectMode ? toggleSelect(id) : onSessionSelect(id))}
                className={cn(
                  "flex w-full cursor-pointer gap-2.5 px-4 py-3 text-left transition-colors hover:bg-muted/50",
                  selectedSessionId === id && !selectMode && "bg-muted",
                  selectMode && selectedIds.has(id) && "bg-primary/5",
                  session.humanAttentionNeeded && session.status === "active" && "border-l-2 border-l-red-500"
                )}
              >
                {selectMode && (
                  <input
                    type="checkbox"
                    checked={selectedIds.has(id)}
                    onChange={() => toggleSelect(id)}
                    onClick={(e) => e.stopPropagation()}
                    className="mt-1 h-4 w-4 shrink-0 cursor-pointer accent-red-500"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span className="text-lg">{getChannelIcon(session.channel)}</span>
                      <div>
                        <div className="text-xs font-medium truncate max-w-[120px]">
                          {session.externalSessionId || (session.sessionId || session.id || 'Unknown').slice(0, 12)}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {formatTimeAgo(session.updatedAt)}
                        </div>
                      </div>
                    </div>
                    {getStatusBadge(session)}
                  </div>

                  {/* Message counts */}
                  <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
                    <span title="Customer messages">👤 {session.customerMessageCount}</span>
                    <span title="AI Agent messages">🤖 {session.aiAgentMessageCount}</span>
                    <span title="Operator messages">👨‍💼 {session.operatorMessageCount}</span>
                  </div>

                  {/* Escalation reason preview */}
                  {session.escalationReason && (
                    <div className="mt-1 text-xs text-red-500 truncate">
                      ⚠️ {session.escalationReason}
                    </div>
                  )}
                </div>
              </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
