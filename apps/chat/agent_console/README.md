# Agent console

The console retains the backend chat queue, history, agent replies and handover
completion protocol. It contains no example operators, contacts or queued records.

## Configuration and extension boundary

`src/config/template.json` uses the same public schema-version-1 contract as the
customer UI. Its checked-in contents are neutral. The deployment agent replaces
it **only in a private staged checkout**, validates it and rebuilds.

Brand name, optional HTTPS/public-asset logo URL, colors, welcome text and locale
defaults affect login and the dashboard. The assistant name labels historical
assistant messages. Localized quick actions populate a reply draft; an agent
must explicitly send it. Supported language changes preserve drafts and active
sessions. `chat-first` puts the conversation before the queue; the other layouts
retain the queue-first arrangement.

The private preparer can copy `brand.logoFile` (PNG/JPEG/WebP) into
`public/brand-logo.png`, `.jpg` or `.webp`. Public `brand.logoUrl` accepts those
same-origin paths as well as HTTPS URLs. The private source path and `logoFile`
field never enter the public JSON.

Both `features.chat` and `features.agentConsole` must be true to mount login,
queue and conversation transport. `agentConsole` defaults to false. An earlier
login in local storage cannot bypass either gate. This console has no browser voice,
phone or profile selector: those flags apply to the customer UI and do not create
background requests here. Runtime provisioning/authorization belongs to the
backend, not these UI flags.
`agentAssist` independently gates Assist telemetry and is not inferred from any
private configuration.

Set `NEXT_PUBLIC_API_URL` to the absolute chat service base URL before building.
Without it the console stays offline; it never falls back to localhost or a
bundled cloud endpoint.

Never put credentials, knowledge text, system prompts or business records in the
public JSON. Additional UI components/styles can be generated in the private
checkout, without changing queue/handover wire keys or backend actions.

## Validation

```bash
npm run test:template
npx tsc --noEmit
npm run build
```

The sibling `ui_v2` browser suite also verifies console login, localization and
queue/accept/reply/completion with intercepted requests and WebSockets.
