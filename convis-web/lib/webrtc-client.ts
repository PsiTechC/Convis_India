/**
 * Browser voice-call client backed by LiveKit Cloud.
 *
 * Flow:
 *   1. POST /api/livekit/token → { livekit_url, token, room_name, identity }
 *   2. Room.connect(livekit_url, token) with mic track published
 *   3. Agent participant (dispatched server-side) produces the assistant's audio
 *      track; we auto-subscribe and play it.
 *
 * All ASR/LLM/TTS happens in the LiveKit agent worker, not in this client.
 * Barge-in, turn-taking, and VAD are handled by AgentSession in the worker —
 * the browser just needs to publish mic audio and play back received audio.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ConnectionState,
  LocalAudioTrack,
  Participant,
  RemoteAudioTrack,
  RemoteParticipant,
  RemoteTrack,
  RemoteTrackPublication,
  Room,
  RoomEvent,
  Track,
  TranscriptionSegment,
  createLocalAudioTrack,
} from "livekit-client";

export type CallState =
  | "idle"
  | "connecting"
  | "connected"
  | "listening"
  | "ai-speaking"
  | "disconnected"
  | "error";

export interface Transcript {
  speaker: "user" | "assistant";
  text: string;
  isFinal: boolean;
  timestamp: number;
}

export interface WebRTCClientConfig {
  apiBaseUrl: string;
  assistantId: string;
  /** Bearer token for /api/livekit/token. Required — endpoint is auth-gated. */
  authToken: string;
  onTranscript?: (text: string, isFinal: boolean, speaker: string) => void;
  onStateChange?: (state: CallState) => void;
  onError?: (error: Error) => void;
  onAudioStart?: () => void;
  onAudioEnd?: () => void;
}

interface TokenResponse {
  livekit_url: string;
  token: string;
  room_name: string;
  identity: string;
}

export class ConvisWebRTCClient {
  private config: WebRTCClientConfig;
  private room: Room | null = null;
  private localTrack: LocalAudioTrack | null = null;
  private audioElement: HTMLAudioElement | null = null;
  private state: CallState = "idle";
  private remoteAudioTrack: RemoteAudioTrack | null = null;

  constructor(config: WebRTCClientConfig) {
    this.config = config;
  }

  getState(): CallState {
    return this.state;
  }

  async start(): Promise<void> {
    if (this.state !== "idle" && this.state !== "disconnected" && this.state !== "error") {
      throw new Error(`Cannot start call in state: ${this.state}`);
    }
    this.setState("connecting");

    try {
      const tokenRes = await this.fetchToken();
      this.localTrack = await createLocalAudioTrack({
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      });

      const room = new Room({ adaptiveStream: true, dynacast: true });
      this.room = room;
      this.wireRoomEvents(room);

      await room.connect(tokenRes.livekit_url, tokenRes.token);
      await room.localParticipant.publishTrack(this.localTrack);

      this.setState("connected");
    } catch (err) {
      this.setState("error");
      this.config.onError?.(err as Error);
      await this.cleanup();
      throw err;
    }
  }

  async stop(): Promise<void> {
    await this.cleanup();
    this.setState("disconnected");
  }

  setMuted(muted: boolean): void {
    if (!this.localTrack) return;
    if (muted) void this.localTrack.mute();
    else void this.localTrack.unmute();
  }

  setVolume(volume: number): void {
    if (this.audioElement) {
      this.audioElement.volume = Math.max(0, Math.min(1, volume));
    }
  }

  // ── Internal ─────────────────────────────────────────────────────────────

  private async fetchToken(): Promise<TokenResponse> {
    const res = await fetch(`${this.config.apiBaseUrl}/api/livekit/token`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.config.authToken}`,
      },
      body: JSON.stringify({
        assistant_id: this.config.assistantId,
      }),
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`Token request failed (${res.status}): ${detail}`);
    }
    return res.json();
  }

  private setState(next: CallState): void {
    this.state = next;
    this.config.onStateChange?.(next);
  }

  private wireRoomEvents(room: Room): void {
    room.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
      if (track.kind === Track.Kind.Audio) {
        this.attachRemoteAudio(track as RemoteAudioTrack, participant);
      }
    });

    room.on(
      RoomEvent.TrackUnsubscribed,
      (_track, _publication: RemoteTrackPublication, _participant: RemoteParticipant) => {
        if (this.remoteAudioTrack === _track) {
          this.remoteAudioTrack = null;
        }
      },
    );

    room.on(RoomEvent.ActiveSpeakersChanged, (speakers: Participant[]) => {
      const agentSpeaking = speakers.some((s) => s.identity !== room.localParticipant.identity);
      if (agentSpeaking && this.state !== "ai-speaking") {
        this.config.onAudioStart?.();
        this.setState("ai-speaking");
      } else if (!agentSpeaking && this.state === "ai-speaking") {
        this.config.onAudioEnd?.();
        this.setState("listening");
      }
    });

    room.on(RoomEvent.TranscriptionReceived, (segments: TranscriptionSegment[], participant) => {
      const speaker =
        participant && participant.identity === room.localParticipant.identity ? "user" : "assistant";
      for (const seg of segments) {
        this.config.onTranscript?.(seg.text, seg.final, speaker);
      }
    });

    room.on(RoomEvent.Disconnected, () => {
      if (this.state !== "disconnected") {
        this.setState("disconnected");
      }
    });

    room.on(RoomEvent.ConnectionStateChanged, (s: ConnectionState) => {
      if (s === ConnectionState.Connected && this.state === "connecting") {
        this.setState("listening");
      }
    });
  }

  private attachRemoteAudio(track: RemoteAudioTrack, _participant: Participant): void {
    this.remoteAudioTrack = track;
    if (!this.audioElement) {
      this.audioElement = document.createElement("audio");
      this.audioElement.autoplay = true;
      this.audioElement.style.display = "none";
      document.body.appendChild(this.audioElement);
    }
    track.attach(this.audioElement);
  }

  private async cleanup(): Promise<void> {
    try {
      if (this.remoteAudioTrack && this.audioElement) {
        this.remoteAudioTrack.detach(this.audioElement);
      }
      if (this.audioElement) {
        this.audioElement.remove();
        this.audioElement = null;
      }
      if (this.localTrack) {
        this.localTrack.stop();
        this.localTrack = null;
      }
      if (this.room) {
        await this.room.disconnect();
        this.room = null;
      }
    } catch (err) {
      console.error("[LiveKit] Cleanup error:", err);
    }
  }
}

// ─── React Hook (keeps the same public surface as before) ───────────────────

export interface UseWebRTCCallReturn {
  state: CallState;
  transcripts: Transcript[];
  duration: number;
  isMuted: boolean;
  volume: number;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  toggleMute: () => void;
  setVolume: (v: number) => void;
}

export function useWebRTCCall(config: {
  apiBaseUrl: string;
  assistantId: string;
  authToken: string;
}): UseWebRTCCallReturn {
  const [state, setState] = useState<CallState>("idle");
  const [transcripts, setTranscripts] = useState<Transcript[]>([]);
  const [duration, setDuration] = useState(0);
  const [isMuted, setIsMuted] = useState(false);
  const [volume, setVolumeState] = useState(1);

  const clientRef = useRef<ConvisWebRTCClient | null>(null);
  const durationTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const connectedAtRef = useRef<number | null>(null);
  const configRef = useRef(config);
  configRef.current = config;

  useEffect(() => {
    if (state === "connected" || state === "listening" || state === "ai-speaking") {
      if (!connectedAtRef.current) connectedAtRef.current = Date.now();
      durationTimerRef.current = setInterval(() => {
        if (connectedAtRef.current) {
          setDuration(Math.floor((Date.now() - connectedAtRef.current) / 1000));
        }
      }, 1000);
    } else {
      if (durationTimerRef.current) {
        clearInterval(durationTimerRef.current);
        durationTimerRef.current = null;
      }
      if (state === "idle" || state === "disconnected") connectedAtRef.current = null;
    }
    return () => {
      if (durationTimerRef.current) clearInterval(durationTimerRef.current);
    };
  }, [state]);

  useEffect(() => {
    return () => {
      clientRef.current?.stop();
    };
  }, []);

  const start = useCallback(async () => {
    if (clientRef.current) {
      await clientRef.current.stop();
      clientRef.current = null;
    }
    setTranscripts([]);
    setDuration(0);
    connectedAtRef.current = null;

    const client = new ConvisWebRTCClient({
      ...configRef.current,
      onStateChange: setState,
      onTranscript: (text, isFinal, speaker) => {
        setTranscripts((prev) => {
          if (!isFinal) {
            const lastIdx = prev.length - 1;
            if (lastIdx >= 0 && !prev[lastIdx].isFinal && prev[lastIdx].speaker === speaker) {
              const updated = [...prev];
              updated[lastIdx] = {
                speaker: speaker as "user" | "assistant",
                text,
                isFinal,
                timestamp: Date.now(),
              };
              return updated;
            }
          }
          return [
            ...prev,
            { speaker: speaker as "user" | "assistant", text, isFinal, timestamp: Date.now() },
          ];
        });
      },
      onError: (err) => {
        // "Client initiated disconnect" is NOT an error — it's how the
        // LiveKit client reports that WE called room.disconnect() (which
        // happens on every normal stop() / unmount). React 18 StrictMode
        // in dev fires the BrowserCallModal's useEffect cleanup once on
        // the discarded first mount, which triggers this. Demote it to
        // a debug log so the console doesn't show a scary red error on
        // every page open.
        const msg = err?.message || "";
        if (msg === "Client initiated disconnect") {
          console.debug("[LiveKit Hook] client-initiated disconnect (expected)");
          return;
        }
        console.error("[LiveKit Hook] Error:", msg);
      },
    });

    clientRef.current = client;
    await client.start();
  }, []);

  const stop = useCallback(async () => {
    if (clientRef.current) {
      await clientRef.current.stop();
      clientRef.current = null;
    }
  }, []);

  const toggleMute = useCallback(() => {
    setIsMuted((prev) => {
      const next = !prev;
      clientRef.current?.setMuted(next);
      return next;
    });
  }, []);

  const setVolume = useCallback((v: number) => {
    const clamped = Math.max(0, Math.min(1, v));
    setVolumeState(clamped);
    clientRef.current?.setVolume(clamped);
  }, []);

  return { state, transcripts, duration, isMuted, volume, start, stop, toggleMute, setVolume };
}

export default ConvisWebRTCClient;
