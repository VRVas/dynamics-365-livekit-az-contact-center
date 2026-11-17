# Human handover

Each channel hands over differently. Select channels with the
[modular deployment guide](../../docs/deployment.md).

| Channel | Mechanism |
|---|---|
| Dynamics-native voice | The voice service leaves the call, then the controller runs native `TransferToAgent` in the same Dynamics conversation |
| Application phone, outbound calls | ACS transfer to an approved operator number: the `/call` request's `operator_number` or `AGENT_PHONE_NUMBER` |
| Custom text chat | The optional representative console and its chat queue and WebSocket protocol |

Native handover doesn't dial a representative's phone into an application
conference. See the
[call-flow diagram](../../docs/dynamics-native-voice/native-call-flow.html).

Application phone is off by default. Every outbound destination, including the
operator number, must be in the private `phonePolicy.allowedOutboundNumbers`;
profiles and generated records don't allow dialing. Inbound application calls
and browser sessions never transfer to a phone.

Automatic application conferencing is a separate option, off by default. It
needs an explicit `ACS_AUTO_CONFERENCE_NUMBER` in the same approved policy.
Don't enable it with native media.

No example in this repository authorizes a live call. Before each test call, get
the caller's readiness, consent and approval for charges. A transfer request
isn't proof that a person answered.

See the [voice service README](README.md) for settings and endpoints.
