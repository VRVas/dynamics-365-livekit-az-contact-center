# Human Agent Console Guide

This guide covers the optional custom-chat representative console. Select
`agent-console` through the [modular deployment guide](../../../docs/deployment.md).
It is not the native Dynamics representative workspace.

## Getting Started

### Accessing the Console

1. **Local Development**: Navigate to `http://localhost:8081`
2. **Production**: Navigate to your deployed Agent Console URL

### Logging In

1. Enter the **Agent ID** approved for this environment.
2. Enter your **Display Name** (this is shown to customers)
3. Click **Enter console**.

> 💡 In the current version, authentication is simplified. In production, integrate with your identity provider.

---

## Dashboard Overview

After logging in, you'll see the Agent Dashboard with two main panels:

### Left Panel: Waiting Queue

Shows customers waiting to be connected with a human agent:

| Column | Description |
|--------|-------------|
| **Session ID** | Unique identifier for the conversation |
| **Reason** | Why the customer requested human assistance |
| **Wait Time** | How long they've been waiting |
| **Accept Button** | Click to start chatting with this customer |

### Right Panel: Active Chat

Once you accept a session, this panel shows:
- **Customer's conversation history** (messages with AI before handover)
- **Live chat** with the customer
- **Message input** at the bottom
- **Complete handover** button and confirmation to finish the handover

---

## Workflow

### 1. Monitoring the Queue

- The queue updates automatically via WebSocket
- New customers appear at the top
- You'll see the reason they requested human help

### 2. Accepting a Session

1. Review the customer's reason for escalation
2. Click **Accept handover** on the session you want to handle
3. The customer is immediately connected to you
4. You'll see their conversation history with the AI

### 3. Viewing Conversation History

When you accept a session, you'll see:

```
📜 Conversation History
─────────────────────────
[Previous messages between customer and AI]

👤 You joined the conversation
─────────────────────────
[Live chat messages from this point]
```

This context helps you understand what the customer has already tried.

### 4. Chatting with the Customer

- Type your message in the input field
- Press **Enter** or click **Send**
- Messages are delivered instantly via WebSocket
- The customer sees your messages in real-time

### 5. Ending the Session

When the conversation is complete:

1. Click the **End Session** button (top-right of chat panel)
2. The customer is notified that the session has ended
3. The customer can continue chatting with the AI
4. You're returned to monitoring the queue

---

## Best Practices

### Response Time
- Accept sessions promptly to minimize customer wait time
- If you can't handle a session, leave it for another agent

### Using Context
- Always read the conversation history before responding
- Avoid asking questions the customer already answered

### Clear Communication
- Identify yourself: "Hi, I'm [Name], I'll be helping you today"
- Be clear about what you can and cannot do
- Provide specific next steps

### Ending Sessions
- Confirm the customer's issue is resolved before ending
- Provide a summary if appropriate
- End sessions cleanly so customers can continue with AI if needed

---

## Message Types

### From Customer
Customer messages appear in blue bubbles on the left side.

### Your Messages
Your messages appear in purple bubbles on the right side.

### Historical Messages (AI)
Messages from the AI conversation appear with a gray indicator and "📜" prefix.

### System Messages
System notifications (like "Agent joined") appear centered with gray styling.

---

## Troubleshooting

### Queue Not Updating
- Check your internet connection
- Look for "Disconnected" status in the header
- The page will automatically reconnect

### Can't Send Messages
- Ensure you have an active session
- Check if the session is still connected
- Try refreshing the page

### Customer Not Receiving Messages
- The customer may have disconnected
- Check the connection status
- If persistent, ask customer to refresh their page

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Send message |
| `Shift + Enter` | New line in message |

---

## Technical Details

### Ports (Local Development)
- **Agent Console**: `http://localhost:8081`
- **Customer UI**: `http://localhost:8080`
- **API**: `http://localhost:3000`

### WebSocket Endpoints
- Agent connections: `/ws/agent/{agent_id}`
- Customer connections: `/ws/customer/{session_id}`

### Session Flow
```
Customer requests handover
        ↓
Added to waiting queue
        ↓
Agent accepts session
        ↓
WebSocket routes messages bidirectionally
        ↓
Agent ends session
        ↓
Customer returns to AI chat
```

---

## FAQ

**Q: Can I handle multiple customers at once?**
A: Currently, the console supports one active session at a time. Future versions may support multiple sessions.

**Q: What happens if I disconnect during a session?**
A: The customer remains connected. When you reconnect, you can accept the session again from the queue.

**Q: Can I transfer a customer to another agent?**
A: Not in the current version. End the session and ask the customer to request human help again.

**Q: Are conversations saved?**
A: Yes, all messages (AI and human) are stored in the conversation history and visible in the CCAgent Console.

---

## Support

For technical issues with the Agent Console, contact your system administrator.
