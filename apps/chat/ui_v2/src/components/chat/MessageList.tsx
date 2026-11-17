'use client';

import { useEffect, useRef } from 'react';
import { Message } from '@/types';
import { MessageBubble } from './MessageBubble';

interface MessageListProps {
  messages: Message[];
  onFollowUp?: (question: string) => void;
  followUpDisabled?: boolean;
}

export function MessageList({ messages, onFollowUp, followUpDisabled }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  if (messages.length === 0) {
    return null;
  }

  return (
    <div className="space-y-2">
      {messages.map((message) => (
        <MessageBubble
          key={message.id}
          message={message}
          onFollowUp={onFollowUp}
          followUpDisabled={followUpDisabled}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
