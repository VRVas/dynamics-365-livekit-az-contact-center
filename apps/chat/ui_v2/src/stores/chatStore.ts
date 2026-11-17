import { create } from 'zustand';
import { v4 as uuidv4 } from 'uuid';
import { Message, HandoverState } from '@/types';
import { getProfiles, Profile } from '@/services/api';
import { template } from '@/config/template';

interface ChatStore {
  // Messages
  messages: Message[];
  addMessage: (message: Message | Omit<Message, 'id' | 'timestamp'>) => string;
  updateMessage: (id: string, content: string) => void;
  clearMessages: () => void;
  
  // Loading states
  isLoading: boolean;
  setIsLoading: (loading: boolean) => void;
  isStreaming: boolean;
  setIsStreaming: (streaming: boolean) => void;
  
  // Session
  conversationId: string | null;
  initSession: () => string;
  
  // Error handling
  error: string | null;
  setError: (error: string | null) => void;
  
  // Handover state
  handover: HandoverState;
  setHandover: (state: Partial<HandoverState>) => void;
  setHandoverState: (state: Partial<HandoverState>) => void; // Alias for consistency
  
  // Profile selection
  profiles: Profile[];
  selectedProfileId: string;
  profileLocked: boolean;
  loadProfiles: () => Promise<void>;
  setSelectedProfile: (id: string) => void;
  lockProfile: () => void;
}

const defaultHandoverState: HandoverState = {
  status: 'none',
  mode: 'ai',
  // Deprecated fields for backward compatibility
  isRequested: false,
  isActive: false,
  humanAgentConnected: false,
};

export const useChatStore = create<ChatStore>((set, get) => ({
  // Messages
  messages: [],
  
  addMessage: (message) => {
    // If message already has id, use it; otherwise generate one
    const id = 'id' in message && message.id ? message.id : uuidv4();
    const timestamp = 'timestamp' in message && message.timestamp ? message.timestamp : new Date();
    
    set((state) => ({
      messages: [
        ...state.messages,
        {
          ...message,
          id,
          timestamp,
        },
      ],
    }));
    return id;
  },
  
  updateMessage: (id, content) => {
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === id ? { ...msg, content, isStreaming: false } : msg
      ),
    }));
  },
  
  clearMessages: () => set({ messages: [] }),
  
  // Loading states
  isLoading: false,
  setIsLoading: (loading) => set({ isLoading: loading }),
  isStreaming: false,
  setIsStreaming: (streaming) => set({ isStreaming: streaming }),
  
  // Session
  conversationId: null,
  
  initSession: () => {
    const existing = get().conversationId;
    if (existing) return existing;
    
    const newId = uuidv4();
    set({ conversationId: newId });
    return newId;
  },
  
  // Error handling
  error: null,
  setError: (error) => set({ error }),
  
  // Handover state
  handover: defaultHandoverState,
  
  setHandover: (state) =>
    set((prev) => ({
      handover: { ...prev.handover, ...state },
    })),
    
  // Alias for setHandover (used by WebSocket hook)
  setHandoverState: (state) =>
    set((prev) => ({
      handover: { ...prev.handover, ...state },
    })),
    
  // Profile selection
  profiles: [],
  selectedProfileId: 'no_profile',
  profileLocked: false,
  
  loadProfiles: async () => {
    if (!template.features.chat || !template.features.profiles) return;
    try {
      console.log('[ChatStore] Loading profiles...');
      const profiles = await getProfiles();
      console.log('[ChatStore] Loaded profiles:', profiles);
      set({ profiles });
    } catch (error) {
      console.error('[ChatStore] Failed to load profiles:', error);
      set({ profiles: [] });
    }
  },
  
  setSelectedProfile: (id) => {
    const state = get();
    if (template.features.profiles && !state.profileLocked && state.profiles.some((profile) => profile.id === id)) {
      set({ selectedProfileId: id });
    }
  },
  
  lockProfile: () => set({ profileLocked: true }),
}));
