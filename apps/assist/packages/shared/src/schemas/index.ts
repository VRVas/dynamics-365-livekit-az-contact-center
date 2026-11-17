// Re-export Zod validation schemas
export * from "./validation";

// JSON Schema definitions for API validation (legacy, kept for reference)
export const MessageJsonSchema = {
  type: "object",
  required: ["messageId", "conversationId", "timestamp", "speaker", "channel", "text"],
  properties: {
    messageId: { type: "string" },
    conversationId: { type: "string" },
    timestamp: { type: "string", format: "date-time" },
    speaker: { type: "string", enum: ["customer", "agent", "system"] },
    channel: { type: "string", enum: ["chat", "voice", "email", "other"] },
    text: { type: "string" },
    metadata: {
      type: "object",
      properties: {
        language: { type: "string" },
        sourceSystem: { type: "string" },
        transcriptionConfidence: { type: "number", minimum: 0, maximum: 1 },
        tags: { type: "array", items: { type: "string" } },
      },
    },
  },
} as const;

export const IngestMessageRequestJsonSchema = {
  type: "object",
  required: ["conversation_id", "message_id", "timestamp", "speaker", "channel", "text"],
  properties: {
    conversation_id: { type: "string" },
    message_id: { type: "string" },
    timestamp: { type: "string", format: "date-time" },
    speaker: { type: "string", enum: ["customer", "agent", "system"] },
    channel: { type: "string", enum: ["chat", "voice", "email", "other"] },
    text: { type: "string" },
    metadata: {
      type: "object",
      properties: {
        language: { type: "string" },
        source_system: { type: "string" },
        transcription_confidence: { type: "number" },
        tags: { type: "array", items: { type: "string" } },
      },
    },
  },
} as const;
