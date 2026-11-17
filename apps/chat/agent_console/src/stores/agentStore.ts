import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface ChatMessage {
  id: string;
  content: string;
  sender: 'customer' | 'agent';
  timestamp: Date;
  isHistory?: boolean;  // True if message is from before handover (AI conversation)
}

interface ActiveSession {
  sessionId: string;
  customerId?: string;
  startedAt: Date;
  messages: ChatMessage[];
}

interface QueueEntry {
  sessionId: string;
  reason?: string;
  waitingTime: number;
  queuedAt: Date;
}

interface AgentStore {
  // Agent info
  agentId: string | null;
  agentName: string | null;
  isLoggedIn: boolean;
  
  // Connection
  isConnected: boolean;
  setConnected: (connected: boolean) => void;
  
  // Queue
  queue: QueueEntry[];
  setQueue: (queue: QueueEntry[]) => void;
  
  // Active session
  activeSession: ActiveSession | null;
  setActiveSession: (session: ActiveSession | null) => void;
  addMessage: (message: ChatMessage) => void;
  
  // Actions
  login: (agentId: string, name: string) => void;
  logout: () => void;
}

export const useAgentStore = create<AgentStore>()(
  persist(
    (set, get) => ({
      // Agent info
      agentId: null,
      agentName: null,
      isLoggedIn: false,
      
      // Connection
      isConnected: false,
      setConnected: (connected) => set({ isConnected: connected }),
      
      // Queue
      queue: [],
      setQueue: (queue) => set({ queue }),
      
      // Active session
      activeSession: null,
      setActiveSession: (session) => set({ activeSession: session }),
      addMessage: (message) => {
        const session = get().activeSession;
        if (session) {
          set({
            activeSession: {
              ...session,
              messages: [...session.messages, message],
            },
          });
        }
      },
      
      // Actions
      login: (agentId, name) => set({
        agentId,
        agentName: name,
        isLoggedIn: true,
      }),
      
      logout: () => set({
        agentId: null,
        agentName: null,
        isLoggedIn: false,
        isConnected: false,
        activeSession: null,
        queue: [],
      }),
    }),
    {
      name: 'agent-store',
      partialize: (state) => ({
        agentId: state.agentId,
        agentName: state.agentName,
        isLoggedIn: state.isLoggedIn,
      }),
    }
  )
);
