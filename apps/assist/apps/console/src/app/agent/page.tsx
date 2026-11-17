"use client";

import React, { useState, useCallback, useRef, useEffect } from "react";
import { ConversationPanel, InsightsPanel, KnowledgeChatPanel, SessionListPanel } from "@/components/agent";
import { AssistUnavailable, TemplateBrand } from "@/components/template-brand";
import { assistTitle, locale, template } from "@/config/template";

// Panel configuration
type PanelId = "sessions" | "conversation" | "insights" | "knowledge";

interface PanelConfig {
  id: PanelId;
  title: string;
  icon: React.ReactNode;
}

const PANELS: PanelConfig[] = [
  { id: "sessions", title: "Sessions", icon: <ListIcon /> },
  { id: "conversation", title: "Conversation", icon: <ChatIcon /> },
  { id: "insights", title: "Insights", icon: <LightbulbIcon /> },
  { id: "knowledge", title: "Knowledge", icon: <BookIcon /> },
];

// Default panel widths (percentages of available width)
const DEFAULT_WIDTHS: Record<PanelId, number> = {
  sessions: 20,
  conversation: 25,
  insights: 25,
  knowledge: 30,
};

function ListIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
      <line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
    </svg>
  );
}

function ChatIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function LightbulbIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 18h6" /><path d="M10 22h4" />
      <path d="M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14" />
    </svg>
  );
}

function BookIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </svg>
  );
}

function ChevronIcon({ direction }: { direction: "left" | "right" }) {
  return (
    <svg 
      xmlns="http://www.w3.org/2000/svg" 
      width="14" 
      height="14" 
      viewBox="0 0 24 24" 
      fill="none" 
      stroke="currentColor" 
      strokeWidth="2" 
      strokeLinecap="round" 
      strokeLinejoin="round"
      className={direction === "left" ? "" : "rotate-180"}
    >
      <polyline points="15 18 9 12 15 6" />
    </svg>
  );
}

// Resizable divider component
function ResizeDivider({ onResize, position }: { onResize: (deltaX: number) => void; position: number }) {
  const isDragging = useRef(false);
  const startX = useRef(0);

  const handleMouseDown = (e: React.MouseEvent) => {
    isDragging.current = true;
    startX.current = e.clientX;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDragging.current) return;
      const deltaX = (e.clientX - startX.current) * (locale === "ar" ? -1 : 1);
      startX.current = e.clientX;
      onResize(deltaX);
    };

    const handleMouseUp = () => {
      isDragging.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  };

  return (
    <div
      className="group relative w-1 shrink-0 cursor-col-resize bg-border hover:bg-primary/50 active:bg-primary transition-colors"
      onMouseDown={handleMouseDown}
      title="Drag to resize"
    >
      <div className="absolute inset-y-0 -left-1 -right-1" />
    </div>
  );
}

export default function AgentPage() {
  return template.features.agentAssist ? <EnabledAgentPage /> : <AssistUnavailable />;
}

function EnabledAgentPage() {
  const [sessionId, setSessionId] = useState<string | undefined>(undefined);
  const [inputSessionId, setInputSessionId] = useState<string>("");
  const [aiQuery, setAiQuery] = useState<string | undefined>(undefined);
  const [replyDraft, setReplyDraft] = useState<string>("");
  const [attentionCount, setAttentionCount] = useState(0);
  
  // Track which panels are collapsed
  const [collapsedPanels, setCollapsedPanels] = useState<Set<PanelId>>(new Set());
  
  // Track panel widths (in pixels)
  const [panelWidths, setPanelWidths] = useState<Record<PanelId, number>>({
    sessions: 0,
    conversation: 0,
    insights: 0,
    knowledge: 0,
  });
  
  const containerRef = useRef<HTMLDivElement>(null);
  const initialized = useRef(false);

  // Initialize panel widths based on container width
  useEffect(() => {
    if (containerRef.current && !initialized.current) {
      const containerWidth = containerRef.current.offsetWidth;
      const collapsedWidth = 40; // Width of collapsed panel buttons
      const collapsedCount = collapsedPanels.size;
      const availableWidth = containerWidth - (collapsedCount * collapsedWidth);
      
      setPanelWidths({
        sessions: (DEFAULT_WIDTHS.sessions / 100) * availableWidth,
        conversation: (DEFAULT_WIDTHS.conversation / 100) * availableWidth,
        insights: (DEFAULT_WIDTHS.insights / 100) * availableWidth,
        knowledge: (DEFAULT_WIDTHS.knowledge / 100) * availableWidth,
      });
      initialized.current = true;
    }
  }, [collapsedPanels.size]);

  // Responsive: rescale panel widths proportionally whenever the container/window resizes, so the
  // layout always fills the viewport instead of staying at the first-render pixel widths.
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      const containerWidth = el.offsetWidth;
      if (!containerWidth) return;
      const availableWidth = containerWidth - collapsedPanels.size * 40;
      if (availableWidth <= 0) return;
      setPanelWidths((prev) => {
        const visible = (Object.keys(prev) as PanelId[]).filter((id) => !collapsedPanels.has(id));
        const total = visible.reduce((s, id) => s + (prev[id] || 0), 0);
        if (total <= 0) {
          return {
            sessions: (DEFAULT_WIDTHS.sessions / 100) * availableWidth,
            conversation: (DEFAULT_WIDTHS.conversation / 100) * availableWidth,
            insights: (DEFAULT_WIDTHS.insights / 100) * availableWidth,
            knowledge: (DEFAULT_WIDTHS.knowledge / 100) * availableWidth,
          };
        }
        const scale = availableWidth / total;
        if (Math.abs(scale - 1) < 0.01) return prev;
        const next = { ...prev };
        for (const id of visible) next[id] = Math.max(150, (prev[id] || 0) * scale);
        return next;
      });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [collapsedPanels]);

  // Get ordered visible panels
  const getVisiblePanels = useCallback(() => {
    return PANELS.filter(p => !collapsedPanels.has(p.id));
  }, [collapsedPanels]);

  // Handle resize between two panels
  const handleResize = useCallback((leftPanelId: PanelId, rightPanelId: PanelId, deltaX: number) => {
    setPanelWidths(prev => {
      const minWidth = 150;
      const newLeftWidth = Math.max(minWidth, prev[leftPanelId] + deltaX);
      const newRightWidth = Math.max(minWidth, prev[rightPanelId] - deltaX);
      
      // Don't allow resize if it would make a panel too small
      if (newLeftWidth < minWidth || newRightWidth < minWidth) {
        return prev;
      }
      
      return {
        ...prev,
        [leftPanelId]: newLeftWidth,
        [rightPanelId]: newRightWidth,
      };
    });
  }, []);

  const togglePanel = useCallback((panelId: PanelId) => {
    setCollapsedPanels(prev => {
      const next = new Set(prev);
      if (next.has(panelId)) {
        next.delete(panelId);
      } else {
        // Don't allow collapsing all panels - keep at least one open
        if (next.size < 3) {
          next.add(panelId);
        }
      }
      return next;
    });
  }, []);

  const isCollapsed = (panelId: PanelId) => collapsedPanels.has(panelId);

  // Sessions are supplied by the live channels.
  const handleNewSession = () => {
    const newSessionId = `session-${Date.now()}`;
    setSessionId(newSessionId);
  };

  // Load an existing session by ID
  const handleLoadSession = () => {
    if (inputSessionId.trim()) {
      setSessionId(inputSessionId.trim());
      setInputSessionId("");
    }
  };

  // Handle session selection from list
  const handleSessionSelect = useCallback((selectedSessionId: string) => {
    setSessionId(selectedSessionId);
  }, []);

  // Handle "Ask AI about this" from conversation panel
  const handleAskAI = useCallback((messageContent: string) => {
    const query = `The customer said: "${messageContent}"\n\nHow should I respond to this? What are the relevant policies?`;
    setAiQuery(query);
  }, []);

  // Handle copying text to the reply draft
  const handleCopyToReply = useCallback((text: string) => {
    setReplyDraft(text);
    console.log("Copied to reply draft:", text);
  }, []);

  // Collapsed panel button component
  const CollapsedPanelButton = ({ panel }: { panel: PanelConfig }) => (
    <button
      onClick={() => togglePanel(panel.id)}
      className="flex h-full w-10 shrink-0 flex-col items-center justify-center gap-2 border-r bg-muted/50 hover:bg-muted transition-colors"
      title={`Expand ${panel.title}`}
    >
      <span className="text-muted-foreground">{panel.icon}</span>
      <span className="text-[10px] font-medium text-muted-foreground [writing-mode:vertical-lr] rotate-180">
        {panel.title}
      </span>
      <ChevronIcon direction="right" />
    </button>
  );

  // Panel header with collapse button
  const PanelHeader = ({ panel, children }: { panel: PanelConfig; children?: React.ReactNode }) => (
    <div className="flex h-10 shrink-0 items-center justify-between border-b bg-muted/30 px-3">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground">{panel.icon}</span>
        <span className="text-sm font-medium">{panel.title}</span>
        {children}
      </div>
      <button
        onClick={() => togglePanel(panel.id)}
        className="flex h-6 w-6 items-center justify-center rounded hover:bg-muted transition-colors"
        title={`Collapse ${panel.title}`}
      >
        <ChevronIcon direction="left" />
      </button>
    </div>
  );

  // Render a panel with optional resize divider
  const renderPanel = (panel: PanelConfig, index: number, visiblePanels: PanelConfig[]) => {
    const isLast = index === visiblePanels.length - 1;
    const nextPanel = !isLast ? visiblePanels[index + 1] : null;
    
    const panelContent = (
      <div 
        key={panel.id}
        className="flex flex-col overflow-hidden"
        style={{ 
          width: panelWidths[panel.id] || 'auto',
          minWidth: 150,
          flexShrink: 0,
          flexGrow: isLast ? 1 : 0,
        }}
      >
        <PanelHeader panel={panel}>
          {panel.id === "sessions" && attentionCount > 0 && (
            <span className="flex items-center gap-1 rounded-full bg-red-500/10 px-2 py-0.5 text-xs font-medium text-red-600">
              <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
              {attentionCount} needs attention
            </span>
          )}
        </PanelHeader>
        <div className="flex-1 overflow-hidden">
          {panel.id === "sessions" && (
            <SessionListPanel
              selectedSessionId={sessionId}
              onSessionSelect={handleSessionSelect}
              pollingInterval={1000}
              onAttentionCountChange={setAttentionCount}
            />
          )}
          {panel.id === "conversation" && (
            <ConversationPanel 
              conversationId={sessionId} 
              onConversationChange={setSessionId}
              onAskAI={handleAskAI}
              replyDraft={replyDraft}
              onReplyDraftChange={setReplyDraft}
              hideHeader
            />
          )}
          {panel.id === "insights" && (
            <InsightsPanel 
              conversationId={sessionId}
              hideHeader
              onActionClick={(action) => {
                console.log("Action clicked:", action);
                const actionWithContent = action as { content?: string };
                if (actionWithContent.content) {
                  setReplyDraft(actionWithContent.content);
                }
              }}
            />
          )}
          {panel.id === "knowledge" && (
            <KnowledgeChatPanel 
              conversationId={sessionId}
              onCopyToReply={handleCopyToReply}
              initialQuery={aiQuery}
              hideHeader
            />
          )}
        </div>
      </div>
    );

    if (isLast || !nextPanel) {
      return panelContent;
    }

    return (
      <React.Fragment key={`${panel.id}-with-divider`}>
        {panelContent}
        <ResizeDivider 
          position={index}
          onResize={(deltaX) => handleResize(panel.id, nextPanel.id, deltaX)} 
        />
      </React.Fragment>
    );
  };

  const visiblePanels = getVisiblePanels();

  return (
    <div className="flex h-screen flex-col bg-background">
      {/* Header */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b px-6">
        <div className="flex items-center gap-4">
          <TemplateBrand />
          <div className="h-6 w-px bg-border" />
          <h1 className="text-lg font-semibold">{assistTitle}</h1>
          <span className="flex items-center gap-1.5 rounded-full bg-green-500/10 px-2.5 py-1 text-xs font-medium text-green-600">
            <span className="h-1.5 w-1.5 rounded-full bg-green-500 animate-pulse" />
            Online
          </span>
        </div>
        <div className="flex items-center gap-4">
          {!sessionId ? (
            <div className="flex items-center gap-2">
              <input
                type="text"
                placeholder="Enter session ID..."
                value={inputSessionId}
                onChange={(e) => setInputSessionId(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleLoadSession()}
                className="rounded-md border px-3 py-1.5 text-xs w-48"
              />
              <button
                onClick={handleLoadSession}
                disabled={!inputSessionId.trim()}
                className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                Load
              </button>
              <span className="text-xs text-muted-foreground">or</span>
              <button
                onClick={handleNewSession}
                disabled
                title="Sessions are created automatically from live calls and chats"
                className="rounded-md border px-3 py-1.5 text-xs font-medium opacity-50 cursor-not-allowed"
              >
                Start New
              </button>
            </div>
          ) : (
            <button
              onClick={() => setSessionId(undefined)}
              className="rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted"
            >
              Change Session
            </button>
          )}
          <div className="text-sm text-muted-foreground">
            Service representative
          </div>
        </div>
      </header>

      {/* Main 4-Panel Layout with Collapsible and Resizable Panels */}
      <div ref={containerRef} className="flex flex-1 overflow-x-auto overflow-y-hidden">
        {PANELS.map((panel, index) => {
          if (isCollapsed(panel.id)) {
            return <CollapsedPanelButton key={panel.id} panel={panel} />;
          }
          
          const visibleIndex = visiblePanels.findIndex(p => p.id === panel.id);
          return renderPanel(panel, visibleIndex, visiblePanels);
        })}
      </div>
    </div>
  );
}
