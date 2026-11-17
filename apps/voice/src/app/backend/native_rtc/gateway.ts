import {
  CallClient, LocalAudioStream, Features,
  type Call, type CallAgent, type MediaStatsCollector,
} from "@azure/communication-calling";
import { AzureCommunicationTokenCredential } from "@azure/communication-common";
import { PcmQueue } from "./pcm";
import { PauseGate } from "./pause";

interface Setup {
  token: string;
  websocketUrl: string;
  expectedCallId: string;
}

let socket: WebSocket | undefined;
let context: AudioContext | undefined;
let agent: CallAgent | undefined;
let call: Call | undefined;
let credential: AzureCommunicationTokenCredential | undefined;
let localAudio: LocalAudioStream | undefined;
let collector: MediaStatsCollector | undefined;
let queue: PcmQueue | undefined;
let stopped = false;
let mediaReady = false;
let pauseGate: PauseGate | undefined;

function report(kind: string, fields: Record<string, string | number | boolean> = {}): void {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ kind, ...fields }));
}

function fail(code: string): void {
  mediaReady = false;
  queue?.clear();
  report("error", { code });
}

async function stop(): Promise<void> {
  if (stopped) return;
  stopped = true;
  mediaReady = false;
  queue?.clear();
  collector?.dispose();
  try {
    if (call && call.state !== "Disconnected") await call.hangUp();
  } finally {
    if (agent) await agent.dispose();
    credential?.dispose();
    if (context && context.state !== "closed") await context.close();
    socket?.close();
  }
}

async function start(settings: Setup): Promise<void> {
  const wsUrl = new URL(settings.websocketUrl);
  if (wsUrl.hostname !== "localhost" || wsUrl.protocol !== "ws:" ||
      !/^[0-9a-f-]{36}$/.test(settings.expectedCallId)) {
    throw new Error("Invalid private native audio setup.");
  }
  socket = new WebSocket(settings.websocketUrl);
  socket.binaryType = "arraybuffer";
  const connectedSocket = socket;
  await new Promise<void>((resolve, reject) => {
    connectedSocket.onopen = () => resolve();
    connectedSocket.onerror = () => reject(new Error("Private media connection failed."));
  });
  context = new AudioContext({ sampleRate: 48000 });
  await context.resume();
  const destination = context.createMediaStreamDestination();
  queue = new PcmQueue(context, destination);
  pauseGate = new PauseGate(() => queue?.clear(), async () => {
    if (!call || call.state !== "Connected" || stopped) throw new Error("Native call is not connected.");
    const current = call;
    if (!current.isMuted) await current.mute();
    if (!current.isMuted) {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => {
          current.off("isMutedChanged", changed);
          reject(new Error("Native microphone mute was not confirmed."));
        }, 3000);
        function changed() {
          if (current.isMuted) {
            clearTimeout(timer);
            current.off("isMutedChanged", changed);
            resolve();
          }
        }
        current.on("isMutedChanged", changed);
        changed();
      });
    }
  });
  localAudio = new LocalAudioStream(destination.stream);
  connectedSocket.onmessage = (event: MessageEvent) => {
    try {
      if (event.data instanceof ArrayBuffer) {
        if (pauseGate?.paused) return;
        if (!mediaReady || stopped) throw new Error("Audio received before media readiness.");
        queue?.enqueue(event.data);
      } else if (typeof event.data === "string") {
        const message = JSON.parse(event.data);
        if (message?.kind === "pause") {
          void pauseGate!.pause(message).then((receipt) => {
            report("paused", { ...receipt, muted: true, queued_frames: 0 });
          }).catch(() => fail("pause_unconfirmed"));
          return;
        }
        if (typeof message !== "object" || message === null || !("kind" in message) || message.kind !== "flush") {
          throw new Error("Unsupported private media command.");
        }
        queue?.clear();
        report("flushed");
      } else {
        throw new Error("Unsupported media payload.");
      }
    } catch {
      fail("invalid_audio_or_queue_overflow");
    }
  };
  connectedSocket.onclose = () => {
    if (!stopped) void stop().catch(() => console.error("Native voice cleanup failed."));
  };

  const client = new CallClient();
  const devices = await client.getDeviceManager();
  const permission = await devices.askDevicePermission({ audio: true, video: false });
  if (!permission.audio) throw new Error("Software audio initialization failed.");
  report("phase", { stage: "devices_ready" });
  credential = new AzureCommunicationTokenCredential(settings.token);
  agent = await client.createCallAgent(credential, { displayName: "HUMAIN" });
  let accepted = false;
  agent.on("incomingCall", async ({ incomingCall }) => {
    try {
      if (accepted || stopped) {
        await incomingCall.reject();
        return;
      }
      accepted = true;
      report("phase", { stage: "invitation_received" });
      call = await incomingCall.accept({ audioOptions: { muted: true } });
      report("phase", { stage: "invitation_accepted" });
      const current = call;
      let starting = false;
      const changed = async () => {
        try {
          if (current.state === "Disconnected") {
            report("disconnected");
            return;
          }
          if (current.state !== "Connected" || starting || stopped) return;
          starting = true;
          if (current.id.toLowerCase() !== settings.expectedCallId) {
            fail("wrong_native_call");
            await current.hangUp();
            return;
          }
          const stream = localAudio;
          if (!stream) throw new Error("Custom audio stream is missing.");
          await current.startAudio(stream);
          if (current.isMuted) await current.unmute();
          if (!current.localAudioStreams.includes(stream)) throw new Error("Custom audio was not selected.");
          report("connected", { call_id: current.id.toLowerCase() });
          collector = current.feature(Features.MediaStats).createCollector();
          let reports = 0;
          collector.on("sampleReported", (sample) => {
            if (stopped) return;
            for (const audio of sample.audio.send) {
              if (!mediaReady && (audio.bitrate ?? 0) > 0 && current.state === "Connected") {
                mediaReady = true;
                report("media_ready", { call_id: current.id.toLowerCase() });
              }
              if (++reports % 5 === 0) {
                report("metrics", { input_level: audio.audioInputLevel ?? 0, bitrate: audio.bitrate ?? 0, frames: queue?.frames ?? 0 });
              }
            }
          });
        } catch {
          fail("calling_media_setup_failed");
        }
      };
      current.on("stateChanged", changed);
      await changed();
    } catch {
      fail("calling_invitation_failed");
    }
  });
  const currentAgent = agent;
  if (currentAgent.connectionState !== "Connected") {
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => {
        currentAgent.off("connectionStateChanged", check);
        reject(new Error("ACS registration timed out."));
      }, 12000);
      function check() {
        if (currentAgent.connectionState === "Connected") {
          clearTimeout(timeout);
          currentAgent.off("connectionStateChanged", check);
          resolve();
        }
      }
      currentAgent.on("connectionStateChanged", check);
      check();
    });
  }
  report("registered");
}

declare global {
  interface Window {
    nativeVoip: { start: typeof start; stop: typeof stop };
  }
}
window.nativeVoip = { start, stop };
