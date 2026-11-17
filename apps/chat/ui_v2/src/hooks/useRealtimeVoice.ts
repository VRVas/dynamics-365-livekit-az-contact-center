'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { VOICE_BASE_URL } from '@/constants';
import { DEFAULT_LOCALE, type Locale } from '@/i18n/messages';

export type VoiceStatus = 'idle' | 'connecting' | 'listening' | 'error';

// --- PCM16 <-> Float32 / base64 helpers (24 kHz mono), ported from the voice console ---
function float32ToInt16(float32: Float32Array): Int16Array {
  const int16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return int16;
}

function int16ToBase64(int16: Int16Array): string {
  const bytes = new Uint8Array(int16.buffer);
  let binary = '';
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToFloat32(b64: string): Float32Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const int16 = new Int16Array(bytes.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    const v = int16[i];
    float32[i] = v < 0 ? v / 0x8000 : v / 0x7fff;
  }
  return float32;
}

/** PCM16 browser client for the server-side HUMAIN/LiveKit bridge. */
export function useRealtimeVoice() {
  const [status, setStatus] = useState<VoiceStatus>('idle');
  const [muted, setMuted] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const sourceNodeRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const mutedRef = useRef(false);
  const queueTimeRef = useRef(0);
  const playingRef = useRef<AudioBufferSourceNode[]>([]);
  const levelRef = useRef(0);
  const micLevelRef = useRef(0);
  const outAnalyserRef = useRef<AnalyserNode | null>(null);
  const rafRef = useRef<number | null>(null);
  const generationRef = useRef(0);
  const readyRef = useRef(false);
  const connectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const languageRef = useRef<Locale>(DEFAULT_LOCALE);
  const sentLanguageRef = useRef<Locale | null>(null);

  const stopAssistantAudio = useCallback(() => {
    playingRef.current.forEach((s) => {
      try {
        s.stop();
      } catch {
        /* already stopped */
      }
    });
    playingRef.current = [];
    if (audioCtxRef.current) queueTimeRef.current = audioCtxRef.current.currentTime;
    setSpeaking(false);
  }, []);

  const playAudio = useCallback((b64: string) => {
    const ctx = audioCtxRef.current;
    if (!ctx) return;
    const float32 = base64ToFloat32(b64);
    const buffer = ctx.createBuffer(1, float32.length, 24000);
    // `.set` on the channel's own Float32Array avoids copyToChannel's strict
    // Float32Array<ArrayBuffer> generic constraint introduced in TS 5.7 lib.dom.
    buffer.getChannelData(0).set(float32);
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(outAnalyserRef.current ?? ctx.destination);
    const startAt = Math.max(queueTimeRef.current, ctx.currentTime + 0.1);
    src.start(startAt);
    queueTimeRef.current = startAt + buffer.duration;
    playingRef.current.push(src);
    setSpeaking(true);
    src.onended = () => {
      playingRef.current = playingRef.current.filter((s) => s !== src);
      if (playingRef.current.length === 0) setSpeaking(false);
    };
  }, []);

  const stop = useCallback(() => {
    generationRef.current += 1;
    readyRef.current = false;
    sentLanguageRef.current = null;
    if (connectTimerRef.current) clearTimeout(connectTimerRef.current);
    connectTimerRef.current = null;
    if (processorRef.current) processorRef.current.onaudioprocess = null;
    processorRef.current?.disconnect();
    sourceNodeRef.current?.disconnect();
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    stopAssistantAudio();
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    if (wsRef.current) {
      wsRef.current.onopen = null;
      wsRef.current.onmessage = null;
      wsRef.current.onerror = null;
      wsRef.current.onclose = null;
      wsRef.current.close();
    }
    if (audioCtxRef.current && audioCtxRef.current.state !== 'closed') {
      void audioCtxRef.current.close().catch((error) => console.error('Audio cleanup failed', error));
    }
    wsRef.current = null;
    processorRef.current = null;
    sourceNodeRef.current = null;
    mediaStreamRef.current = null;
    audioCtxRef.current = null;
    outAnalyserRef.current = null;
    levelRef.current = 0;
    micLevelRef.current = 0;
    mutedRef.current = false;
    setMuted(false);
    setStatus('idle');
    setSpeaking(false);
    setWarning(null);
  }, [stopAssistantAudio]);

  const setLanguage = useCallback((language: Locale) => {
    languageRef.current = language;
    const ws = wsRef.current;
    if (readyRef.current && ws?.readyState === WebSocket.OPEN && language !== sentLanguageRef.current) {
      try {
        ws.send(JSON.stringify({ type: 'session.language', language }));
        sentLanguageRef.current = language;
      } catch (error) {
        console.error('Voice language update failed', error);
        setWarning('Could not update the voice language. Please ask the assistant to switch language or reconnect.');
      }
    }
  }, []);

  const start = useCallback(async (voice?: string, language: Locale = DEFAULT_LOCALE) => {
    if (audioCtxRef.current || wsRef.current) return;
    if (!VOICE_BASE_URL) {
      setError('Voice service is not configured.');
      setStatus('error');
      return;
    }
    languageRef.current = language;
    const generation = ++generationRef.current;
    setError(null);
    setWarning(null);
    setStatus('connecting');
    const fail = (message: string) => {
      if (generationRef.current !== generation) return;
      console.error('Voice session failed:', message);
      stop();
      setError(message);
      setStatus('error');
    };
    try {
      const ctx = new AudioContext({ sampleRate: 24000 });
      audioCtxRef.current = ctx;
      await ctx.resume();
      if (generationRef.current !== generation) return;
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (generationRef.current !== generation) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      mediaStreamRef.current = stream;
      queueTimeRef.current = ctx.currentTime;

      // Audio-reactive level: blend mic energy (listening) with assistant output energy (speaking).
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.8;
      analyser.connect(ctx.destination);
      outAnalyserRef.current = analyser;
      const outData = new Uint8Array(analyser.frequencyBinCount);
      const tick = () => {
        analyser.getByteTimeDomainData(outData);
        let sum = 0;
        for (let i = 0; i < outData.length; i++) {
          const v = (outData[i] - 128) / 128;
          sum += v * v;
        }
        const outLevel = Math.sqrt(sum / outData.length) * 1.6;
        const target = Math.min(1, Math.max(micLevelRef.current, outLevel));
        levelRef.current = levelRef.current * 0.78 + target * 0.22;
        rafRef.current = requestAnimationFrame(tick);
      };
      rafRef.current = requestAnimationFrame(tick);

      const wsUrl = VOICE_BASE_URL.replace(/^http/, 'ws') + '/realtime';
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      connectTimerRef.current = setTimeout(
        () => fail('HUMAIN did not become ready in time. Please try again.'),
        120000,
      );

      ws.onopen = () => {
        sentLanguageRef.current = languageRef.current;
        ws.send(
          JSON.stringify({
            type: 'session.update',
            session: { ...(voice ? { voice } : {}), language: languageRef.current },
          }),
        );
      };

      const sourceNode = ctx.createMediaStreamSource(stream);
      sourceNodeRef.current = sourceNode;
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      processorRef.current = processor;
      sourceNode.connect(processor);
      processor.connect(ctx.destination);
      processor.onaudioprocess = (event) => {
        if (!readyRef.current || ws.readyState !== WebSocket.OPEN) return;
        const input = event.inputBuffer.getChannelData(0);
        let sum = 0;
        for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
        micLevelRef.current = mutedRef.current ? 0 : Math.min(1, Math.sqrt(sum / input.length) * 3.2);
        // Keep the media clock running while muted so the agent can finish the user's turn.
        const pcm = mutedRef.current ? new Int16Array(input.length) : float32ToInt16(input);
        ws.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: int16ToBase64(pcm) }));
      };

      ws.onmessage = (event) => {
        let msg: {
          type?: string;
          delta?: string;
          error?: { message?: string };
          warning?: { message?: string } | null;
        };
        try {
          msg = JSON.parse(event.data);
        } catch {
          fail('Invalid message from the voice backend.');
          return;
        }
        if (!msg || typeof msg !== 'object') {
          fail('Invalid message from the voice backend.');
          return;
        }
        switch (msg.type) {
          case 'session.updated':
            readyRef.current = true;
            if (connectTimerRef.current) clearTimeout(connectTimerRef.current);
            connectTimerRef.current = null;
            setStatus('listening');
            setLanguage(languageRef.current);
            break;
          case 'session.language_requested':
            setWarning((current) => current === 'Could not update the voice language. Please ask the assistant to switch language or reconnect.' ? null : current);
            break;
          case 'session.warning':
            setWarning(msg.warning?.message || null);
            break;
          case 'response.output_audio.delta':
            if (msg.delta) playAudio(msg.delta);
            break;
          case 'input_audio_buffer.speech_started':
            stopAssistantAudio();
            break;
          case 'error':
            fail(msg.error?.message || 'Voice backend error.');
            break;
        }
      };

      ws.onerror = () => fail(`Could not reach the voice backend at ${VOICE_BASE_URL}.`);
      ws.onclose = () => {
        fail('The voice session disconnected. Please start a new session.');
      };
    } catch (e) {
      fail(e instanceof Error ? e.message : 'Audio init failed.');
    }
  }, [playAudio, stop, stopAssistantAudio, setLanguage]);

  const toggleMute = useCallback(() => {
    setMuted((m) => {
      mutedRef.current = !m;
      return !m;
    });
  }, []);

  // Stop everything when the Voice view unmounts (e.g. switching back to Chat).
  useEffect(() => () => stop(), [stop]);

  return { status, muted, speaking, error, warning, levelRef, start, stop, toggleMute, setLanguage };
}
