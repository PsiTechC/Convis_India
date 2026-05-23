# LiveKit + Twilio SIP setup

Convis uses LiveKit Cloud as the media plane for all voice calls. Twilio still
owns phone numbers and carries PSTN traffic, but audio flows through LiveKit
instead of Twilio Media Streams.

```
Browser ──WebRTC──► LiveKit room ──► LiveKit Agent (Deepgram + OpenAI + ElevenLabs)
                         ▲
Twilio PSTN ──SIP──► LiveKit SIP ─┘
```

The backend (`convis-api`) never touches audio. It:
1. Mints LiveKit tokens for browser clients
2. Returns TwiML that hands inbound Twilio calls off to LiveKit SIP
3. Calls the LiveKit API to dispatch outbound SIP participants for outgoing calls

The agent process (`livekit-agent` docker service) joins each room and runs the
voice pipeline.

---

## 1. LiveKit Cloud

1. Create a LiveKit Cloud project → copy the project URL, API Key, API Secret.
2. Fill in `.env`:
   ```
   LIVEKIT_URL=wss://<project>.livekit.cloud
   LIVEKIT_API_KEY=APIxxxx
   LIVEKIT_API_SECRET=xxxx
   LIVEKIT_AGENT_NAME=convis-agent
   ```
3. In the LiveKit console → **Telephony** page, note the inbound SIP host
   (e.g. `<project>-sip.livekit.cloud`). Put that in `LIVEKIT_SIP_INBOUND_HOST`.

## 2. Twilio Elastic SIP Trunking

Twilio programmable-voice numbers work, but SIP trunking gives lower latency
and is the recommended transport.

### Termination (outbound: Convis → phone)
1. Twilio console → **Elastic SIP Trunking → Trunks → Create**.
2. Termination URI: set to the outbound URI LiveKit provides for your project
   (from the LiveKit SIP page, e.g. `<project>-sip.livekit.cloud`).
3. Assign Caller ID / phone numbers to this trunk (the numbers your assistants
   dial from).
4. In LiveKit → **Telephony → Outbound Trunks → New**:
   - Protocol: SIP
   - Address: `<your-twilio-trunk>.pstn.twilio.com`
   - Auth: username/password from your Twilio trunk
   - Numbers: the E.164 numbers you registered with Twilio
5. Save the returned trunk id as `LIVEKIT_SIP_OUTBOUND_TRUNK_ID=ST_...`.

### Origination (inbound: phone → Convis)

Pick one of:

**Option A — keep Twilio's HTTP webhook (simpler).** Your Twilio number Voice
URL stays pointed at:
```
https://api.convis.ai/api/inbound-calls/connect/<assistant_id>
```
That endpoint creates a LiveKit room, dispatches the agent, and returns TwiML
that `<Dial><Sip>` hands the call off to `sip:<room>@${LIVEKIT_SIP_INBOUND_HOST}`.
Nothing else to configure.

**Option B — pure SIP origination (no TwiML hop).** Point the Twilio trunk's
Origination URI at LiveKit SIP and configure a LiveKit dispatch rule that
routes inbound calls to the agent. See the LiveKit docs "SIP inbound trunks"
and "Dispatch rules".

## 3. Deployment

```
cp convis-api/.env.production.example convis-api/.env
# fill in the LIVEKIT_* values above, plus the existing DEEPGRAM / OPENAI /
# ELEVENLABS / TWILIO keys.

docker compose up -d --build
```

The `livekit-agent` container registers with LiveKit Cloud as
`${LIVEKIT_AGENT_NAME}`. The FastAPI service dispatches rooms under that agent
name, so the worker is automatically assigned to each new call.

## 4. Smoke test

1. **Browser call**
   `POST /api/livekit/token { "assistant_id": "..." }` → returns a token the
   `BrowserCallModal` component uses. Open the assistant in the web UI and
   start a browser call.
2. **Inbound phone**
   Dial a Twilio number assigned to an assistant. Twilio hits
   `/api/inbound-calls/connect/<assistant_id>` → call lands in a LiveKit room.
3. **Outbound phone**
   `POST /api/outbound-calls/make-call/<assistant_id> { "phone_number": "+1..." }`
   → LiveKit places the call via your Twilio trunk.

Logs to watch:
- `convis-api` — room dispatch, TwiML generation
- `convis-livekit-agent` — agent session start, STT/LLM/TTS events
- Twilio debugger — inbound TwiML / outbound SIP traces

## 5. What changed

Removed:
- Custom WebSocket "WebRTC" signaling (`app/services/webrtc/`,
  `app/routes/webrtc/`)
- Twilio `<Stream>` Media Streams handlers (`call_handlers/optimized_stream_handler.py`,
  `ultra_low_latency_handler.py`, the streaming ASR/LLM/TTS modules, and the
  offline Whisper/Piper paths)
- The old `app/routes/outbound_calls/media-stream/{assistant_id}` WebSocket

Added:
- `app/services/livekit/` — assistant config loader, token minting, SIP dispatch,
  agent worker entrypoint
- `app/routes/livekit/` — `POST /api/livekit/token` for browser clients
- `livekit-agent` service in `docker-compose.yml`
