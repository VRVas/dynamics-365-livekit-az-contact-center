import { CosmosClient, Container, Database, CosmosClientOptions, SqlParameter } from '@azure/cosmos';
import { DefaultAzureCredential } from '@azure/identity';
import { requireModule } from './deployment';
import type {
  SentimentTrend,
  SentimentLabel,
  CustomerKPIs,
  ConversationKPIs,
  ComplianceKPIs,
  NextBestAction,
  ExtractedEntities,
  SessionStatus,
  Channel,
} from '@ccagent/shared';

// Speaker type for messages (customer, ai_agent, operator)
export type DBSpeaker = 'customer' | 'ai_agent' | 'operator';

// Session represents a customer interaction (replaces DBConversation)
export interface DBSession {
  id: string;
  sessionId: string;                    // Alias for id (for new code)
  conversationId: string;               // Partition key - same as id (for Cosmos DB)
  externalSessionId?: string;           // ID from source system
  
  // Status
  status: SessionStatus;
  humanAttentionNeeded: boolean;
  humanAttentionRequestedAt?: string;
  escalationReason?: string;
  
  // Channel info
  channel: Channel;
  sourceSystem?: string;
  
  // Participants
  customerId?: string;
  customerName?: string;
  
  // Timestamps
  createdAt: string;
  updatedAt: string;
  completedAt?: string;
  
  // Metrics
  messageCount: number;
  customerMessageCount: number;
  aiAgentMessageCount: number;
  operatorMessageCount: number;
  
  // Context
  intent?: string;
  sentiment?: SentimentLabel;
  language?: string;
  
  // Metadata
  metadata?: Record<string, unknown>;
}

// Legacy type alias for backward compatibility
export type DBConversation = DBSession;

export interface DBMessage {
  id: string;
  sessionId: string;                    // Partition key
  conversationId?: string;              // Deprecated: alias for sessionId
  sender: DBSpeaker;
  content: string;
  contentType?: string;
  timestamp: string;
  metadata?: Record<string, unknown>;
}

// Comprehensive insights stored in Cosmos DB
export interface DBInsights {
  id?: string;
  conversationId: string;
  updatedAt?: string;
  // Sentiment analysis
  sentiment: SentimentTrend;
  // KPIs
  customerKPIs: CustomerKPIs;
  conversationKPIs: ConversationKPIs;
  complianceKPIs: ComplianceKPIs;
  // Actions and summary
  nextBestActions: NextBestAction[];
  summary: string;
  extractedEntities: ExtractedEntities;
}

// Environment variables
const endpoint = process.env.COSMOS_DB_ENDPOINT || '';
const key = process.env.COSMOS_DB_KEY || '';
const databaseId = process.env.COSMOS_DB_DATABASE || 'agentassist';

// Container IDs
const CONTAINERS = {
  conversations: 'conversations',
  messages: 'messages',
  insights: 'insights',
} as const;

// Singleton client
let cosmosClient: CosmosClient | null = null;
let database: Database | null = null;
let containers: Record<keyof typeof CONTAINERS, Container> | null = null;

/**
 * Initialize the Cosmos DB client and ensure database/containers exist
 */
async function initializeClient(): Promise<{
  client: CosmosClient;
  database: Database;
  containers: Record<keyof typeof CONTAINERS, Container>;
}> {
  requireModule('agentAssist');
  if (cosmosClient && database && containers) {
    return { client: cosmosClient, database, containers };
  }

  if (!endpoint) {
    throw new Error('COSMOS_DB_ENDPOINT must be set');
  }

  const connectionPolicy = {
    requestTimeout: 30000,
    retryOptions: {
      maxRetryAttemptCount: 3,
      maxWaitTimeInSeconds: 30,
    },
  };

  if (key) {
    // Key-based auth (local development)
    const options: CosmosClientOptions = {
      endpoint,
      key,
      connectionPolicy,
    };
    cosmosClient = new CosmosClient(options);
  } else {
    // Managed Identity / AAD auth (Azure deployment). On ACA with a user-assigned identity,
    // DefaultAzureCredential must be told which managed identity to use via AZURE_CLIENT_ID.
    const credential = new DefaultAzureCredential({
      managedIdentityClientId: process.env.AZURE_CLIENT_ID,
    });
    const options: CosmosClientOptions = {
      endpoint,
      aadCredentials: credential,
      connectionPolicy,
    };
    cosmosClient = new CosmosClient(options);
  }

  // Database + containers are pre-created by infrastructure (Bicep). Under Cosmos data-plane
  // RBAC the app cannot create databases/containers, so use direct handles (not createIfNotExists).
  database = cosmosClient.database(databaseId);

  const containerIds = [CONTAINERS.conversations, CONTAINERS.messages, CONTAINERS.insights];
  const createdContainers: Partial<Record<keyof typeof CONTAINERS, Container>> = {};
  for (const id of containerIds) {
    createdContainers[id as keyof typeof CONTAINERS] = database.container(id);
  }

  containers = createdContainers as Record<keyof typeof CONTAINERS, Container>;

  return { client: cosmosClient, database, containers };
}

/**
 * Get initialized containers
 */
async function getContainers() {
  const { containers } = await initializeClient();
  return containers;
}

// ============= Session Operations (replaces Conversation) =============

export async function createSession(
  session: Omit<DBSession, 'createdAt' | 'updatedAt' | 'sessionId' | 'conversationId'>
): Promise<DBSession> {
  const { containers } = await initializeClient();
  const now = new Date().toISOString();

  const newSession: DBSession = {
    ...session,
    sessionId: session.id,        // Alias for id
    conversationId: session.id,   // Partition key = id
    humanAttentionNeeded: session.humanAttentionNeeded ?? false,
    messageCount: session.messageCount ?? 0,
    customerMessageCount: session.customerMessageCount ?? 0,
    aiAgentMessageCount: session.aiAgentMessageCount ?? 0,
    operatorMessageCount: session.operatorMessageCount ?? 0,
    createdAt: now,
    updatedAt: now,
  };

  const { resource } = await containers.conversations.items.create(newSession);
  return resource as DBSession;
}

// Backward compatible alias
export const createConversation = createSession;

export async function getSession(sessionId: string): Promise<DBSession | null> {
  const { containers } = await initializeClient();

  try {
    // Point read: item(id, partitionKeyValue) - partition key is /conversationId
    // We use sessionId as both the document id AND the conversationId partition key value
    console.log(`[cosmos-client] getSession: Looking up id="${sessionId}"`);
    const { resource, statusCode } = await containers.conversations.item(sessionId, sessionId).read<DBSession>();
    console.log(`[cosmos-client] getSession: status=${statusCode}, found=${!!resource}`);
    return resource || null;
  } catch (error: unknown) {
    const errCode = (error as Record<string, unknown>)?.code;
    const errMsg = (error as Error)?.message;
    console.log(`[cosmos-client] getSession: Error - code=${errCode}, message=${errMsg}`);
    
    if (errCode === 404) {
      // Fallback: try SQL query for legacy documents or partition key mismatch
      console.log(`[cosmos-client] getSession: Trying SQL query fallback`);
      try {
        const { resources } = await containers.conversations.items
          .query({
            query: 'SELECT * FROM c WHERE c.id = @id OR c.sessionId = @id OR c.conversationId = @id',
            parameters: [{ name: '@id', value: sessionId }],
          })
          .fetchAll();
        if (resources.length > 0) {
          return resources[0] as DBSession;
        }
      } catch (fallbackError) {
        console.log(`[cosmos-client] getSession: Fallback query failed:`, fallbackError);
      }
      return null;
    }
    throw error;
  }
}

// Backward compatible alias
export const getConversation = getSession;

export async function listSessions(options?: {
  limit?: number;
  offset?: number;
  status?: DBSession['status'];
  humanAttentionNeeded?: boolean;
}): Promise<{ sessions: DBSession[]; total: number }> {
  const { containers } = await initializeClient();

  // Build query - always require sessionId to filter out legacy documents
  const conditions: string[] = ['IS_DEFINED(c.sessionId)'];
  const parameters: SqlParameter[] = [];

  if (options?.status) {
    conditions.push('c.status = @status');
    parameters.push({ name: '@status', value: options.status });
  }

  if (options?.humanAttentionNeeded !== undefined) {
    conditions.push('c.humanAttentionNeeded = @humanAttentionNeeded');
    parameters.push({ name: '@humanAttentionNeeded', value: options.humanAttentionNeeded });
  }

  const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

  // Get total count
  const countQuery = `SELECT VALUE COUNT(1) FROM c ${whereClause}`;
  const { resources: countResult } = await containers.conversations.items
    .query({ query: countQuery, parameters })
    .fetchAll();
  const total = countResult[0] || 0;

  // Get paginated results - fetch all and sort in JS due to Cosmos DB SQL limitations
  const limit = options?.limit || 50;
  const offset = options?.offset || 0;

  // Simple query without complex ORDER BY - we'll sort in JavaScript
  const dataQuery = `SELECT * FROM c ${whereClause}`;

  const { resources: rawSessions } = await containers.conversations.items
    .query({ query: dataQuery, parameters })
    .fetchAll();

  // Sort by priority in JavaScript:
  // 1. humanAttentionNeeded AND active (highest priority)
  // 2. active
  // 3. completed
  // 4. abandoned
  // Within each group, sort by updatedAt DESC
  const sortedSessions = (rawSessions as DBSession[]).sort((a, b) => {
    const getPriority = (s: DBSession) => {
      if (s.humanAttentionNeeded && s.status === 'active') return 0;
      if (s.status === 'active') return 1;
      if (s.status === 'completed') return 2;
      return 3; // abandoned
    };
    
    const priorityDiff = getPriority(a) - getPriority(b);
    if (priorityDiff !== 0) return priorityDiff;
    
    // Within same priority, sort by updatedAt DESC
    return new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime();
  });

  // Apply pagination
  const paginatedSessions = sortedSessions.slice(offset, offset + limit);

  return { sessions: paginatedSessions, total };
}

// Backward compatible alias
export async function listConversations(options?: {
  limit?: number;
  offset?: number;
  status?: DBSession['status'];
  agentId?: string;
}): Promise<{ conversations: DBSession[]; total: number }> {
  const result = await listSessions(options);
  return { conversations: result.sessions, total: result.total };
}

export async function updateSession(
  sessionId: string,
  updates: Partial<Omit<DBSession, 'id' | 'createdAt'>>
): Promise<DBSession | null> {
  const { containers } = await initializeClient();

  const existing = await getSession(sessionId);
  if (!existing) return null;

  const updated: DBSession = {
    ...existing,
    ...updates,
    updatedAt: new Date().toISOString(),
  };

  const { resource } = await containers.conversations
    .item(sessionId, sessionId)
    .replace(updated);

  return resource as DBSession;
}

// Backward compatible alias
export const updateConversation = updateSession;

// ============= Message Operations =============

export async function createMessage(message: Omit<DBMessage, 'timestamp'>): Promise<DBMessage> {
  const { containers } = await initializeClient();
  
  // Use sessionId, fall back to conversationId for backward compatibility
  const sessionId = message.sessionId || message.conversationId;
  if (!sessionId) {
    throw new Error('sessionId is required');
  }

  const newMessage: DBMessage = {
    ...message,
    sessionId,
    conversationId: sessionId,  // Partition key = sessionId
    timestamp: new Date().toISOString(),
  };

  const { resource } = await containers.messages.items.create(newMessage);

  // Update session's updatedAt and message counts
  const session = await getSession(sessionId);
  if (session) {
    const countUpdate: Partial<DBSession> = {
      messageCount: (session.messageCount || 0) + 1,
    };
    
    // Update speaker-specific counts
    if (message.sender === 'customer') {
      countUpdate.customerMessageCount = (session.customerMessageCount || 0) + 1;
    } else if (message.sender === 'ai_agent') {
      countUpdate.aiAgentMessageCount = (session.aiAgentMessageCount || 0) + 1;
    } else if (message.sender === 'operator') {
      countUpdate.operatorMessageCount = (session.operatorMessageCount || 0) + 1;
    }
    
    await updateSession(sessionId, countUpdate);
  }

  return resource as DBMessage;
}

export async function getMessage(messageId: string, sessionId: string): Promise<DBMessage | null> {
  const { containers } = await initializeClient();

  try {
    const { resource } = await containers.messages.item(messageId, sessionId).read<DBMessage>();
    return resource || null;
  } catch (error: unknown) {
    if (error && typeof error === 'object' && 'code' in error && error.code === 404) {
      return null;
    }
    throw error;
  }
}

export async function listMessages(
  sessionId: string,
  options?: { limit?: number; offset?: number }
): Promise<{ messages: DBMessage[]; total: number }> {
  const { containers } = await initializeClient();

  // Get total count - check both sessionId and conversationId for backward compatibility
  const countQuery = 'SELECT VALUE COUNT(1) FROM m WHERE m.sessionId = @sessionId OR m.conversationId = @sessionId';
  const { resources: countResult } = await containers.messages.items
    .query({
      query: countQuery,
      parameters: [{ name: '@sessionId', value: sessionId }],
    })
    .fetchAll();
  const total = countResult[0] || 0;

  // Get paginated results
  const limit = options?.limit || 50;
  const offset = options?.offset || 0;

  const dataQuery = `
    SELECT * FROM m 
    WHERE m.sessionId = @sessionId OR m.conversationId = @sessionId
    ORDER BY m.timestamp ASC 
    OFFSET @offset LIMIT @limit
  `;

  const { resources: messages } = await containers.messages.items
    .query({
      query: dataQuery,
      parameters: [
        { name: '@sessionId', value: sessionId },
        { name: '@offset', value: offset },
        { name: '@limit', value: limit },
      ],
    })
    .fetchAll();

  return { messages: messages as DBMessage[], total };
}

// ============= Insights Operations =============

export async function upsertInsights(insights: DBInsights): Promise<DBInsights> {
  const { containers } = await initializeClient();

  const doc = {
    ...insights,
    id: insights.conversationId, // Use conversationId as id for easy lookup
    updatedAt: new Date().toISOString(),
  };

  const { resource } = await containers.insights.items.upsert(doc);

  return resource as unknown as DBInsights;
}

export async function getInsights(conversationId: string): Promise<DBInsights | null> {
  const { containers } = await initializeClient();

  try {
    const { resource } = await containers.insights
      .item(conversationId, conversationId)
      .read<DBInsights>();
    return resource || null;
  } catch (error: unknown) {
    if (error && typeof error === 'object' && 'code' in error && error.code === 404) {
      return null;
    }
    throw error;
  }
}

// ============= Delete Operations =============

/** Delete a single session document. Partition key (/conversationId) value == sessionId. */
export async function deleteSession(sessionId: string): Promise<boolean> {
  const { containers } = await initializeClient();
  try {
    await containers.conversations.item(sessionId, sessionId).delete();
    return true;
  } catch (error: unknown) {
    if (error && typeof error === 'object' && 'code' in error && error.code === 404) return false;
    throw error;
  }
}

/** Delete every message belonging to a session. Messages are partitioned by /sessionId. */
export async function deleteSessionMessages(sessionId: string): Promise<number> {
  const { containers } = await initializeClient();
  const { resources } = await containers.messages.items
    .query<{ id: string; sessionId?: string; conversationId?: string }>({
      query: 'SELECT c.id, c.sessionId, c.conversationId FROM c WHERE c.sessionId = @id OR c.conversationId = @id',
      parameters: [{ name: '@id', value: sessionId }],
    })
    .fetchAll();

  let deleted = 0;
  for (const msg of resources) {
    const pk = msg.sessionId ?? msg.conversationId ?? sessionId;
    try {
      await containers.messages.item(msg.id, pk).delete();
      deleted++;
    } catch (error: unknown) {
      if (!(error && typeof error === 'object' && 'code' in error && error.code === 404)) throw error;
    }
  }
  return deleted;
}

/** Delete the insights document for a session. Insights are partitioned by /conversationId. */
export async function deleteSessionInsights(conversationId: string): Promise<boolean> {
  const { containers } = await initializeClient();
  try {
    await containers.insights.item(conversationId, conversationId).delete();
    return true;
  } catch (error: unknown) {
    if (error && typeof error === 'object' && 'code' in error && error.code === 404) return false;
    throw error;
  }
}

/** Cascade delete: messages -> insights -> session. */
export async function deleteSessionCascade(
  sessionId: string
): Promise<{ deleted: boolean; messagesDeleted: number }> {
  const messagesDeleted = await deleteSessionMessages(sessionId);
  await deleteSessionInsights(sessionId);
  const deleted = await deleteSession(sessionId);
  return { deleted, messagesDeleted };
}

// ============= Utility Functions =============

export async function ensureSessionExists(
  sessionId: string, 
  channel: Channel = 'chat'
): Promise<DBSession> {
  let session = await getSession(sessionId);

  if (!session) {
    session = await createSession({
      id: sessionId,
      status: 'active',
      humanAttentionNeeded: false,
      channel,
      messageCount: 0,
      customerMessageCount: 0,
      aiAgentMessageCount: 0,
      operatorMessageCount: 0,
    });
  }

  return session;
}

// Backward compatible alias
export const ensureConversationExists = ensureSessionExists;

export { getContainers, initializeClient };
