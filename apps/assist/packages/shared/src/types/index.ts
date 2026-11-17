// Canonical message types for the Agent Assist platform

// Speaker types: customer, ai_agent (AI handling), operator (human agent)
export type Speaker = "customer" | "ai_agent" | "operator";
export type Channel = "chat" | "voice" | "email" | "other";
export type SessionStatus = "active" | "completed" | "abandoned";

export interface MessageMetadata {
  language?: string;
  sourceSystem?: string;
  transcriptionConfidence?: number;
  tags?: string[];
}

export interface Message {
  messageId: string;
  sessionId: string;           // Session this message belongs to
  conversationId?: string;     // Deprecated: alias for sessionId
  timestamp: string;
  speaker: Speaker;
  channel: Channel;
  text: string;
  metadata?: MessageMetadata;
}

// Deprecated: Use Session instead
export interface Conversation {
  conversationId: string;
  createdAt: string;
  updatedAt: string;
  channel: Channel;
  status: "active" | "closed" | "on-hold";
  messages: Message[];
  metadata?: Record<string, unknown>;
}

// Session represents a customer interaction (replaces Conversation)
export interface Session {
  // Identity
  id: string;                           // Internal session ID
  externalSessionId?: string;           // ID from source system (contact center)
  
  // Status
  status: SessionStatus;
  humanAttentionNeeded: boolean;        // Triggers alert in agent UI
  humanAttentionRequestedAt?: string;   // When escalation was requested
  escalationReason?: string;            // Why human attention was requested
  
  // Channel info
  channel: Channel;
  sourceSystem?: string;                // e.g., "voice", "web-chat"
  
  // Participants
  customerId?: string;                  // Customer identifier (if known)
  customerName?: string;                // Customer name (if provided)
  
  // Timestamps
  createdAt: string;                    // Session start
  updatedAt: string;                    // Last activity
  completedAt?: string;                 // Session end
  
  // Metrics (denormalized for list view)
  messageCount: number;
  customerMessageCount: number;
  aiAgentMessageCount: number;
  operatorMessageCount: number;
  
  // Context
  intent?: string;                      // Primary detected intent
  sentiment?: SentimentLabel;           // Current sentiment
  language?: string;                    // Detected language
  
  // Metadata
  metadata?: Record<string, unknown>;   // Custom fields from source system
}

// Sentiment types
export type SentimentLabel = "positive" | "neutral" | "negative";

export interface SentimentScore {
  label: SentimentLabel;
  score: number;
  confidence: number;
}

export interface SentimentTrend {
  current: SentimentScore;
  history: SentimentScore[];
  frustrationScore: number;
}

// KPI types
export type RiskLevel = "low" | "medium" | "high";
export type ComplexityLevel = "simple" | "moderate" | "complex";

export interface CustomerKPIs {
  issueComplexity: ComplexityLevel;
  escalationRisk: RiskLevel;
  churnRisk: RiskLevel;
}

export interface ConversationKPIs {
  durationSeconds: number;
  estimatedHandleTimeSeconds: number;
  customerMessageCount: number;
  agentMessageCount: number;
  firstTimeResolutionLikelihood: number;
  topicIntent?: string;
}

export interface ComplianceKPIs {
  greetingDetected: boolean;
  identityVerified: boolean;
  disclaimerMentioned: boolean;
}

// Next Best Action types
export type ActionType = "reply_template" | "workflow" | "knowledge_snippet" | "link";

export interface NextBestAction {
  id: string;
  type: ActionType;
  label: string;
  description: string;
  content?: string;
  url?: string;
  priority: number;
}

// Insights aggregate
export interface ConversationInsights {
  conversationId: string;
  updatedAt: string;
  sentiment: SentimentTrend;
  customerKPIs: CustomerKPIs;
  conversationKPIs: ConversationKPIs;
  complianceKPIs: ComplianceKPIs;
  nextBestActions: NextBestAction[];
  summary: string;
  extractedEntities: ExtractedEntities;
}

export interface ExtractedEntities {
  customerName?: string;
  customerId?: string;
  productsMentioned: string[];
  orderNumbers: string[];
  ticketNumbers: string[];
}

// Knowledge types
export interface KnowledgeSnippet {
  id: string;
  title: string;
  content: string;
  source: string;
  category: string;
  url?: string;
  lastUpdated: string;
  relevanceScore: number;
}

export interface AgentAssistChatRequest {
  question: string;
  conversationId?: string;
  conversationContext?: string;
}

export interface AgentAssistChatResponse {
  answer: string;
  citations: KnowledgeSnippet[];
  confidence: number;
}
