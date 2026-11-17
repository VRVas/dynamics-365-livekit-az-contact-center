// Minimal client for the chat orchestrator's operator websocket, so a supervisor in the
// assist console can talk to a customer on a chat session. The orchestrator relays an operator
// chat_message to the customer's /ws/customer and also emits it back into the assist transcript.

export function getOrchestratorBase(): string {
  return (
    process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ||
    'http://localhost:8000'
  );
}

export function getOrchestratorAgentWsUrl(agentId: string): string {
  const base = getOrchestratorBase().replace(/^http/, 'ws');
  return `${base}/ws/agent/${encodeURIComponent(agentId)}`;
}
