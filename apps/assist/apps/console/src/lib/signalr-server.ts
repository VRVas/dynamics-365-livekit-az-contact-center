import crypto from 'crypto';
import { requireModule } from './deployment';

const hubName = 'agentassist';

interface ParsedConnectionString {
  endpoint: string;
  accessKey: string;
  version: string;
}

// Lazy-parsed connection string
let cachedConfig: ParsedConnectionString | null = null;

function getConfig(): ParsedConnectionString {
  requireModule('agentAssist');
  if (cachedConfig) return cachedConfig;

  const connectionString = process.env.SIGNALR_CONNECTION_STRING || '';
  if (!connectionString) {
    return { endpoint: '', accessKey: '', version: '1.0' };
  }

  const parts: Record<string, string> = {};
  
  connectionString.split(';').forEach((part) => {
    const [key, ...valueParts] = part.split('=');
    if (key && valueParts.length > 0) {
      parts[key.trim()] = valueParts.join('=').trim();
    }
  });

  cachedConfig = {
    endpoint: (parts['Endpoint'] || '').replace(/\/$/, ''),
    accessKey: parts['AccessKey'] || '',
    version: parts['Version'] || '1.0',
  };

  return cachedConfig;
}

/**
 * Generate a JWT token for SignalR client authentication
 */
function generateToken(audience: string, userId?: string, expiresInMinutes = 60): string {
  const config = getConfig();
  
  const expiry = Math.floor(Date.now() / 1000) + expiresInMinutes * 60;
  
  const header = {
    alg: 'HS256',
    typ: 'JWT',
  };

  const payload: Record<string, unknown> = {
    aud: audience,
    exp: expiry,
    iat: Math.floor(Date.now() / 1000),
  };

  if (userId) {
    payload.nameid = userId;
  }

  const base64Header = Buffer.from(JSON.stringify(header)).toString('base64url');
  const base64Payload = Buffer.from(JSON.stringify(payload)).toString('base64url');
  
  const signature = crypto
    .createHmac('sha256', config.accessKey)
    .update(`${base64Header}.${base64Payload}`)
    .digest('base64url');

  return `${base64Header}.${base64Payload}.${signature}`;
}

/**
 * Negotiate SignalR connection for client
 */
export function negotiate(userId?: string): { url: string; accessToken: string } {
  const config = getConfig();
  const url = `${config.endpoint}/client/?hub=${hubName}`;
  const token = generateToken(url, userId);
  
  return {
    url,
    accessToken: token,
  };
}

/**
 * Send a message to all clients in a group (conversation)
 */
export async function sendToGroup(
  group: string,
  target: string,
  message: unknown
): Promise<void> {
  const config = getConfig();
  if (!config.endpoint) {
    console.warn('SignalR not configured, skipping sendToGroup');
    return;
  }

  const url = `${config.endpoint}/api/v1/hubs/${hubName}/groups/${encodeURIComponent(group)}`;
  const token = generateToken(url);

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      target,
      arguments: [message],
    }),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Failed to send SignalR message: ${response.status} - ${text}`);
  }
}

/**
 * Send a message to all connected clients
 */
export async function broadcast(target: string, message: unknown): Promise<void> {
  const config = getConfig();
  if (!config.endpoint) {
    console.warn('SignalR not configured, skipping broadcast');
    return;
  }

  const url = `${config.endpoint}/api/v1/hubs/${hubName}`;
  const token = generateToken(url);

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      target,
      arguments: [message],
    }),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Failed to broadcast SignalR message: ${response.status} - ${text}`);
  }
}

/**
 * Send a message to a specific user
 */
export async function sendToUser(
  userId: string,
  target: string,
  message: unknown
): Promise<void> {
  const config = getConfig();
  if (!config.endpoint) {
    console.warn('SignalR not configured, skipping sendToUser');
    return;
  }

  const url = `${config.endpoint}/api/v1/hubs/${hubName}/users/${encodeURIComponent(userId)}`;
  const token = generateToken(url);

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      target,
      arguments: [message],
    }),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Failed to send SignalR message to user: ${response.status} - ${text}`);
  }
}

/**
 * Notify clients of a new message
 */
export async function notifyNewMessage(
  conversationId: string,
  message: {
    id: string;
    sender: 'customer' | 'ai_agent' | 'operator';
    content: string;
    timestamp: string;
  }
): Promise<void> {
  await sendToGroup(conversationId, 'newMessage', {
    type: 'new_message',
    conversationId,
    message,
  });
}

/**
 * Notify clients of updated insights
 */
export async function notifyInsightsUpdated(
  conversationId: string,
  insights: {
    sentiment: 'positive' | 'neutral' | 'negative';
    topics: string[];
    suggestedActions: string[];
  }
): Promise<void> {
  await sendToGroup(conversationId, 'insightsUpdated', {
    type: 'insights_updated',
    conversationId,
    insights,
  });
}

/**
 * Notify clients of conversation status change
 */
export async function notifyConversationUpdate(
  conversationId: string,
  status: 'active' | 'closed' | 'pending',
  eventType: 'conversation_started' | 'conversation_ended' | 'conversation_updated' = 'conversation_updated'
): Promise<void> {
  // Broadcast to all clients since this affects the conversation list
  await broadcast('conversationUpdated', {
    type: eventType,
    conversationId,
    status,
  });
}

export function subscriberFromToken(token: string): string | null {
  const config = getConfig();
  const parts = token.split('.');
  if (!config.accessKey || parts.length !== 3 || parts.some(part => !/^[A-Za-z0-9_-]+$/.test(part))) {
    return null;
  }
  const expected = crypto.createHmac('sha256', config.accessKey).update(`${parts[0]}.${parts[1]}`).digest();
  const signature = Buffer.from(parts[2], 'base64url');
  if (signature.length !== expected.length || !crypto.timingSafeEqual(signature, expected)) return null;
  try {
    const header: Record<string, unknown> = JSON.parse(Buffer.from(parts[0], 'base64url').toString());
    const payload: Record<string, unknown> = JSON.parse(Buffer.from(parts[1], 'base64url').toString());
    if (!header || !payload || header.alg !== 'HS256' ||
        payload.aud !== `${config.endpoint}/client/?hub=${hubName}` ||
        typeof payload.exp !== 'number' || payload.exp <= Date.now() / 1000 ||
        typeof payload.nameid !== 'string' || !payload.nameid) return null;
    return payload.nameid;
  } catch {
    return null;
  }
}

export async function setConversationMembership(userId: string, conversationId: string, join: boolean): Promise<void> {
  const config = getConfig();
  const audience = `${config.endpoint}/api/v1/hubs/${hubName}/groups/${encodeURIComponent(conversationId)}/users/${encodeURIComponent(userId)}`;
  const response = await fetch(join ? `${audience}?ttl=3600` : audience, {
    method: join ? 'PUT' : 'DELETE',
    headers: { Authorization: `Bearer ${generateToken(audience)}` },
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) {
    throw new Error(`SignalR group membership failed (HTTP ${response.status}).`);
  }
}

export { hubName };
