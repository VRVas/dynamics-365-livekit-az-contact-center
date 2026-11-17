# Configurable customer chat

Start with [the modular deployment guide](../../docs/deployment.md).
The active chat implementation is [orchestrator](orchestrator), with
[customer UI](ui_v2) and the optional [representative console](agent_console).

## Selected behavior

- `web-chat` deploys the orchestrator and customer UI.
- `agent-console` adds custom-chat human handover and its representative UI.
- `crm`, `knowledge` and `email` independently select their existing tools.
- Browser voice and application phone use the separate voice service.
- Native Dynamics voice uses its own native conversation/queue, not the custom
  chat handover protocol.

There are no bundled customer records or business facts. Private preparation
injects approved branding, additive persona, optional knowledge and profile hints.
Without CRM, chat does not attempt to load Dataverse customer context.

An embedded knowledge file does not require Foundry IQ. Retrieval is a separate
selection with approved sources. Disabled tools are not registered.

## Handover and identity

When selected, custom-chat handover retains its queue, WebSocket messages,
history and completion lifecycle. Unselected handover has no UI controls,
background subscription or published handover tool.

Profile hints are not authentication. The backend obtains authoritative CRM
data and binds operations to the active conversation rather than a model-supplied
customer ID. Uncertain writes remain uncertain until readback verifies them.
Production identity and public exposure require a separate deployment review.

See [channel integration](../../docs/channel-integration.md) for shared API and
optional Assist boundaries. Use the root prepared manifest, not per-app
deployment files or another environment's URLs/keys.
