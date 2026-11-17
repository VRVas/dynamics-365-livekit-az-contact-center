import * as signalR from '@microsoft/signalr';

// SignalR hub connection singleton
let hubConnection: signalR.HubConnection | null = null;
let subscriberToken: string | null = null;

// Event handlers registry
type MessageHandler = (message: MessageEvent) => void;
type InsightsHandler = (insights: InsightsEvent) => void;
type ConversationHandler = (conversation: ConversationEvent) => void;

interface MessageEvent {
  type: 'new_message';
  conversationId: string;
  message: {
    id: string;
    sender: 'customer' | 'ai_agent' | 'operator';
    content: string;
    timestamp: string;
  };
}

interface InsightsEvent {
  type: 'insights_updated';
  conversationId: string;
  insights: {
    sentiment: 'positive' | 'neutral' | 'negative';
    topics: string[];
    suggestedActions: string[];
  };
}

interface ConversationEvent {
  type: 'conversation_started' | 'conversation_ended' | 'conversation_updated';
  conversationId: string;
  status?: 'active' | 'closed' | 'pending';
}

const eventHandlers = {
  message: new Set<MessageHandler>(),
  insights: new Set<InsightsHandler>(),
  conversation: new Set<ConversationHandler>(),
};

/**
 * Negotiate with our API to get the SignalR Service URL and access token
 */
async function negotiateConnection(): Promise<{ url: string; accessToken: string }> {
  const response = await fetch('/api/negotiate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });

  if (!response.ok) {
    throw new Error(`Negotiate failed: ${response.status}`);
  }

  const data = await response.json();
  return { url: data.url, accessToken: data.accessToken };
}

/**
 * Get or create the SignalR hub connection
 */
export async function getConnection(): Promise<signalR.HubConnection> {
  if (hubConnection && hubConnection.state === signalR.HubConnectionState.Connected) {
    return hubConnection;
  }

  // Get connection info from our negotiate endpoint
  const { url, accessToken } = await negotiateConnection();
  subscriberToken = accessToken;

  hubConnection = new signalR.HubConnectionBuilder()
    .withUrl(url, {
      accessTokenFactory: () => accessToken,
      skipNegotiation: true,  // Skip negotiation since we already have the URL
      transport: signalR.HttpTransportType.WebSockets,  // Use WebSockets directly
    })
    .withAutomaticReconnect({
      nextRetryDelayInMilliseconds: (retryContext) => {
        // Exponential backoff: 0s, 2s, 4s, 8s, max 30s
        const delay = Math.min(Math.pow(2, retryContext.previousRetryCount) * 1000, 30000);
        return delay;
      },
    })
    .configureLogging(signalR.LogLevel.Information)
    .build();

  // Set up event handlers
  hubConnection.on('newMessage', (data: MessageEvent) => {
    eventHandlers.message.forEach((handler) => handler(data));
  });

  hubConnection.on('insightsUpdated', (data: InsightsEvent) => {
    eventHandlers.insights.forEach((handler) => handler(data));
  });

  hubConnection.on('conversationUpdated', (data: ConversationEvent) => {
    eventHandlers.conversation.forEach((handler) => handler(data));
  });

  // Handle connection events
  hubConnection.onclose((error) => {
    console.log('SignalR connection closed', error);
  });

  hubConnection.onreconnecting((error) => {
    console.log('SignalR reconnecting...', error);
  });

  hubConnection.onreconnected((connectionId) => {
    console.log('SignalR reconnected:', connectionId);
  });

  return hubConnection;
}

/**
 * Start the SignalR connection
 */
export async function startConnection(): Promise<void> {
  const connection = await getConnection();
  
  if (connection.state === signalR.HubConnectionState.Disconnected) {
    await connection.start();
    console.log('SignalR connected');
  }
}

/**
 * Stop the SignalR connection
 */
export async function stopConnection(): Promise<void> {
  if (hubConnection) {
    await hubConnection.stop();
    hubConnection = null;
    subscriberToken = null;
  }
}

/**
 * Subscribe to new message events
 */
export function onNewMessage(handler: MessageHandler): () => void {
  eventHandlers.message.add(handler);
  return () => eventHandlers.message.delete(handler);
}

/**
 * Subscribe to insights update events
 */
export function onInsightsUpdated(handler: InsightsHandler): () => void {
  eventHandlers.insights.add(handler);
  return () => eventHandlers.insights.delete(handler);
}

/**
 * Subscribe to conversation events
 */
export function onConversationUpdate(handler: ConversationHandler): () => void {
  eventHandlers.conversation.add(handler);
  return () => eventHandlers.conversation.delete(handler);
}

/**
 * Join a conversation group to receive updates
 */
export async function joinConversation(conversationId: string): Promise<void> {
  await updateMembership(conversationId, 'join');
}

/**
 * Leave a conversation group
 */
export async function leaveConversation(conversationId: string): Promise<void> {
  await updateMembership(conversationId, 'leave');
}

async function updateMembership(conversationId: string, action: 'join' | 'leave'): Promise<void> {
  if (!subscriberToken) throw new Error('Connect to realtime messaging before changing subscriptions.');
  const response = await fetch('/api/realtime/groups', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${subscriberToken}` },
    body: JSON.stringify({ conversationId, action }),
  });
  if (!response.ok) throw new Error(`Realtime subscription failed (HTTP ${response.status}).`);
}

/**
 * Get connection state
 */
export function getConnectionState(): signalR.HubConnectionState | null {
  return hubConnection?.state ?? null;
}

/**
 * Check if connected
 */
export function isConnected(): boolean {
  return hubConnection?.state === signalR.HubConnectionState.Connected;
}

// Export types for consumers
export type { MessageEvent, InsightsEvent, ConversationEvent };
