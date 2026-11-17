# Customer contact center UI

Next.js chat, browser voice and outbound phone controls.

## Configuration and extension boundary

`src/config/template.json` is the public, build-time UI contract (schema version
1). The checked-in template is neutral: English/Arabic, chat only, no representative handover, logo,
profiles, phone country, description or suggested prompts. The deployment agent
replaces this file **only in a private staged checkout**, validates it, and builds
the app. `src/config/template.ts` provides its types, not a runtime configuration
service.

- Brand/assistant names, optional HTTPS or public-asset logo URL, primary/accent
  colors, welcome text and localized quick actions drive the UI.
  The private preparer's `brand.logoFile` may supply PNG/JPEG/WebP; it copies the
  image into `public/brand-logo.png`, `.jpg` or `.webp` and emits the corresponding
  `/brand-logo.*` URL. Neither `logoFile` nor source filesystem paths enter the
  public JSON.
- `split` shows chat and call controls side by side; `chat-first` initially
  collapses call controls; `voice-first` gives them more space. On small screens
  the panels stack: a `voice-first` call panel uses the visible height and the
  chat follows below, while the other layouts limit it to half the screen. A
  single selected channel fills the workspace. Call controls remain accessible
  on small screens.
- Chat, browser voice and phone dialing mount independently. Profiles are a chat
  extension: both `chat` and `profiles` must be enabled. Choices come only from the
  backend; errors or an empty response never generate fallback records.
- `phone` represents approved outbound UI: the private preparer enables it only
  when application telephony and outbound-destination approval are both selected.
  Dialing uses the entered number and never requires a CRM/profile selection.
  Numbers and approval policy remain server-side. Inbound PSTN can remain enabled
  separately while `phone` is false and the outbound UI is absent.
- `agentConsole` enables custom-chat representative handover and the separate
  agent console. When false, handover controls, sockets and HTTP actions stay
  disabled, including when a stream suggests handover.
- `agentAssist` enables Assist's agent/supervisor UI and telemetry components.
  When false, they do not subscribe or poll; this does not change the separate
  CRM/knowledge API layer. Both flags default to false and use only this
  public contract, never private runtime configuration.
- `locale.default` controls server rendering and the initial language.
  `locale.supported` limits explicit language choices. Deliberate choices persist
  locally. Switching language preserves drafts and active conversations.
- `locale.dialingCountryCode` can be empty. Without a selected country code,
  enter the full international number, including `+`.
- Quick-action `label` is displayed; its localized `prompt` is sent as a normal
  chat message. Defaults contain no actions.

Set `NEXT_PUBLIC_API_URL` and, for selected voice/phone modules,
`NEXT_PUBLIC_VOICE_URL` to absolute service base URLs **before building**. There
are no cloud or localhost defaults; absent endpoints do not initiate requests.
These public values must never contain credentials.

The JSON must not contain credentials, knowledge text, system prompts or business
records. UI gates are not backend authorization. Runtime services, profiles,
permissions and server-side actions belong to the deployment/backend layer.
Additional presentation components or styles can be generated inside the private
app checkout; preserve transport and handover hooks. Accepted HUMAIN voice
choices, media transport and interruption handling remain server-compatible.

Readex Pro's Arabic and Latin subsets are self-hosted by Next.js; the font loader
normally fetches them at build time, not from visitors' browsers.

## Local development and validation

With the existing dependencies installed:

```bash
npm run dev                 # port 8080
npm run test:template
npm run test:voice
npm run test:ui
npx tsc --noEmit
npm run lint
npm run build
```

Browser tests need Chromium (`npx playwright install chromium`) and the sibling
`agent_console` dependencies. They own local servers on ports 38471–38474,
intercept backend requests, and exercise neutral defaults, private test-only
configured copies, RTL/LTR switching, phone validation and customer/agent
handover. They never call a voice provider. The configured copy lives under
ignored `.test-workspace`, not in the source config.

`tests/browser/ui-evidence.spec.ts` checks that the voice orb and the Start
button are fully visible together on first load, without scrolling, at 390×844
and 1440×1000, in Arabic and English. A control fails if the viewport or a
scrolling container cuts off any part of it. The spec also saves screenshots of
the mobile voice, listening and chat states (`mobile-voice.png`,
`mobile-voice-listening.png`, `mobile-chat.png`) under `test-results/`; use
`--output` to choose another folder. Each capture waits until the controls'
cumulative opacity is at least 0.99. The listening state uses a fake microphone
and a mocked voice WebSocket.
