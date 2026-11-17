// Message types
export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: Date | string;
  isStreaming?: boolean;
  isFromHuman?: boolean; // True if message is from human agent (not AI)
}

// Starter button type
export interface Starter {
  label: string;
  message: string;
  icon?: string;
}

// Chat state
export interface ChatState {
  messages: Message[];
  isLoading: boolean;
  isStreaming: boolean;
  error: string | null;
  conversationId: string | null;
}

// API types
export interface StreamChunk {
  type: 'token' | 'complete' | 'error' | 'handover_suggested';
  content?: string;
  error?: string;
  reason?: string;
  keywords?: string[];
}

// Handover status from API
export type HandoverStatus = 'none' | 'suggested' | 'requested' | 'active' | 'completed';
export type HandoverMode = 'ai' | 'human';

// Handover types (for Phase 1+)
export interface HandoverState {
  status: HandoverStatus;
  mode: HandoverMode;
  queuePosition?: number;
  suggestedReason?: string;
  suggestedKeywords?: string[];
  requestedAt?: Date;
  humanAgentId?: string;
  humanAgentName?: string;
  // Deprecated - use status/mode instead
  isRequested?: boolean;
  isActive?: boolean;
  humanAgentConnected?: boolean;
}

// Handover suggestion from AI
export interface HandoverSuggestion {
  reason: string;
  keywords: string[];
}

// WebSocket message types
export interface WSMessage {
  type: 'message' | 'handover_request' | 'agent_joined' | 'agent_left' | 'typing';
  payload: unknown;
}
