import { API_BASE_URL } from '@/constants';
import { DEFAULT_LOCALE } from '@/i18n/messages';
import { HandoverSuggestion } from '@/types';
import { template } from '@/config/template';

function chatUrl(path: string) {
  if (!template.features.chat || !API_BASE_URL) throw new Error('Chat service is not configured.');
  return `${API_BASE_URL}${path}`;
}

function handoverUrl(path: string) {
  const url = chatUrl(path);
  if (!template.features.agentConsole) throw new Error('Agent console is not enabled.');
  return url;
}

export interface SendMessageOptions {
  conversationId: string;
  message: string;
  profileId?: string; // Optional profile ID to send with the first message
  locale?: 'en' | 'ar';
  onToken: (token: string) => void;
  onComplete: () => void;
  onError: (error: string) => void;
  onHandoverSuggested?: (suggestion: HandoverSuggestion) => void;
  onHandoverRequested?: () => void;
  onAgentChange?: (agentName: string) => void; // Called when a new agent starts responding
}

// Stream marks from the API
type StreamMark = 'ping' | 'start' | 'delta' | 'function_result' | 'end' | 'response' | 'error' | 'handover_suggested' | 'handover_requested';

interface DeltaContent {
  content?: string | null;
  tool_calls?: Array<{
    function: {
      name: string;
      arguments: string;
    };
  }> | null;
}

export async function sendMessage({
  conversationId,
  message,
  profileId,
  locale = DEFAULT_LOCALE,
  onToken,
  onComplete,
  onError,
  onHandoverSuggested,
  onHandoverRequested,
  onAgentChange,
}: SendMessageOptions): Promise<void> {
  try {
    const url = chatUrl(`/conversation/${conversationId}/stream`);
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        message,
        media: null,
        profile_id: template.features.profiles ? profileId || null : null,
        locale,
      }),
    });

    console.log('[API] Response status:', response.status);
    
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const reader = response.body?.getReader();
    if (!reader) {
      throw new Error('No response body');
    }

    console.log('[API] Starting to read stream...');
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      
      if (done) {
        console.log('[API] Stream done');
        onComplete();
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      console.log('[API] Received chunk, buffer length:', buffer.length);
      
      // Process complete lines (newline-delimited JSON)
      const lines = buffer.split('\n');
      buffer = lines.pop() || ''; // Keep incomplete line in buffer

      for (const line of lines) {
        if (!line.trim()) continue;
        
        console.log('[API] Processing line:', line.substring(0, 100));
        
        try {
          const parsed = JSON.parse(line);
          
          // Format: [mark, content]
          if (Array.isArray(parsed) && parsed.length >= 2) {
            const [mark, content] = parsed as [StreamMark, unknown];
            // Enhanced logging for stream analysis
            if (mark === 'start') {
              console.log('[API] === STREAM START ===', content);
            } else if (mark === 'end') {
              console.log('[API] === STREAM END ===');
            } else if (mark === 'response') {
              console.log('[API] === STREAM RESPONSE ===', JSON.stringify(content).substring(0, 200));
            } else {
              console.log('[API] Mark:', mark);
            }
            
            // Skip ping messages (heartbeats)
            if (mark === 'ping') continue;
            if (mark === 'handover_requested') {
              if (template.features.agentConsole) onHandoverRequested?.();
              continue;
            }
            
            // Handle error messages
            if (mark === 'error') {
              const errorContent = content as { message?: string };
              onError(errorContent?.message || 'Unknown error');
              return;
            }
            
            // Handle start mark (agent name)
            if (mark === 'start') {
              // Agent started responding - notify about agent change
              const agentName = content as string;
              if (onAgentChange && agentName) {
                onAgentChange(agentName);
              }
              continue;
            }
            
            // Handle delta mark (streaming content)
            if (mark === 'delta') {
              const deltaContent = content as DeltaContent;
              if (deltaContent?.content) {
                console.log('[API] Token:', deltaContent.content);
                onToken(deltaContent.content);
              }
              // Tool calls are displayed separately in the original UI
              // For now, we skip them in the text stream
              continue;
            }
            
            // Handle function_result (tool response) - skip for text display
            if (mark === 'function_result') {
              continue;
            }
            
            // Handle end mark (message complete)
            if (mark === 'end') {
              continue; // Stream continues until 'response'
            }
            
            // Handle handover suggestion from AI
            if (mark === 'handover_suggested') {
              const suggestion = content as HandoverSuggestion;
              console.log('[API] Handover suggested:', suggestion);
              if (template.features.agentConsole && onHandoverSuggested) {
                onHandoverSuggested(suggestion);
              }
              continue;
            }
            
            // Handle response mark (final response with usage info)
            // Note: Don't return here! Multi-agent scenarios may have multiple response marks
            // The stream will end naturally when done=true, which calls onComplete()
            if (mark === 'response') {
              console.log('[API] Response mark received (continuing for multi-agent)');
              continue;
            }
          }
        } catch (e) {
          // If not JSON, skip
          console.warn('[API] Failed to parse stream line:', line, e);
        }
      }
    }
  } catch (error) {
    onError(error instanceof Error ? error.message : 'Unknown error');
  }
}

// Health check
export async function checkHealth(): Promise<boolean> {
  try {
    const response = await fetch(chatUrl('/health'));
    return response.ok;
  } catch {
    return false;
  }
}

// Handover API functions
export interface HandoverStatus {
  status: 'none' | 'requested' | 'active' | 'completed';
  requested_at: string | null;
  activated_at: string | null;
  human_agent_id: string | null;
  human_agent_name: string | null;
  escalation_reason: string | null;
}

export async function getHandoverStatus(conversationId: string): Promise<HandoverStatus> {
  const response = await fetch(handoverUrl(`/conversation/${conversationId}/handover-status`));
  if (!response.ok) {
    throw new Error(`Failed to get handover status: ${response.status}`);
  }
  return response.json();
}

export async function requestHandover(conversationId: string, reason?: string): Promise<{ success: boolean; message: string; status: HandoverStatus }> {
  const response = await fetch(handoverUrl(`/conversation/${conversationId}/request-handover`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: reason || 'Customer requested human assistance' }),
  });
  if (!response.ok) {
    throw new Error(`Failed to request handover: ${response.status}`);
  }
  return response.json();
}

export async function completeHandover(conversationId: string): Promise<{ success: boolean; message: string; status: HandoverStatus }> {
  const response = await fetch(handoverUrl(`/conversation/${conversationId}/complete-handover`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!response.ok) {
    throw new Error(`Failed to complete handover: ${response.status}`);
  }
  return response.json();
}

// Profile types
export interface Profile {
  id: string;
  display_name: string;
  display_name_ar?: string;
}

export interface ProfileDetail extends Profile {
  profile_data: string;
}

// Profile API functions
export async function getProfiles(): Promise<Profile[]> {
  if (!template.features.chat || !template.features.profiles || !API_BASE_URL) return [];
  try {
    const response = await fetch(`${API_BASE_URL}/profiles`);
    if (!response.ok) {
      throw new Error(`Failed to fetch profiles: ${response.status}`);
    }
    return response.json();
  } catch (error) {
    console.error('[API] Failed to fetch profiles:', error);
    return [];
  }
}

export async function getProfile(profileId: string): Promise<ProfileDetail | null> {
  if (!template.features.chat || !template.features.profiles || !API_BASE_URL) return null;
  try {
    const response = await fetch(`${API_BASE_URL}/profiles/${profileId}`);
    if (!response.ok) {
      return null;
    }
    return response.json();
  } catch (error) {
    console.error('[API] Failed to fetch profile:', error);
    return null;
  }
}
