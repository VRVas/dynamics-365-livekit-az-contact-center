// Zod schemas for runtime validation
import { z } from "zod";

// ============================================================================
// Enums
// ============================================================================

// Speaker types: customer, ai_agent (AI handling), operator (human agent)
export const SpeakerSchema = z.enum(["customer", "ai_agent", "operator"]);
export const ChannelSchema = z.enum(["chat", "voice", "email", "other"]);
export const ConversationStatusSchema = z.enum(["active", "closed", "on-hold"]);
export const SessionStatusSchema = z.enum(["active", "completed", "abandoned"]);
export const SentimentLabelSchema = z.enum(["positive", "neutral", "negative"]);
export const RiskLevelSchema = z.enum(["low", "medium", "high"]);
export const ComplexityLevelSchema = z.enum(["simple", "moderate", "complex"]);
export const ActionTypeSchema = z.enum([
  "reply_template",
  "workflow",
  "knowledge_snippet",
  "link",
]);

// ============================================================================
// Core Message Schemas
// ============================================================================

export const MessageMetadataSchema = z.object({
  language: z.string().optional(),
  sourceSystem: z.string().optional(),
  transcriptionConfidence: z.number().min(0).max(1).optional(),
  tags: z.array(z.string()).optional(),
});

export const MessageSchema = z.object({
  messageId: z.string().uuid(),
  conversationId: z.string().min(1),
  timestamp: z.string().datetime(),
  speaker: SpeakerSchema,
  channel: ChannelSchema,
  text: z.string(),
  metadata: MessageMetadataSchema.optional(),
});

export const ConversationSchema = z.object({
  conversationId: z.string().min(1),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
  channel: ChannelSchema,
  status: ConversationStatusSchema,
  messages: z.array(MessageSchema).optional(),
  metadata: z.record(z.string(), z.unknown()).optional(),
});

// Session schema (replaces Conversation for new API)
export const SessionSchema = z.object({
  id: z.string().min(1),
  externalSessionId: z.string().optional(),
  status: SessionStatusSchema,
  humanAttentionNeeded: z.boolean(),
  humanAttentionRequestedAt: z.string().datetime().optional(),
  escalationReason: z.string().optional(),
  channel: ChannelSchema,
  sourceSystem: z.string().optional(),
  customerId: z.string().optional(),
  customerName: z.string().optional(),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
  completedAt: z.string().datetime().optional(),
  messageCount: z.number().int().default(0),
  customerMessageCount: z.number().int().default(0),
  aiAgentMessageCount: z.number().int().default(0),
  operatorMessageCount: z.number().int().default(0),
  intent: z.string().optional(),
  sentiment: SentimentLabelSchema.optional(),
  language: z.string().optional(),
  metadata: z.record(z.string(), z.unknown()).optional(),
});

// ============================================================================
// Ingest API Schemas
// ============================================================================

export const IngestMessageRequestSchema = z.object({
  session_id: z.string().min(1).optional(),        // Preferred: session ID
  conversation_id: z.string().min(1).optional(),   // Deprecated: alias for session_id
  message_id: z.string().min(1),
  timestamp: z.string().datetime(),
  speaker: SpeakerSchema,
  channel: ChannelSchema,
  text: z.string().min(1),
  metadata: z
    .object({
      language: z.string().optional(),
      source_system: z.string().optional(),
      transcription_confidence: z.number().min(0).max(1).optional(),
      tags: z.array(z.string()).optional(),
    })
    .optional(),
}).refine(
  (data) => data.session_id || data.conversation_id,
  { message: "Either session_id or conversation_id must be provided" }
);

export const IngestMessageResponseSchema = z.object({
  success: z.boolean(),
  messageId: z.string(),
  sessionId: z.string(),
  conversationId: z.string(),  // Deprecated: same as sessionId
  isNewSession: z.boolean().optional(),
  isNewConversation: z.boolean().optional(),  // Deprecated: same as isNewSession
});

// ============================================================================
// Session API Schemas
// ============================================================================

// POST /api/sessions - Create new session
export const CreateSessionRequestSchema = z.object({
  externalSessionId: z.string().optional(),
  channel: ChannelSchema,
  sourceSystem: z.string().optional(),
  customerId: z.string().optional(),
  customerName: z.string().optional(),
  metadata: z.record(z.string(), z.unknown()).optional(),
});

// PATCH /api/sessions/{id} - Update session
export const UpdateSessionRequestSchema = z.object({
  status: SessionStatusSchema.optional(),
  humanAttentionNeeded: z.boolean().optional(),
  escalationReason: z.string().optional(),
  customerId: z.string().optional(),
  customerName: z.string().optional(),
  intent: z.string().optional(),
  metadata: z.record(z.string(), z.unknown()).optional(),
});

// GET /api/sessions - Query params
export const SessionListQuerySchema = z.object({
  status: SessionStatusSchema.optional(),
  humanAttentionNeeded: z.boolean().optional(),
  limit: z.number().int().min(1).max(100).default(50),
  continuationToken: z.string().optional(),
});

// Session summary for list view
export const SessionSummarySchema = z.object({
  id: z.string(),
  externalSessionId: z.string().optional(),
  status: SessionStatusSchema,
  humanAttentionNeeded: z.boolean(),
  humanAttentionRequestedAt: z.string().datetime().optional(),
  channel: ChannelSchema,
  sourceSystem: z.string().optional(),
  customerName: z.string().optional(),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
  messageCount: z.number().int(),
  intent: z.string().optional(),
  sentiment: SentimentLabelSchema.optional(),
});

export const SessionListResponseSchema = z.object({
  sessions: z.array(SessionSummarySchema),
  continuationToken: z.string().optional(),
});

// ============================================================================
// Conversation List Schemas (Deprecated - use Session schemas)
// ============================================================================

export const ConversationSummarySchema = z.object({
  conversationId: z.string(),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
  channel: ChannelSchema,
  status: ConversationStatusSchema,
  messageCount: z.number().int(),
  lastMessage: z.string().optional(),
});

export const ConversationListResponseSchema = z.object({
  conversations: z.array(ConversationSummarySchema),
  continuationToken: z.string().optional(),
});

export const MessageListResponseSchema = z.object({
  messages: z.array(MessageSchema),
  hasMore: z.boolean().optional(),
});

// ============================================================================
// Insights Schemas
// ============================================================================

export const SentimentScoreSchema = z.object({
  label: SentimentLabelSchema,
  score: z.number().min(-1).max(1),
  confidence: z.number().min(0).max(1),
});

export const SentimentTrendSchema = z.object({
  current: SentimentScoreSchema,
  history: z.array(SentimentScoreSchema),
  frustrationScore: z.number().min(0).max(1),
});

export const CustomerKPIsSchema = z.object({
  issueComplexity: ComplexityLevelSchema,
  escalationRisk: RiskLevelSchema,
  churnRisk: RiskLevelSchema,
});

export const ConversationKPIsSchema = z.object({
  durationSeconds: z.number().int(),
  estimatedHandleTimeSeconds: z.number().int().optional(),
  customerMessageCount: z.number().int(),
  agentMessageCount: z.number().int(),
  firstTimeResolutionLikelihood: z.number().min(0).max(1).optional(),
  topicIntent: z.string().optional(),
});

export const ComplianceKPIsSchema = z.object({
  greetingDetected: z.boolean(),
  identityVerified: z.boolean(),
  disclaimerMentioned: z.boolean(),
});

export const NextBestActionSchema = z.object({
  id: z.string(),
  type: ActionTypeSchema,
  label: z.string(),
  description: z.string(),
  content: z.string().optional(),
  url: z.string().url().optional(),
  priority: z.number().int().min(1),
});

export const ExtractedEntitiesSchema = z.object({
  customerName: z.string().optional(),
  customerId: z.string().optional(),
  productsMentioned: z.array(z.string()).default([]),
  orderNumbers: z.array(z.string()).default([]),
  ticketNumbers: z.array(z.string()).default([]),
});

export const ConversationInsightsSchema = z.object({
  conversationId: z.string(),
  updatedAt: z.string().datetime(),
  sentiment: SentimentTrendSchema,
  customerKPIs: CustomerKPIsSchema,
  conversationKPIs: ConversationKPIsSchema,
  complianceKPIs: ComplianceKPIsSchema,
  nextBestActions: z.array(NextBestActionSchema),
  summary: z.string(),
  extractedEntities: ExtractedEntitiesSchema,
});

// ============================================================================
// Knowledge Schemas
// ============================================================================

export const KnowledgeSearchRequestSchema = z.object({
  query: z.string().min(1),
  topK: z.number().int().min(1).max(20).default(5),
  category: z.string().optional(),
  includeContent: z.boolean().default(true),
});

export const KnowledgeSnippetSchema = z.object({
  id: z.string(),
  title: z.string(),
  content: z.string(),
  source: z.string(),
  category: z.string(),
  url: z.string().url().optional(),
  lastUpdated: z.string().datetime().optional(),
  relevanceScore: z.number().min(0).max(1).optional(),
});

export const KnowledgeSearchResponseSchema = z.object({
  results: z.array(KnowledgeSnippetSchema),
  totalCount: z.number().int().optional(),
});

// ============================================================================
// Agent Assist Chat Schemas
// ============================================================================

export const AgentAssistChatRequestSchema = z.object({
  question: z.string().min(1),
  conversationId: z.string().optional(),
  conversationContext: z.string().optional(),
});

export const AgentAssistChatResponseSchema = z.object({
  answer: z.string(),
  citations: z.array(KnowledgeSnippetSchema),
  confidence: z.number().min(0).max(1),
});

// ============================================================================
// SignalR Schemas
// ============================================================================

export const SignalRConnectionInfoSchema = z.object({
  url: z.string().url(),
  accessToken: z.string(),
});

// ============================================================================
// Error Schemas
// ============================================================================

export const ErrorDetailSchema = z.object({
  field: z.string().optional(),
  message: z.string(),
});

export const ErrorResponseSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    details: z.array(ErrorDetailSchema).optional(),
  }),
});

// ============================================================================
// Query Parameter Schemas
// ============================================================================

export const ConversationListQuerySchema = z.object({
  status: ConversationStatusSchema.optional(),
  channel: ChannelSchema.optional(),
  limit: z.coerce.number().int().min(1).max(100).default(20),
  continuationToken: z.string().optional(),
});

export const MessageListQuerySchema = z.object({
  limit: z.coerce.number().int().min(1).max(200).default(50),
  before: z.string().datetime().optional(),
});
