"use client";

import { useState, useEffect, useCallback, useImperativeHandle, forwardRef } from "react";
import { cn } from "@/lib/utils";
import { template } from "@/config/template";

interface Sentiment {
  current: {
    label: string;
    score: number;
    confidence: number;
  };
  frustrationScore: number;
}

interface KPIs {
  customerKPIs: {
    issueComplexity: string;
    escalationRisk: string;
    churnRisk: string;
  };
  conversationKPIs: {
    durationSeconds: number;
    customerMessageCount: number;
    agentMessageCount: number;
    topicIntent?: string;
  };
}

interface NextBestAction {
  id: string;
  type: string;
  label: string;
  description: string;
  priority: number;
}

interface Insights {
  conversationId: string;
  sentiment: Sentiment;
  customerKPIs: KPIs["customerKPIs"];
  conversationKPIs: KPIs["conversationKPIs"];
  nextBestActions: NextBestAction[];
  summary: string;
}

interface InsightsPanelProps {
  conversationId?: string;
  onActionClick?: (action: NextBestAction) => void;
  /** Trigger value - increment to force refresh */
  refreshTrigger?: number;
  /** Polling interval for insights in ms (default: 3000) */
  pollingInterval?: number;
  /** Hide the internal header (when using external panel header) */
  hideHeader?: boolean;
}

export interface InsightsPanelRef {
  refresh: () => void;
}

export const InsightsPanel = forwardRef<InsightsPanelRef, InsightsPanelProps>(
  function GatedInsightsPanel(props, ref) {
    return template.features.agentAssist ? <EnabledInsightsPanel {...props} ref={ref} /> : null;
  }
);

const EnabledInsightsPanel = forwardRef<InsightsPanelRef, InsightsPanelProps>(
  function InsightsPanel({ conversationId, onActionClick, refreshTrigger, pollingInterval = 3000, hideHeader = false }, ref) {
  const [insights, setInsights] = useState<Insights | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchInsights = useCallback(async () => {
    if (!conversationId) {
      setInsights(null);
      return;
    }
    
    // Only show loading on first fetch
    if (!insights) {
      setLoading(true);
    }
    setError(null);
    try {
      const res = await fetch(`/api/conversations/${conversationId}/insights`);
      if (!res.ok) throw new Error("Failed to fetch insights");
      const data = await res.json();
      setInsights(data);
    } catch (err) {
      // Only set error on first fetch failure
      if (!insights) {
        setError(err instanceof Error ? err.message : "Unknown error");
      }
    } finally {
      setLoading(false);
    }
  }, [conversationId, insights]);

  // Expose refresh method via ref
  useImperativeHandle(ref, () => ({
    refresh: fetchInsights,
  }), [fetchInsights]);

  useEffect(() => {
    if (!conversationId) {
      setInsights(null);
      return;
    }

    fetchInsights();

    // Refresh insights periodically (default 3 seconds)
    const interval = setInterval(fetchInsights, pollingInterval);
    return () => clearInterval(interval);
  }, [conversationId, pollingInterval]);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      {!hideHeader && (
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold">Insights & Recommendations</h2>
          <p className="text-xs text-muted-foreground">Real-time analysis</p>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {loading && !insights && (
          <div className="flex items-center justify-center py-8">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        )}

        {error && (
          <div className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {!conversationId && (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <div className="mb-3 rounded-full bg-muted p-3">
              <svg className="h-6 w-6 text-muted-foreground" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
            </div>
            <p className="text-sm font-medium">No conversation selected</p>
            <p className="text-xs text-muted-foreground">
              Insights will appear when a conversation is active
            </p>
          </div>
        )}

        {insights && (
          <>
            {/* Sentiment Gauge */}
            <SentimentCard sentiment={insights.sentiment} />

            {/* KPI Cards */}
            <div className="grid grid-cols-2 gap-2">
              <KPICard 
                label="Issue Complexity" 
                value={insights.customerKPIs.issueComplexity}
                variant={insights.customerKPIs.issueComplexity === "complex" ? "warning" : "default"}
              />
              <KPICard 
                label="Escalation Risk" 
                value={insights.customerKPIs.escalationRisk}
                variant={insights.customerKPIs.escalationRisk === "high" ? "danger" : insights.customerKPIs.escalationRisk === "medium" ? "warning" : "default"}
              />
              <KPICard 
                label="Messages" 
                value={`${insights.conversationKPIs.customerMessageCount + insights.conversationKPIs.agentMessageCount}`}
              />
              <KPICard 
                label="Duration" 
                value={formatDuration(insights.conversationKPIs.durationSeconds)}
              />
            </div>

            {/* Topic/Intent */}
            {insights.conversationKPIs.topicIntent && (
              <div className="rounded-lg border p-3">
                <p className="text-xs font-medium text-muted-foreground mb-1">Detected Topic</p>
                <p className="text-sm font-medium">{insights.conversationKPIs.topicIntent}</p>
              </div>
            )}

            {/* Summary */}
            {insights.summary && (
              <div className="rounded-lg bg-muted/50 p-3">
                <p className="text-xs font-medium text-muted-foreground mb-1">Summary</p>
                <p className="text-sm">{insights.summary}</p>
              </div>
            )}

            {/* Next Best Actions */}
            {insights.nextBestActions.length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-2">Suggested Actions</p>
                <div className="space-y-2">
                  {insights.nextBestActions.map((action) => (
                    <button
                      key={action.id}
                      onClick={() => onActionClick?.(action)}
                      className="w-full rounded-lg border p-3 text-left transition-colors hover:bg-muted/50"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <p className="text-sm font-medium">{action.label}</p>
                          <p className="text-xs text-muted-foreground">{action.description}</p>
                        </div>
                        <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
                          {action.type.replace("_", " ")}
                        </span>
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
});

function SentimentCard({ sentiment }: { sentiment: Sentiment }) {
  const label = sentiment.current.label;
  const score = sentiment.current.score;
  const frustration = sentiment.frustrationScore;

  const getSentimentColor = () => {
    if (label === "positive") return "text-green-500";
    if (label === "negative") return "text-red-500";
    return "text-yellow-500";
  };

  const getSentimentBg = () => {
    if (label === "positive") return "bg-green-500";
    if (label === "negative") return "bg-red-500";
    return "bg-yellow-500";
  };

  return (
    <div className="rounded-lg border p-4">
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs font-medium text-muted-foreground">Customer Sentiment</p>
        <span className={cn("text-sm font-semibold capitalize", getSentimentColor())}>
          {label}
        </span>
      </div>
      
      {/* Sentiment Bar */}
      <div className="relative h-2 rounded-full bg-muted overflow-hidden mb-2">
        <div 
          className={cn("absolute left-1/2 h-full transition-all", getSentimentBg())}
          style={{ 
            width: `${Math.abs(score) * 50}%`,
            transform: score >= 0 ? "translateX(0)" : "translateX(-100%)"
          }}
        />
        <div className="absolute left-1/2 top-0 h-full w-0.5 bg-border" />
      </div>
      
      <div className="flex justify-between text-[10px] text-muted-foreground">
        <span>Negative</span>
        <span>Neutral</span>
        <span>Positive</span>
      </div>

      {frustration > 0.3 && (
        <div className="mt-3 flex items-center gap-2 rounded-md bg-orange-500/10 px-2 py-1.5">
          <svg className="h-4 w-4 text-orange-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <span className="text-xs font-medium text-orange-600">
            Frustration detected ({Math.round(frustration * 100)}%)
          </span>
        </div>
      )}
    </div>
  );
}

function KPICard({ 
  label, 
  value, 
  variant = "default" 
}: { 
  label: string; 
  value: string; 
  variant?: "default" | "warning" | "danger";
}) {
  const variantStyles = {
    default: "bg-muted/50",
    warning: "bg-yellow-500/10 border-yellow-500/20",
    danger: "bg-red-500/10 border-red-500/20",
  };

  const textStyles = {
    default: "",
    warning: "text-yellow-600",
    danger: "text-red-600",
  };

  return (
    <div className={cn("rounded-lg border p-3", variantStyles[variant])}>
      <p className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">{label}</p>
      <p className={cn("text-lg font-semibold capitalize", textStyles[variant])}>{value}</p>
    </div>
  );
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins < 60) return `${mins}m ${secs}s`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins % 60}m`;
}
