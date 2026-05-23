# Convis vs Vapi — Comparison & Catch-Up Checklist

**Status:** Draft, written 2026-05-07 (v2 — expanded). Reflects post-audit state of Convis (after the security fixes shipped today) vs publicly-documented Vapi capabilities as of the model's knowledge cutoff (Jan 2026). **Verify any specific Vapi feature claim against vapi.ai/docs before depending on it for product strategy** — they ship fast.

---

## TL;DR

Convis has the same **core architecture** as Vapi — a streaming voice pipeline (STT → LLM → TTS) bolted onto telephony. The gap isn't architectural; it's:

1. **Multi-tenant primitives** (Vapi has clean per-tenant isolation; Convis has just-fixed but still-fragile single-Twilio-account assumptions)
2. **Surface area** (Vapi has SDKs for browser/mobile/React Native + CLI + REST API + GraphQL-ish; Convis is dashboard-only)
3. **Polish & ops** (Vapi has CI, observability, docs portal, status page, public changelog; Convis has none of these)
4. **Compliance posture** (Vapi has SOC 2 Type II + HIPAA BAA; Convis has neither)
5. **Feature breadth** (Vapi has squads, voice cloning, A/B testing, visual workflow builder, conversation analysis rubrics, voicemail detection, background audio, transient assistants, server URL hooks; Convis has the core flow only)
6. **Telephony depth** (Vapi has DTMF, IVR menus, warm/cold transfer, conference, recording controls, time-based routing; Convis has straight inbound + outbound)
7. **Ecosystem** (Vapi has 30+ pre-built integrations; Convis has 5–6 custom-built ones)

**Convis is at MVP-with-real-customers level. Vapi is at platform-scale level. The gap is real but bridgeable in 6–9 months with focused work.**

Legend used throughout: ✅ present and solid · 🟡 partial / works but rough · ❌ missing · 🔒 security/compliance gap

Effort tags on tasks: `[XS]` <1 day · `[S]` 1–3 days · `[M]` 1–2 weeks · `[L]` 3–6 weeks · `[XL]` months

---

## Section 1 — Current architectural state of Convis

For grounding (so the catch-up plan makes sense):

- **API**: FastAPI + pydantic v2 + pymongo, deployed to AWS App Runner. ~36 routes across 11 areas (auth, assistants, voices, dashboard, phone-numbers, inbound-calls, outbound-calls, calendar, whatsapp, integrations, campaigns).
- **Web**: Next.js 15.5.4 + React + tailwindcss + livekit-client + reactflow. App Runner. **Zero tests.**
- **Agent worker**: separate ECS Fargate task running livekit-agents framework. Holds the Deepgram + OpenAI + ElevenLabs streaming pipeline; uses Silero VAD; OpenAI prompt cache via warmer running every 4 min.
- **Telephony**: Twilio Programmable Voice + LiveKit SIP. Single Twilio account currently shared across all users (tenant-isolation fix shipped today; multi-Twilio-account architecture still TODO).
- **Data**: MongoDB Atlas/VPS hybrid (needs reconciliation). Collections: `users`, `assistants`, `phone_numbers`, `call_logs`, `provider_connections`, `knowledge_chunks`, `call_attempts`, `verified_caller_ids`, `notifications`, `campaigns`, integrations docs.
- **Compliance**: AWS BAA-able but BAAs not signed yet. PII (caller phone numbers, transcripts) flows through CloudWatch unredacted. No SOC 2.

---

## Section 2 — Side-by-side feature comparison (deep)

### 2.1 — Voice / audio pipeline

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Streaming STT — Deepgram | ✅ nova-2/3 | ✅ nova-2-phonecall, nova-3 multi | At parity |
| Streaming STT — Whisper / GPT-4o-Transcribe | ✅ | ❌ | Add as fallback |
| Streaming STT — Talkscriber, Gladia, AssemblyAI | ✅ multiple | ❌ | Provider abstraction needed |
| STT language auto-detect | ✅ | 🟡 only via multilingual mode | Polish |
| STT custom vocabulary / boost words | ✅ | ❌ | High-impact for branded terms |
| STT keyword filler removal | ✅ | ❌ | Polish |
| Streaming LLM — OpenAI | ✅ all SOTA | ✅ gpt-4o-mini | At parity |
| Streaming LLM — Anthropic | ✅ Claude 3.5/4 | ❌ | Add for redundancy + customer demand |
| Streaming LLM — Groq (low latency) | ✅ | ❌ | Add for sub-500ms |
| Streaming LLM — Together / Fireworks (open models) | ✅ | ❌ | Add when needed |
| Streaming LLM — BYO (custom OpenAI-compatible endpoint) | ✅ | ❌ | Enterprise blocker |
| Streaming LLM — per-call hot-swap | ✅ | ❌ Convis is per-assistant only | Add `model` override per call |
| Streaming TTS — ElevenLabs | ✅ flash + multilingual + v3 | ✅ flash_v2_5 | At parity |
| Streaming TTS — Cartesia Sonic | ✅ | ✅ | At parity |
| Streaming TTS — PlayHT | ✅ | ❌ | Add for cost diversity |
| Streaming TTS — OpenAI TTS / Deepgram Aura | ✅ | ❌ | Add cheap option |
| Streaming TTS — Azure | ✅ enterprise | ❌ | Enterprise blocker |
| Voice cloning (instant) | ✅ via providers | 🟡 voice_id settable, no clone UI | Build clone-from-sample wizard |
| Voice library marketplace | ✅ | ❌ | Curate 50+ voices in dashboard |
| Voice settings (stability, similarity, style, speaker boost) | ✅ exposed | ✅ partial in assistants doc | Expose in UI |
| Speed / rate / pitch | ✅ | ✅ tts_speed | At parity |
| Emotion / expressive | ✅ | ✅ expressive_mode flag | At parity |
| Voice activity detection (Silero) | ✅ tunable | ✅ pre-warmed | At parity |
| End-of-utterance smart detection | ✅ smart EOU + interruption rules | ✅ min_endpointing_delay + min_interruption_duration | At parity |
| Background audio / room noise | ✅ play during silence | ❌ | Add as feature |
| Filler words ("um, let me check…") | ✅ | ❌ | Add as feature |
| Disfluency tolerance | ✅ tunable | 🟡 implicit via VAD | Document |
| Crosstalk handling | ✅ | 🟡 barge-in tuned | At parity |
| Audio prompts (insert pre-recorded clips) | ✅ | ❌ | Useful for legal disclaimers |
| Sub-second voice-to-voice latency | ✅ ~700ms target | ✅ ~800ms measured | At parity |
| Telephony codec support (opus, μ-law, PCM) | ✅ | ✅ via LiveKit/Twilio | At parity |
| 8kHz vs 16kHz handling | ✅ auto | ✅ Deepgram nova-2-phonecall on 8kHz | At parity |
| Audio-only debug recording (no LLM) | ✅ | ❌ | Add for ASR tuning |

### 2.2 — Telephony / phone features

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Inbound calls | ✅ | ✅ shipped today | At parity |
| Outbound calls | ✅ | ✅ | At parity |
| Browser → phone (web SDK) | ✅ | ❌ | Major gap |
| Phone → browser (web SDK) | ✅ | ❌ | Major gap |
| Twilio integration | ✅ BYO + Vapi-managed | 🟡 BYO single-account | Multi-tenant fix needed |
| Vonage / Plivo / Telnyx | ✅ | ❌ | Add when customer asks |
| LiveKit SIP | ✅ | ✅ primary | At parity |
| Vobiz (Indian carrier) | ❌ | ✅ | **Convis ahead** |
| Number purchasing (in product) | ✅ marketplace UI | 🟡 basic Twilio purchase route | Polish, expose UI |
| Number portability | ✅ | ❌ | Add when needed |
| Vanity numbers / area-code search | ✅ | 🟡 Twilio passthrough | Wire UI |
| Toll-free numbers | ✅ | 🟡 Twilio passthrough | Wire UI |
| 10DLC SMS registration | ✅ | ❌ | Need for SMS |
| International numbers | ✅ | 🟡 Twilio passthrough | Document |
| E.164 validation | ✅ | ✅ regex | At parity |
| DTMF detection (keypad) | ✅ during conversation | ❌ | Add for IVR-like flows |
| DTMF input (collect digits — phone, account #, OTP) | ✅ block | ❌ | Add as tool |
| DTMF output (play tones) | ✅ | ❌ | Rare but useful |
| IVR menus ("press 1 for sales") | ✅ via blocks | ❌ | Out of scope or build |
| Time-of-day / business-hours routing | ✅ | ❌ | Add as assistant rule |
| Failover routing (assistant down → backup) | ✅ | ❌ | Add for reliability |
| Caller ID (set From on outbound) | ✅ | ✅ caller_id param | At parity |
| Caller ID name (CNAM) | ✅ Twilio passthrough | 🟡 | Expose |
| Spam-likely flagging mitigation | ✅ verified caller IDs | 🟡 verified_caller_ids collection exists | Wire UI |
| Concurrent call limits per number | ✅ | ❌ | Add as guardrail |
| Concurrent call limits per account | ✅ | ❌ | Add as guardrail |
| Per-number rate limit | ✅ | ❌ | Add |
| Hold music / on-hold experience | ✅ | ❌ | Add |
| Warm transfer (introduce + hand off) | ✅ | ❌ | High customer demand |
| Cold transfer (raw transfer to number) | ✅ | ❌ | High customer demand |
| Conference (3-way, agent + 2 people) | ✅ | ❌ | Add |
| Whisper / supervisor mode | ✅ | ❌ | Add |
| Live monitoring (manager listens in) | ✅ | ❌ | Add via LiveKit |
| Recording controls (start/stop mid-call) | ✅ | ❌ always-on | Add for compliance regions |
| Dual-channel recording | ✅ | ✅ shipped today | At parity |
| Recording encryption at rest | ✅ KMS | 🟡 Twilio storage default | Customer KMS |
| Recording retention controls (per tenant/per call) | ✅ | ❌ | Compliance need |
| Voicemail detection (AMD) | ✅ Twilio AMD or heuristic | ❌ | Add as setting |
| Voicemail drop (leave pre-recorded message) | ✅ | ❌ | Add |
| Pre-call announcement / disclosure | ✅ for compliance | ❌ | Add for TCPA |
| Post-call SMS / survey | ✅ | ❌ | Add via WhatsApp routes |
| DNC (Do Not Call) list integration | ✅ | ❌ | TCPA compliance |
| Outbound call rate caps (TCPA) | ✅ | 🟡 campaign_dialer pacing | Polish |
| Call disposition tagging | ✅ | ❌ | Add UI |
| Click-to-call | ✅ via web SDK | ❌ | Tied to web SDK |
| SIP URI dial (call internal extensions) | ✅ | ✅ | At parity |
| WebRTC dial-in/out | ✅ | 🟡 LiveKit native | At parity for power users |
| MOS / jitter / packet loss reporting | ✅ | 🟡 call_quality fields exist | Wire dashboard |
| Number reverse lookup / validation | ✅ Twilio Lookup | ❌ | Add |

### 2.3 — Voice agent capabilities

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Persona / character | ✅ system prompt | ✅ | At parity |
| Variable injection ({{customer_name}}) | ✅ | 🟡 unclear if templated | Verify and polish |
| Greeting (pre-LLM, fast TTS) | ✅ first message | ✅ shipped (`session.say`) | At parity |
| Multilingual switching mid-call | ✅ | ✅ verified | At parity |
| Code-switching detection (Hinglish) | ✅ | ✅ multilingual prompt | **Convis ahead in India** |
| NLU intents (custom intent definitions) | ✅ | ❌ implicit via LLM | Add for structured outcomes |
| Entity extraction (slot filling) | ✅ named slots | ❌ | Add |
| Form-filling (collect N fields with confirmation) | ✅ | ❌ | Major missing |
| Numeric input (account #, phone, date) | ✅ tunable | 🟡 LLM-based, brittle | Add tool-based collector |
| Spell-it-out input ("J-A-N-E") | ✅ | ❌ | Polish |
| Date/time relative parsing ("next Tuesday at 3") | ✅ | 🟡 calendar service does some | Polish |
| Address input (multi-line) | ✅ | ❌ | Add |
| Confirmation patterns (read-back) | ✅ | 🟡 prompt-driven | Standardize |
| Conversation memory (long-term) | ✅ across calls | ❌ | Add |
| Per-conversation context | ✅ | ✅ in-session | At parity |
| Conversation summary (post-call) | ✅ | 🟡 post-call processor | Wire UI |
| Action item extraction | ✅ | ❌ | Add |
| Sentiment per-turn + overall | ✅ | 🟡 fields exist | Wire dashboard |
| End conditions (stop talking after 30s silence) | ✅ tunable | ✅ idle_watchdog | At parity |
| Goodbye / farewell detection | ✅ | ✅ end_call function tool | At parity |
| Personality presets ("friendly", "professional") | ✅ persona library | ❌ | Add curated presets |
| Per-assistant LLM/TTS overrides | ✅ | ✅ in Mongo doc | At parity |
| Per-call LLM/TTS overrides (transient) | ✅ | ❌ | Add |

### 2.4 — Tool / function calling

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Define tools via JSON schema | ✅ in dashboard | 🟡 only `end_call` and KB hardcoded | Add config-driven framework |
| HTTP tools (REST) | ✅ POST/GET/etc. | ❌ | Major missing |
| GraphQL tools | ✅ | ❌ | Lower priority |
| MCP server integration | ✅ | ❌ | Coming up in industry |
| Tool result streaming | ✅ | n/a | Add when tools framework lands |
| Tool authentication (per-tool credentials) | ✅ | n/a | Add when tools framework lands |
| Tool retries / timeout | ✅ | n/a | Add |
| Async tools (long-running) | ✅ | ❌ | Add |
| Tool composition (chain) | ✅ | ❌ | Add |
| Conditional tool invocation | ✅ via blocks | ❌ | Add |
| Tool sandboxing (URL allowlist) | ✅ | 🔒 SSRF risk if added without sandbox | **Block before shipping** |
| Built-in tools: Google Calendar | ✅ | ✅ | At parity |
| Built-in tools: HubSpot, Salesforce | ✅ | 🟡 HubSpot route | Polish |
| Built-in tools: Zapier triggers | ✅ | ❌ | Add |
| Built-in tools: knowledge retrieval | ✅ | ✅ | At parity |
| Built-in tools: end_call, transfer | ✅ | 🟡 end_call only | Add transfer |

### 2.5 — Knowledge base / RAG

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| File ingestion: PDF | ✅ | ✅ | At parity |
| File ingestion: DOCX | ✅ | ✅ | At parity |
| File ingestion: MD / TXT | ✅ | ✅ | At parity |
| File ingestion: HTML / URL | ✅ scrape | ❌ | Add |
| Notion / Drive / Confluence connectors | ✅ | ❌ | Add when needed |
| Auto-chunking (semantic, fixed-size) | ✅ | 🟡 fixed-size only | Add semantic |
| Custom chunk size / overlap | ✅ | ❌ | Expose |
| Embedding model selection | ✅ | 🟡 text-embedding-3-small fixed | Expose |
| Hybrid search (BM25 + vector) | ✅ | ❌ vector only | Add for keyword-heavy queries |
| Re-ranking | ✅ | ❌ | Add |
| Query rewriting (HyDE) | ✅ | ❌ | Add |
| Multi-document RAG | ✅ | ✅ | At parity |
| Citation surfacing in transcript | ✅ | ❌ | Add UI |
| Auto-FAQ generation (LLM extracts Q&A) | ✅ | ❌ | High-impact feature |
| KB versioning | ✅ | ❌ | Add |
| KB freshness / re-index | ✅ scheduled | 🟡 manual | Add cron |
| KB analytics (most-queried, miss rate) | ✅ | ❌ | Add dashboard |
| Conversation as KB (past calls index) | ✅ | ❌ | Add |
| Per-tenant chunk isolation | ✅ | 🟡 user_id scoping (untested) | **Audit + tests** |
| Encrypted KB storage | ✅ KMS | ❌ | Add |

### 2.6 — Workflow / orchestration

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Visual workflow builder | ✅ block-based | ❌ | Reactflow already in deps |
| Block: greeting | ✅ | n/a | Add when builder lands |
| Block: question (collect) | ✅ | n/a | |
| Block: condition (branch) | ✅ | n/a | |
| Block: tool (HTTP/integration) | ✅ | n/a | |
| Block: transfer | ✅ | n/a | |
| Block: end | ✅ | n/a | |
| Block: voicemail | ✅ | n/a | |
| Variables / context across blocks | ✅ | n/a | |
| Sub-workflows (composability) | ✅ | n/a | |
| Workflow versioning | ✅ | ❌ | Add |
| A/B testing (workflow variants) | ✅ | ❌ | Add |
| Sandbox / preview mode | ✅ | ❌ | Add |
| Conversation simulator (text-mode) | ✅ | ❌ | Add for testing |

### 2.7 — Multi-agent / squads

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Squad (multi-agent collection) | ✅ | ❌ | Major missing |
| Hand-off mid-call | ✅ context-preserving | ❌ | Major missing |
| Hand-off criteria (LLM-decided or rule-based) | ✅ | ❌ | Add |
| Shared context across squad | ✅ | ❌ | Add |
| Dynamic squad selection (by intent) | ✅ | ❌ | Add |
| Hierarchical agents (greeter → specialist → senior) | ✅ | ❌ | Add |

### 2.8 — Calendar / scheduling

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Google Calendar | 🟡 | ✅ wired | **Convis ahead** |
| Outlook / Microsoft 365 | 🟡 | ❌ | Add |
| Calendly | 🟡 | ❌ | Add |
| Cal.com | 🟡 | ❌ | Add |
| Multi-calendar booking (find slots across) | ✅ | ❌ | Add |
| Timezone-aware booking | ✅ | ✅ | At parity |
| Buffer time / slot duration config | ✅ | ✅ | At parity |
| Availability rules per day | ✅ | ✅ | At parity |
| Send confirmation email + SMS | ✅ | ✅ + WhatsApp | **Convis ahead** |
| Reschedule / cancel via call | ✅ | 🟡 rescheduling exists | Polish |
| Reminder calls | ✅ | ❌ | Add via campaigns |

### 2.9 — Integrations / ecosystem

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| **CRM** |  |  |  |
| Salesforce | ✅ | ❌ | Add |
| HubSpot | ✅ | ✅ route exists | At parity |
| Pipedrive | ✅ | ❌ | Add |
| Zoho CRM | ✅ | ❌ | Add |
| Close.io | ✅ | ❌ | Add |
| **Help desk** |  |  |  |
| Zendesk | ✅ | ❌ | Add |
| Intercom | ✅ | ❌ | Add |
| Freshdesk | ✅ | ❌ | Add |
| Help Scout | ✅ | ❌ | Add |
| **Calendar** |  |  |  |
| Google Calendar | ✅ | ✅ | At parity |
| Outlook | ✅ | ❌ | Add |
| Calendly | ✅ | ❌ | Add |
| **Comms** |  |  |  |
| Slack | ✅ | 🟡 partial | Polish |
| Teams | ✅ | ❌ | Add |
| Discord | ✅ | ❌ | Add |
| **Email** |  |  |  |
| SendGrid | ✅ | 🟡 SMTP | Polish |
| Mailgun | ✅ | ❌ | Add |
| AWS SES | ✅ | ❌ | Add (compliance) |
| Postmark | ✅ | ❌ | Add |
| **SMS / Messaging** |  |  |  |
| Twilio SMS | ✅ | 🟡 webhook only | Polish |
| MessageBird | ✅ | ❌ | Add |
| WhatsApp Business | 🟡 | ✅ routes shipped | **Convis ahead** |
| Telegram | ✅ | ❌ | Add |
| **Workflow** |  |  |  |
| Zapier | ✅ | ❌ | Add app |
| Make | ✅ | ❌ | Add app |
| n8n | 🟡 | ✅ custom nodes | **Convis ahead** |
| Pipedream | ✅ | ❌ | Add |
| **Analytics** |  |  |  |
| Mixpanel | ✅ | ❌ | Add |
| Amplitude | ✅ | ❌ | Add |
| Segment | ✅ | ❌ | Add (key for distribution) |
| Heap | ✅ | ❌ | Add |
| **Storage** |  |  |  |
| AWS S3 | ✅ | 🟡 implicit (Twilio recordings) | Add user-bucket |
| GCS | ✅ | ❌ | Add |
| Dropbox / Box | ✅ | ❌ | Lower priority |
| **Project management** |  |  |  |
| Jira | 🟡 | ✅ route exists | **Convis ahead** |
| Asana | ✅ | ❌ | Add |
| Notion | ✅ | ❌ | Add |
| Linear | ✅ | ❌ | Add |
| ClickUp | ✅ | ❌ | Add |
| **Database connectors** |  |  |  |
| Postgres | ✅ via custom tool | ❌ | Add via tool framework |
| MySQL | ✅ via custom tool | ❌ | Add via tool framework |
| **Payment** |  |  |  |
| Stripe | ✅ for billing + customer tools | ❌ | Add |
| Square | ✅ | ❌ | Lower priority |
| **E-commerce** |  |  |  |
| Shopify | ✅ | ❌ | Add for retail vertical |
| WooCommerce | ✅ | ❌ | Lower priority |
| **Voice apps** |  |  |  |
| Alexa / Google Home | 🟡 | ❌ | Lower priority |
| **Custom / generic** |  |  |  |
| Generic webhook (HTTP POST) | ✅ HMAC-signed | 🟡 internal only | Expose customer-facing |
| Generic OAuth flow for tools | ✅ | ❌ | Add |
| Generic API key auth | ✅ | ❌ | Add to tool framework |

### 2.10 — SDKs / client libraries

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Web SDK (`@vapi-ai/web`) | ✅ | ❌ | **Major gap** — wraps audio capture, transcript, events |
| iOS SDK (Swift Package) | ✅ | ❌ | Major gap |
| Android SDK (Kotlin) | ✅ | ❌ | Major gap |
| React Native SDK | ✅ | ❌ | Major gap |
| Flutter SDK | ✅ | ❌ | Add later |
| Python SDK (server-side) | ✅ | ❌ | Easy add |
| Node SDK (server-side) | ✅ | ❌ | Easy add |
| Go SDK | ✅ | ❌ | Lower priority |
| Ruby SDK | ✅ | ❌ | Lower priority |
| PHP SDK | ✅ | ❌ | Lower priority |

### 2.11 — Developer experience / tooling

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Public REST API | ✅ versioned, documented | 🟡 FastAPI auto-OpenAPI uncurated | Curate + version |
| Public docs portal (Mintlify / ReadMe) | ✅ docs.vapi.ai | ❌ | Add |
| Quickstart guides per use case | ✅ | ❌ | Add (5–10 walkthroughs) |
| Postman / Insomnia workspace | ✅ | ❌ | Add |
| CLI tool (`vapi` command) | ✅ | ❌ | Lower priority |
| Conversation simulator (text playground) | ✅ | ❌ | Big DX win |
| Sandbox API keys | ✅ | ❌ | Add |
| Sandbox vs production environments | ✅ | ❌ | Add |
| Project templates / starter repos | ✅ GitHub | ❌ | Add 5+ samples |
| Live changelog | ✅ | ❌ | Add |
| RSS / email digest of changes | ✅ | ❌ | Add |
| Discord/Slack community | ✅ | ❌ | Add when audience exists |
| Office hours / live workshops | ✅ | ❌ | Add when audience exists |
| Status page (real metrics) | ✅ | ❌ | **Add immediately** |
| API rate-limit headers (X-RateLimit-Remaining etc.) | ✅ | ❌ | Add when rate limiting lands |
| Idempotency keys | ✅ | ❌ | Add for outbound dial |
| Pagination cursors (not just skip/limit) | ✅ | 🟡 skip/limit | Upgrade to cursors |

### 2.12 — Dashboard / UI

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Assistant create/edit | ✅ rich form | ✅ but 2,800-line single component | Refactor when touched |
| Assistant duplication | ✅ | ❌ | Easy add |
| Assistant versioning | ✅ | ❌ | Add (link to test suite) |
| Voice library browser | ✅ | 🟡 dropdown | Polish |
| KB upload + manage | ✅ | ✅ | At parity |
| Phone number management | ✅ | ✅ shipped today | At parity |
| Number purchase wizard | ✅ | 🟡 basic | Polish |
| Call list with filters | ✅ rich | 🟡 basic | Add date/sentiment/duration filters |
| Call detail (timeline + audio + transcript) | ✅ | 🟡 modal | Polish into dedicated page |
| Live call monitoring | ✅ | ❌ | Add via LiveKit |
| Per-call cost breakdown | ✅ | 🟡 data exists, no UI | Wire |
| Per-call latency breakdown | ✅ STT/LLM/TTS | ❌ | Add |
| Sentiment per turn | ✅ | 🟡 stored, no UI | Wire |
| Search across transcripts | ✅ full-text | 🟡 basic substring | Atlas Search |
| User & team management | ✅ RBAC | 🟡 admin/user only | Add team roles |
| Member invites + SSO | ✅ | ❌ | Add |
| Audit log (admin actions) | ✅ | ❌ | **Compliance need** |
| Analytics dashboard (calls, duration, success) | ✅ | 🟡 dashboard route exists | Polish |
| Alerts (slack/email on triggers) | ✅ | ❌ | Add |
| Custom KPI widgets | ✅ | ❌ | Lower priority |
| Light/dark mode | ✅ | ✅ | At parity |
| Mobile-responsive | ✅ | 🟡 untested | Audit + fix |

### 2.13 — Observability & analytics

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Real-time call dashboard | ✅ | ❌ | Add |
| Call timeline view (waterfall) | ✅ | ❌ | Add |
| Audio waveform visualization | ✅ | ❌ | Add |
| Per-stage latency breakdown | ✅ STT/LLM/TTS/network | ❌ logs only | Add |
| Transcript with timestamps | ✅ | 🟡 stored, no UI | Polish |
| Tool invocation timeline | ✅ | n/a | Add when tools land |
| Cost per call | ✅ | 🟡 cost_calculator | Wire UI |
| Cost per minute | ✅ | 🟡 | Wire UI |
| Margin per call (revenue - cost) | ✅ | ❌ | Add when billing lands |
| Sentiment per turn + overall | ✅ | 🟡 | Wire UI |
| Conversation quality score | ✅ rubric-based | ❌ | Add |
| Silence / interruption / talk-time ratios | ✅ | ❌ | Add |
| Words-per-minute (agent vs caller) | ✅ | ❌ | Add |
| ASR confidence scores | ✅ | 🟡 stored | Surface in UI |
| Custom KPIs | ✅ rubrics | ❌ | Add |
| Funnel analysis (multi-step success) | ✅ | ❌ | Add |
| Cohort analysis | ✅ | ❌ | Add |
| A/B test result reporting | ✅ | ❌ | Add when A/B lands |
| Exportable reports (PDF, CSV) | ✅ | 🟡 CSV partial | Polish |
| Scheduled email reports | ✅ | ❌ | Add |
| Webhook on call end | ✅ | 🟡 internal post-call only | Expose externally |
| Slack/Teams alerts | ✅ | ❌ | Add |
| PagerDuty | ✅ | ❌ | Add |
| Datadog / Grafana export | ✅ | 🟡 CloudWatch | Add Grafana |

### 2.14 — Reliability & SLA

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| 99.9 / 99.95 / 99.99% uptime SLA | ✅ docs.vapi.ai | ❌ unmeasured | Measure first |
| Multi-region deployment | ✅ | ❌ us-east-1 only | Add when needed |
| Active-active failover | ✅ | ❌ | Major effort |
| Provider failover (Deepgram→Whisper) | ✅ | ❌ | Add (provider abstraction first) |
| Database replication (multi-region) | ✅ Atlas global | ❌ | Atlas can do this |
| Backup + restore | ✅ Atlas continuous | 🟡 unknown for VPS | Move to Atlas |
| Point-in-time recovery | ✅ | 🟡 | Atlas Dedicated |
| DR drills (annual) | ✅ | ❌ | Add |
| Chaos engineering | ✅ | ❌ | Add Litmus or similar |
| Load testing (per release) | ✅ | ❌ | Add k6 / Artillery |
| Capacity planning docs | ✅ | ❌ | Add |
| Auto-scaling per region | ✅ | 🟡 App Runner auto-scales | Document |
| Concurrent call limits documented | ✅ | ❌ | Document |
| Health checks per dependency | ✅ | 🟡 /health endpoint | Add per-dep |

### 2.15 — Security & compliance

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| SOC 2 Type II | ✅ | ❌ | 6–9 months prep |
| HIPAA BAA available | ✅ | ❌ | Vendor BAAs first |
| GDPR DPA | ✅ | 🟡 | Audit, formalize |
| CCPA | ✅ | 🟡 | Audit, formalize |
| ISO 27001 | ✅ | ❌ | Lower priority |
| PCI DSS | n/a (no cards) | n/a | n/a |
| Data residency options (EU, US, IN) | ✅ | ❌ | Single region |
| Encryption at rest (KMS-backed) | ✅ | 🟡 Mongo default | Add CMK |
| Encryption in transit (TLS 1.3) | ✅ | ✅ | At parity |
| Customer-managed keys (BYOK) | ✅ enterprise | ❌ | Add for enterprise |
| VPC peering / private endpoints | ✅ | ❌ | Add when asked |
| IP allowlisting | ✅ | ❌ | Add |
| SSO / SAML | ✅ | ❌ | Add (Workspace + Okta) |
| SCIM provisioning | ✅ | ❌ | Add for enterprise |
| 2FA / MFA enforcement | ✅ | ❌ | Add |
| Audit logs (admin actions) | ✅ | ❌ | **Add for compliance** |
| Audit log API (export) | ✅ | ❌ | Add |
| PII redaction in logs | ✅ | 🔒 caller phone in CloudWatch | Fix today |
| PII redaction in transcripts (live) | ✅ | ❌ | Add |
| Per-call data retention | ✅ | ❌ | Add |
| Right to erasure (GDPR) | ✅ | 🟡 manual | Automate |
| Data export (GDPR portability) | ✅ | ❌ | Add |
| Pen-test reports (annual) | ✅ | ❌ | Hire |
| Bug bounty | ✅ | ❌ | Add when scale supports |
| Security questionnaire ready (SIG, CAIQ) | ✅ pre-filled | ❌ | Pre-fill |
| Vendor risk register | ✅ | ❌ | Add |
| Webhook signature verification (Twilio) | ✅ | ✅ shipped today | At parity |
| Webhook replay protection (timestamp window) | ✅ | ❌ | **Add today** |
| Per-tenant rate limiting | ✅ | ❌ | Add |
| Function-tool sandbox (URL allowlist, no SSRF) | ✅ | n/a | Build before tools |
| JWT crit-header validation | ✅ jwcrypto | ✅ shipped today (PyJWT 2.12) | At parity |

### 2.16 — Pricing / billing

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Per-minute usage tracking | ✅ | ✅ cost_calculator | At parity |
| Per-second granularity | ✅ | 🟡 minute | Polish |
| Usage-based billing (Stripe metered) | ✅ | ❌ | Major effort |
| Customer-facing invoices | ✅ | ❌ | Tied to billing |
| Free tier / trial credits | ✅ | ❌ | Tied to billing |
| Spend caps / budget alerts | ✅ | ❌ | Add |
| Self-service plan upgrade | ✅ | ❌ | Tied to billing |
| Volume discounts / annual contracts | ✅ | ❌ | Tied to billing |
| Multi-currency | ✅ | 🟡 USD only | Add |
| Tax handling (Stripe Tax / Avalara) | ✅ | ❌ | Tied to billing |
| Multiple payment methods (card / ACH / wire) | ✅ | ❌ | Tied to billing |
| Auto-recharge | ✅ | ❌ | Add |
| Dunning / payment retries | ✅ | ❌ | Add |
| Per-cost-center invoicing | ✅ enterprise | ❌ | Add for enterprise |
| Pricing calculator (public) | ✅ | ❌ | Add |
| Public pricing page | ✅ | ❌ | Add |
| Reseller / partner program | ✅ | ❌ | Add when audience exists |

### 2.17 — Customer experience

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Self-service onboarding wizard | ✅ | ❌ | Add |
| Interactive product tour | ✅ | ❌ | Add (Userflow) |
| In-app help / chat | ✅ | ❌ | Add (Intercom) |
| Email support | ✅ | 🟡 informal | Formalize |
| 24/7 support tier | ✅ enterprise | ❌ | Add when paid |
| Dedicated CSM | ✅ enterprise | ❌ | Add when paid |
| Solution architect | ✅ enterprise | ❌ | Add when paid |
| Migration tools (from competitors) | ✅ | ❌ | Add (Vapi importer) |
| Branding / white-label | ✅ enterprise | ❌ | Add |
| Custom domains | ✅ | ❌ | Add |
| Co-branded customer portal | ✅ | ❌ | Add |

### 2.18 — Marketing & community

| Capability | Vapi | Convis | Notes |
|---|---|---|---|
| Public docs portal | ✅ | ❌ | Add |
| Blog with technical content | ✅ | 🟡 site exists | Add posts |
| Customer case studies | ✅ | ❌ | Add 3–5 |
| Customer logos | ✅ | ❌ | Add |
| Public changelog | ✅ | ❌ | Add |
| Webinars / workshops | ✅ | ❌ | Add quarterly |
| Discord / Slack community | ✅ | ❌ | Add when audience > 50 |
| GitHub presence | ✅ open SDKs | ❌ | Open-source SDKs when ready |
| Affiliate program | ✅ | ❌ | Add later |
| Reseller program | ✅ | ❌ | Add later |

---

## Section 3 — Where Convis is genuinely ahead

Don't undersell what you have. Convis differentiation that Vapi can't match easily:

1. **India-first language coverage** — Hindi/Hinglish/Devanagari handling, Sarvam evaluation in flight, Vobiz for Indian carriers. Vapi's multilingual is broad but not India-tuned.
2. **Self-hostable / single-tenant deploy option** — banks, hospitals, defense customers that need their own VPC. Vapi-SaaS can't serve cleanly.
3. **n8n custom nodes already shipped** — workflow automation is your installed base.
4. **WhatsApp Business + Calendar + Jira native routes** — productized integrations Vapi treats as 3rd-party.
5. **Cost transparency** — `cost_calculator.py` already does per-call attribution; needs the dashboard wiring but data is there.
6. **Knowledge base on Mongo (not Pinecone)** — fewer vendors in stack, lower ops burden, easier compliance story.

These are real differentiators. Lead with them in positioning. Don't chase Vapi's full feature breadth — pick the 2–3 wedges and dominate.

---

## Section 4 — Catch-up checklist (deeply granular, area-by-area)

Each area has tasks tagged P0 (blocker) / P1 (closes obvious gap) / P2 (polish). Effort tags as defined above.

### 4.1 — Compliance & security baseline (P0 — existential)

#### Vendor BAAs / contracts
- [ ] **[M]** Sign AWS BAA in Artifact, enable for the account
- [ ] **[M]** Sign Twilio HIPAA Eligibility (enroll voice + programmable SMS)
- [ ] **[M]** Upgrade LiveKit Cloud to Scale/Enterprise tier and sign BAA
- [ ] **[M]** Upgrade Deepgram to Enterprise / Growth Plus and sign BAA
- [ ] **[M]** Apply for OpenAI Zero Data Retention + Enterprise BAA
- [ ] **[M]** Upgrade ElevenLabs to Enterprise tier and sign BAA (or switch TTS to PlayHT/AWS Polly which have BAAs at standard tier)
- [ ] **[S]** MongoDB Atlas Dedicated cluster (M10+) and sign BAA. Migrate Mongo off VPS.
- [ ] **[XS]** Hostinger VPS — confirm no PHI ever touched it; remove from infra map.
- [ ] **[S]** Maintain a vendor BAA register (vendor / contract date / scope / expiry / contact) in shared doc.

#### Privacy / data handling
- [ ] **[S]** Strip caller phone numbers from CloudWatch INFO logs (hash or last-4 only).
- [ ] **[S]** Move full transcripts to a separate, BAA-covered, encrypted, ≤30d-retention log group with strict IAM.
- [ ] **[S]** Audit every `logger.info/error` for PII echoes; redact.
- [ ] **[M]** Live PII redaction filter on Deepgram transcripts (regex SSN/credit card/email/phone before storing).
- [ ] **[S]** Per-tenant data retention controls (call_logs, recordings, transcripts) — settable in dashboard.
- [ ] **[S]** Right-to-erasure endpoint: deletes user + all owned resources cascading.
- [ ] **[S]** Data-export endpoint (GDPR portability): user clicks "Export my data" → S3 zip URL emailed.
- [ ] **[S]** Per-call recording retention override (some calls are legal-hold → never delete).

#### Encryption / key management
- [ ] **[M]** Move from manually-rotated `ENCRYPTION_KEY` to AWS KMS-backed envelope encryption.
- [ ] **[M]** Implement dual-key reads during rotation (current key + previous key both tried; re-encrypt to current on read).
- [ ] **[S]** Re-encrypt existing `provider_connections` rows that are unreadable (we hit this during the dedupe today).
- [ ] **[XS]** KMS CMK on S3 buckets, ECR images, CloudWatch logs, Atlas (M10+ Customer KMS).
- [ ] **[M]** BYOK (customer-managed keys) for enterprise tier.

#### Auth / authn / authz
- [ ] **[S]** Audit remaining 35 routes for IDOR — sweep `@router.get/put/delete("...{some_id}")` patterns; ensure ownership check or admin override.
- [ ] **[S]** Audit ALL webhook routes for `Depends(verify_twilio_signature)` (only inbound was swept today).
- [ ] **[S]** Add Twilio webhook timestamp window check (5 min) — without it, signed body is replayable forever.
- [ ] **[S]** SSO / SAML / OIDC (Google Workspace + Okta IdPs) for enterprise.
- [ ] **[M]** SCIM 2.0 provisioning endpoint for IdP-driven user lifecycle.
- [ ] **[S]** 2FA / MFA enforcement (TOTP via authy/Google Authenticator).
- [ ] **[S]** Session revocation on logout / password change (today: stateless JWT, no revoke list).
- [ ] **[S]** Refresh token + short-lived access token (today: 24h JWT).

#### Multi-tenant Twilio (architectural blocker)
- [ ] **[M]** `verify_twilio_signature` looks up the right `auth_token` per assistant_id (assistant → user_id → provider_connections.twilio.auth_token).
- [ ] **[M]** Same lookup for outbound dial — use the owning user's Twilio creds.
- [ ] **[M]** Subaccount-per-customer model OR keep BYO with per-call credential routing. Document decision.
- [ ] **[S]** Health-check endpoint per customer Twilio account (verify creds still work).

#### Audit logs
- [ ] **[M]** App-level audit log: every assistant edit, phone-number assign, KB upload, user invite, payment-method change → immutable doc with actor / target / timestamp / before / after.
- [ ] **[S]** Audit log export endpoint (paginated, JSON & CSV).
- [ ] **[XS]** CloudTrail enabled all-regions, log-file integrity validation on.

#### Tool sandboxing (before shipping function tools)
- [ ] **[M]** URL allowlist enforced at HTTP fetch time — no `169.254.0.0/16`, no `127.0.0.1`, no `localhost`, no `.internal`.
- [ ] **[M]** DNS rebinding protection (resolve once, pin IP for duration of request).
- [ ] **[S]** Per-tool max body size, max latency, max retries.
- [ ] **[S]** Per-tool credential vault (encrypted at rest).

#### Pen-test / external review
- [ ] **[L]** Annual external pen-test (Cure53 / Doyensec / Trail of Bits).
- [ ] **[S]** Bug bounty program (HackerOne / Intigriti) when scale supports.
- [ ] **[S]** Pre-fill SIG and CAIQ security questionnaires.

### 4.2 — Reliability / SLA (P0)

- [ ] **[XS]** Status page wired to App Runner + LiveKit + Mongo + OpenAI + Deepgram + ElevenLabs health (Better Uptime / Statuspage.io).
- [ ] **[S]** /health endpoint returns per-dependency status (Mongo ping, LiveKit token mint, OpenAI 1-token call, Deepgram WS handshake).
- [ ] **[XS]** PagerDuty / Slack alerts on health-check failure.
- [ ] **[S]** On-call runbook: ASR not working, OpenAI 429s, recording failure, ECR silent push, encryption key drift.
- [ ] **[S]** Capacity docs: max concurrent calls per agent_worker process, max calls per Twilio account, max Mongo connections.
- [ ] **[M]** Load testing — k6 or Artillery, run on every release. Target: 50 concurrent calls smooth, 100 graceful degradation.
- [ ] **[L]** Multi-region deployment (us-east-1 primary, us-west-2 hot standby).
- [ ] **[L]** Active-active failover between regions.
- [ ] **[M]** Provider failover: Deepgram down → fall through to Whisper. ElevenLabs down → fall through to Cartesia. Need provider abstraction layer first.
- [ ] **[S]** Atlas continuous backup + point-in-time recovery enabled.
- [ ] **[S]** Annual DR drill: restore Mongo from backup, verify no data loss.
- [ ] **[M]** Chaos engineering: scheduled brownouts (kill agent_worker mid-call, drop OpenAI for 30s, etc.). Verify graceful degradation.

### 4.3 — CI/CD & quality (P0 — currently ZERO)

- [ ] **[XS]** `.github/workflows/api.yml`: pytest + pip-audit + ruff/black on every PR.
- [ ] **[XS]** `.github/workflows/web.yml`: install + build + lint + tsc on every PR.
- [ ] **[XS]** `.github/workflows/security.yml`: weekly cron — pip-audit, npm audit, secrets scan (gitleaks).
- [ ] **[S]** Repair the 17 broken livekit/* tests (`get_current_user` signature drift).
- [ ] **[S]** Set up vitest for `convis-web` — at least 10 tests covering login, ai-agent edit form, phone-numbers tab, dashboard.
- [ ] **[S]** Set up Playwright for end-to-end (login → create assistant → assign number → see in dashboard).
- [ ] **[XS]** Branch protection: require PR + 1 approval + green CI before merge to main.
- [ ] **[S]** Auto-deploy to staging environment on merge to main; manual promote to prod.
- [ ] **[XS]** Pin transitive deps and run `pip-audit` / `npm audit` weekly.
- [ ] **[S]** Pre-commit hooks: ruff, gitleaks, prettier.
- [ ] **[XS]** Delete deprecated dirs (`bolna-master/`, `n8n-custom-nodes/` if unused).
- [ ] **[S]** Coverage tool — pytest-cov (target 60% on api), v8 coverage (target 50% on web).
- [ ] **[S]** Mutation testing on critical paths (mutmut or cosmic-ray) for auth, signature verification, ownership checks.

### 4.4 — Voice / audio pipeline (P1)

#### Provider abstraction
- [ ] **[M]** STT provider interface — `Stt(provider, model, language, ...)` with implementations for Deepgram, Whisper, GPT-4o-Transcribe, Talkscriber, Gladia.
- [ ] **[M]** LLM provider interface — same pattern. Implementations: OpenAI, Anthropic, Groq, Together, BYO HTTPS endpoint.
- [ ] **[M]** TTS provider interface. Implementations: ElevenLabs, Cartesia, PlayHT, OpenAI TTS, Deepgram Aura, Azure.
- [ ] **[S]** Per-call provider override (today: per-assistant only).
- [ ] **[S]** Cost normalization across providers (per-token, per-character, per-second).

#### STT depth
- [ ] **[S]** Custom vocabulary / boost words per assistant (brand names, product SKUs).
- [ ] **[S]** STT auto-detect + confidence threshold.
- [ ] **[S]** Keyword filler removal ("um, uh, you know").
- [ ] **[XS]** Audio-only debug recording mode (capture raw audio for ASR tuning without LLM/TTS).

#### LLM depth
- [ ] **[S]** Add Anthropic Claude (already has streaming + tools).
- [ ] **[S]** Add Groq for sub-500ms low-latency.
- [ ] **[M]** BYO LLM endpoint (OpenAI-compatible URL + API key).
- [ ] **[S]** Per-call model override via API.
- [ ] **[S]** Token budget per call (not just max_tokens per response).

#### TTS depth
- [ ] **[M]** Voice cloning wizard: upload sample → ElevenLabs/PlayHT API → store voice_id → expose in agent edit.
- [ ] **[S]** Voice library marketplace (curated 50+ voices, filterable by language/gender/style).
- [ ] **[S]** Background audio (stream room noise / call-center bg under TTS).
- [ ] **[S]** Filler words ("uh, let me check…") emitted while LLM thinks.
- [ ] **[S]** Audio prompts: insert pre-recorded clips (legal disclaimers, hold music).
- [ ] **[XS]** Speed/pitch/stability/similarity controls in UI.

#### Latency budgets
- [ ] **[S]** Per-stage latency tracking (STT_ms, LLM_TTFT_ms, TTS_TTFB_ms, network_ms) — measure, log, surface.
- [ ] **[S]** Latency budget alerts (p95 > 1500ms → page).

### 4.5 — Telephony / phone features (P1)

#### Call control
- [ ] **[M]** **Warm transfer** — agent says "let me transfer you to John, hold on" → conferences in John → drops out. LiveKit supports this via room participants.
- [ ] **[M]** **Cold transfer** — agent dials a number, drops the customer onto it.
- [ ] **[M]** **Conference call** — 3-way (agent + 2 humans).
- [ ] **[S]** **Hold + hold music** — pause TTS/LLM, play music.
- [ ] **[S]** **Whisper / supervisor mode** — manager listens via separate audio track + can prompt the agent.
- [ ] **[S]** **Live monitoring** — admin in dashboard listens to active call (LiveKit subscriber-only token).
- [ ] **[S]** **Recording start/stop mid-call** — for compliance regions where consent required.

#### DTMF / IVR
- [ ] **[M]** **DTMF detection** during conversation (Twilio sends keypad as input).
- [ ] **[M]** **DTMF input collection** ("press 1 for sales") — block in workflow.
- [ ] **[S]** **DTMF output** (play tones) — rare but needed for IVR navigation.
- [ ] **[M]** **IVR menu builder** — phone tree before AI takes over (or AI-first with DTMF fallback).

#### Routing
- [ ] **[S]** **Time-of-day / business-hours routing** — "after 6pm route to voicemail".
- [ ] **[S]** **Failover routing** — primary assistant down → backup.
- [ ] **[S]** **Geographic routing** — caller area code → regional assistant.
- [ ] **[S]** **Round-robin** between multiple numbers / assistants.

#### Voicemail
- [ ] **[S]** **Voicemail detection (AMD)** via Twilio's `MachineDetection=Enable` parameter on outbound.
- [ ] **[S]** **Voicemail drop** — pre-recorded message left on AMD-detected line, agent disconnects.
- [ ] **[S]** **Voicemail transcription** for inbound calls that go to vmail.

#### Compliance helpers
- [ ] **[S]** **Pre-call disclosure** — "this call may be recorded" before AI begins.
- [ ] **[S]** **DNC list** — outbound calls check against a Do-Not-Call list before dialing.
- [ ] **[S]** **TCPA pacing** — outbound campaigns respect quiet hours per state.
- [ ] **[S]** **Caller-ID verification (STIR/SHAKEN)** for outbound legitimacy.

#### Quality
- [ ] **[S]** **MOS score / jitter / packet loss** dashboard (call_quality fields exist, surface them).
- [ ] **[S]** **Concurrent call cap** per Twilio number, per account.

#### Numbers
- [ ] **[S]** Number purchase wizard — area code search, capabilities filter, prefill.
- [ ] **[S]** Number porting workflow.
- [ ] **[S]** Toll-free + 10DLC SMS registration UI.
- [ ] **[S]** Vanity number search.

### 4.6 — Voice agent capabilities (P1)

#### Persona
- [ ] **[XS]** Persona presets ("dental receptionist", "real-estate qualifier", "support tier-1") — curate 10.
- [ ] **[XS]** Variable injection in system prompt — `{{customer_name}}`, `{{order_id}}`, etc., bound at call start.
- [ ] **[S]** First-message templating (separate from system prompt).

#### Form filling / slot
- [ ] **[M]** **Slot-filling framework** — define fields (name, email, date) with type, validation, confirmation. Agent automatically asks until filled.
- [ ] **[S]** **Numeric input collector** — phone, OTP, account number — DTMF + speech with retry.
- [ ] **[S]** **Date/time parser** — natural language ("next Tuesday at 3pm in IST") → ISO.
- [ ] **[S]** **Address collector** — multi-line with normalization (USPS / Google Maps).
- [ ] **[S]** **Spell-it-out** mode for emails/codes.
- [ ] **[S]** **Confirmation pattern** — read back collected slots, ask "is that correct?".

#### Memory
- [ ] **[M]** **Long-term memory** — past calls retrievable as RAG over conversation history (per caller phone number or email).
- [ ] **[S]** **Per-conversation context window** management (drop old turns gracefully).
- [ ] **[S]** **Cross-call notes** — agent appends to a "customer file" each call.

#### Goals / outcomes
- [ ] **[M]** **Conversation analysis rubrics** — define success criteria per assistant (e.g., "did caller agree to schedule?"); LLM evaluates each call against rubric.
- [ ] **[S]** **Action item extraction** — LLM pulls TODOs from each call.
- [ ] **[S]** **Conversation summary** — auto-generated, stored, surfaced in dashboard.

#### Multilingual
- [ ] **[S]** Code-switch examples in prompt for top 5 Indian languages (Hindi, Marathi, Telugu, Tamil, Bengali).
- [ ] **[S]** Per-language voice mapping (Hindi → ElevenLabs Hindi voice, English → Rachel).

### 4.7 — Tool / function calling (P1)

#### Framework
- [ ] **[M]** **Generic tool framework** — config-driven. Tool def stored as JSON schema in assistant doc:
```json
{
  "name": "lookup_order",
  "description": "Get order status by order ID",
  "method": "GET",
  "url": "https://customer.com/api/orders/{order_id}",
  "auth": "bearer_token",
  "params": [{"name":"order_id","type":"string","required":true}]
}
```
- [ ] **[S]** Tool result formatting → natural language ("Your order #1234 ships tomorrow").
- [ ] **[S]** Tool retries (3x with backoff), timeout (5s default), error messages.
- [ ] **[S]** Per-tool credential vault (encrypted, scoped to user).
- [ ] **[M]** Tool sandbox (URL allowlist + DNS rebind protection — see security section).
- [ ] **[S]** Async / long-running tools (poll-and-update pattern).
- [ ] **[S]** Tool call logging (request, response, latency) — debuggable.
- [ ] **[M]** **MCP server integration** (industry-emerging tool protocol).

#### Built-in tools
- [ ] **[XS]** transfer_call (warm + cold)
- [ ] **[XS]** end_call (already shipped)
- [ ] **[S]** send_sms via Twilio
- [ ] **[S]** send_whatsapp (WhatsApp routes already exist, expose as tool)
- [ ] **[S]** send_email
- [ ] **[S]** schedule_event (Google Calendar — already wired)
- [ ] **[S]** lookup_knowledge (KB query — already wired)
- [ ] **[S]** create_ticket (Zendesk / Intercom / Jira)
- [ ] **[S]** create_lead (HubSpot / Salesforce)
- [ ] **[S]** record_payment (Stripe)

### 4.8 — Knowledge base / RAG (P1)

#### Ingestion
- [ ] **[S]** URL scraper (Playwright-based) — paste URL, scrape, chunk, embed.
- [ ] **[S]** Notion connector (OAuth → import pages).
- [ ] **[S]** Google Drive connector.
- [ ] **[S]** Confluence connector.
- [ ] **[S]** Sitemap.xml ingestion (whole-site index).
- [ ] **[S]** YouTube transcript ingestion.

#### Retrieval quality
- [ ] **[S]** **Hybrid search** (BM25 + vector) — Atlas Search for keyword + existing vector for semantic.
- [ ] **[M]** **Re-ranking** with Cohere or BGE rerank.
- [ ] **[S]** **Query rewriting** (HyDE — generate hypothetical answer first, embed it, search).
- [ ] **[S]** **Multi-query retrieval** (LLM generates 3 query variants, fan out).
- [ ] **[S]** **Citation tracking** — return doc_id + chunk_id; surface in transcript.
- [ ] **[S]** **Semantic chunking** (instead of fixed-size).
- [ ] **[S]** Configurable chunk size / overlap per assistant.
- [ ] **[S]** Configurable embedding model.

#### Auto-FAQ
- [ ] **[M]** Auto-FAQ generation — on doc upload, run LLM pass to extract Q&A pairs; store as separate KB entries; rebalance retrieval to prefer Q&A on questions.

#### Ops
- [ ] **[S]** KB versioning (pin a snapshot to an assistant version).
- [ ] **[S]** KB freshness — periodic re-fetch of URL-sourced docs (cron).
- [ ] **[S]** KB analytics — most-queried, miss rate, low-confidence answers.
- [ ] **[S]** **Tenant isolation tests** for `knowledge_chunks` (untested today — confirmed gap).
- [ ] **[S]** Encrypted KB storage (KMS).

### 4.9 — Workflow / orchestration & multi-agent (P1)

#### Visual workflow builder (reactflow already a dep)
- [ ] **[L]** Block builder UI: drag-drop nodes (greeting, question, condition, tool, transfer, voicemail, end).
- [ ] **[M]** Variables passed between blocks ("{{collected_email}}" available downstream).
- [ ] **[M]** Conditional branching (if/else on variable values, intent matches).
- [ ] **[M]** Loops / retry blocks ("ask again if not confirmed").
- [ ] **[M]** Sub-workflow composition.
- [ ] **[S]** Workflow versioning.
- [ ] **[S]** Workflow preview / simulator (text mode, no audio).
- [ ] **[M]** Workflow A/B test (split traffic across versions).

#### Squads (multi-agent)
- [ ] **[L]** Squad data model: collection of assistants with hand-off rules.
- [ ] **[M]** Hand-off mechanism: agent calls `transfer_to_squad_member(member_id, context)`. New agent joins room with shared transcript.
- [ ] **[M]** Routing rules: by intent, by collected variable, by LLM decision.
- [ ] **[S]** Hierarchical agents (greeter → specialist → senior).
- [ ] **[S]** Squad analytics — handoff rate, hand-back rate, success per member.

### 4.10 — Calendar & scheduling (P2)

- [ ] **[S]** Outlook / Microsoft 365 integration.
- [ ] **[S]** Calendly / Cal.com connectors.
- [ ] **[S]** Multi-calendar booking (find slot across N calendars).
- [ ] **[S]** Reminder calls (24h + 1h before appt) via campaign system.
- [ ] **[S]** Reschedule / cancel via voice (already has rescheduling — polish).
- [ ] **[S]** Buffer time / slot duration UI.
- [ ] **[S]** Per-day availability rules (today: hardcoded).
- [ ] **[S]** Holiday calendar awareness (skip Indian/US public holidays).

### 4.11 — Integrations / ecosystem (P1/P2)

#### CRM (P1 — sales-critical)
- [ ] **[M]** Salesforce: OAuth flow, contact lookup, lead creation, activity logging.
- [ ] **[S]** HubSpot: polish existing route, add object types beyond contacts.
- [ ] **[S]** Pipedrive.
- [ ] **[S]** Zoho CRM.
- [ ] **[S]** Close.io.

#### Help desk (P1)
- [ ] **[S]** Zendesk: ticket creation from call, append transcript.
- [ ] **[S]** Intercom.
- [ ] **[S]** Freshdesk.

#### Comms (P1)
- [ ] **[S]** Slack: incoming-webhook for call alerts, slash commands for outbound dial.
- [ ] **[S]** Microsoft Teams: same.
- [ ] **[XS]** Discord: nice-to-have for tech audiences.

#### Workflow (P1)
- [ ] **[M]** Zapier app: triggers (call.started, call.ended, transcript.final), actions (start outbound call, update assistant).
- [ ] **[M]** Make (Integromat) app.
- [ ] **[XS]** n8n: already shipped, polish & document.
- [ ] **[S]** Pipedream.

#### Analytics (P2)
- [ ] **[S]** Segment connector (events → analytics destinations).
- [ ] **[S]** Mixpanel direct.
- [ ] **[S]** Amplitude direct.

#### Storage (P2)
- [ ] **[S]** S3 user-bucket option for recordings (BYOK + customer-controlled retention).
- [ ] **[S]** GCS option.

#### Project management (P2)
- [ ] **[S]** Asana, Notion, Linear, ClickUp — same pattern as Jira (which exists).

#### Database connectors (via tool framework, P2)
- [ ] **[S]** Postgres connector (read-only, parameterized queries).
- [ ] **[S]** MySQL connector.

#### Payment (P2)
- [ ] **[S]** Stripe (already needed for Convis billing — kill two birds).
- [ ] **[XS]** Square (lower priority).

#### E-commerce (P2 — if retail customer)
- [ ] **[S]** Shopify connector — order lookup, inventory, customer history.
- [ ] **[S]** WooCommerce.

### 4.12 — SDKs & client libraries (P1 — biggest DX gap)

#### Web SDK (highest priority — enables click-to-call demos)
- [ ] **[M]** `@convis-ai/web` — wraps livekit-client + auth + transcript stream + tool events.
  - `convis.start({ assistantId, options })` returns a Call object
  - `call.on('transcript', cb)` / `call.on('toolCall', cb)` / `call.on('end', cb)`
  - `call.send(text)` to inject system message mid-call
  - `call.mute()` / `call.unmute()` / `call.end()`
  - Bundled UI widget (1 line: `<convis-call-button assistant-id="..." />`)

#### Mobile SDKs
- [ ] **[L]** iOS SDK (Swift Package, wraps livekit-ios).
- [ ] **[L]** Android SDK (Kotlin, wraps livekit-android).
- [ ] **[M]** React Native SDK (wraps `@livekit/react-native`).
- [ ] **[M]** Flutter SDK.

#### Server SDKs
- [ ] **[S]** `convis-py` — Python (assistant CRUD, call control, KB upload).
- [ ] **[S]** `convis-node` — TypeScript/Node.
- [ ] **[S]** `convis-go` — Go.
- [ ] **[XS]** OpenAPI auto-generated SDKs as a fallback for languages without manual SDK.

### 4.13 — Developer experience (P1)

- [ ] **[M]** Docs portal (Mintlify or ReadMe) at docs.convis.ai. Sections: Getting Started · Concepts · API Reference · SDKs · Tutorials · Cookbook.
- [ ] **[S]** 5–10 quickstart guides ("Inbound receptionist in 5 minutes," "Outbound qualifier from a Google Sheet," etc.).
- [ ] **[S]** Postman / Insomnia workspace, hosted.
- [ ] **[S]** Conversation simulator (text mode, no audio) — `simulate("hi") → "Welcome, how can I help?"`.
- [ ] **[S]** Sandbox API keys + sandbox environment (no real telephony costs).
- [ ] **[XS]** GitHub samples repo: 5+ starter apps (web widget, outbound bulk caller, KB chatbot, etc.).
- [ ] **[XS]** Public changelog (one-page, RSS-fed).
- [ ] **[XS]** Discord server (start small, invite-only initially).
- [ ] **[XS]** Versioned API (`/v1/...`).
- [ ] **[S]** Idempotency keys on POST routes.
- [ ] **[S]** Cursor-based pagination on list endpoints.
- [ ] **[S]** Rate-limit response headers (`X-RateLimit-Remaining`, `X-RateLimit-Reset`).
- [ ] **[XS]** OpenAPI spec curated (separate descriptions, examples) — not just FastAPI's auto-gen.
- [ ] **[S]** CLI (`convis` command): `convis assistants list`, `convis call dial +1...`, `convis logs tail`.

### 4.14 — Dashboard / UI polish (P2)

- [ ] **[M]** Refactor `app/ai-agent/page.tsx` (2,800 lines, single client component) into 5–10 sub-components with proper data fetching.
- [ ] **[S]** Assistant duplication (1-click clone with new name).
- [ ] **[S]** Assistant versioning (rollback to previous prompt).
- [ ] **[S]** Voice library browser (filter by language, gender, style; preview clip).
- [ ] **[S]** Number purchase wizard (capabilities, region, price preview).
- [ ] **[S]** Call list — date range filter, sentiment filter, duration filter, search across transcripts.
- [ ] **[M]** **Call detail page** (replace modal): timeline waterfall (audio waveform + transcript + tool calls + STT/LLM/TTS timing).
- [ ] **[S]** Live call monitoring (admin clicks active call → joins as listener via LiveKit subscriber-only token).
- [ ] **[S]** Per-call cost panel (already has data).
- [ ] **[S]** Per-call latency panel.
- [ ] **[S]** Sentiment + summary surfaced (already has data).
- [ ] **[S]** Search across transcripts (Atlas Search full-text index on `transcript_text`).
- [ ] **[S]** Team management (invites, roles: admin, member, viewer).
- [ ] **[S]** Audit log viewer (admin only).
- [ ] **[M]** Analytics dashboard widgets: calls/day, success rate, avg duration, p95 latency, top failure reasons.
- [ ] **[S]** Customizable alert rules (Slack/email) — "alert me when daily call volume drops 50%".
- [ ] **[S]** Mobile-responsive audit (today: untested on small viewports).
- [ ] **[XS]** Keyboard shortcuts (J/K to navigate calls, etc.).
- [ ] **[S]** A11y audit (WCAG 2.1 AA: focus order, color contrast, aria labels).

### 4.15 — Observability & analytics (P1)

- [ ] **[S]** Real-time call dashboard (active calls, ringing, completed in last 5 min).
- [ ] **[M]** Per-call timeline (waterfall): each turn has STT_start → STT_end → LLM_TTFT → LLM_complete → TTS_TTFB → TTS_complete.
- [ ] **[S]** Per-call cost breakdown UI (data exists in `call_logs.cost`).
- [ ] **[S]** Per-call latency p50/p95/p99 dashboards (CloudWatch or Grafana).
- [ ] **[S]** Sentiment per turn + overall (data exists, surface).
- [ ] **[S]** Conversation quality score (rubric-based — see 4.6).
- [ ] **[S]** Silence ratio, talk-time, words-per-minute.
- [ ] **[S]** Funnel analysis (per assistant: "% of calls hit greeting → % hit collection → % hit confirmation → % hit booking").
- [ ] **[S]** ASR confidence score histogram per assistant.
- [ ] **[S]** Failure-mode taxonomy (LLM 429, TTS WSS drop, STT silence, idle hangup).
- [ ] **[S]** Customer-facing webhook events (call.started, call.ended, transcript.partial, transcript.final, tool.invoked).
- [ ] **[S]** Webhook delivery retry + DLQ.
- [ ] **[S]** Slack/Teams alerts (configurable).
- [ ] **[XS]** PagerDuty integration.
- [ ] **[S]** Datadog / Grafana export of CloudWatch metrics.
- [ ] **[S]** Exportable reports (PDF, CSV) — daily/weekly/monthly.
- [ ] **[S]** Scheduled email reports.

### 4.16 — Pricing / billing (P2 — large effort)

- [ ] **[L]** Stripe metered billing setup — meters: `voice_minutes`, `llm_tokens`, `tts_chars`, `stt_seconds`.
- [ ] **[M]** Per-call cost attribution → meter increments (cost_calculator already does this).
- [ ] **[S]** Customer-facing invoices (Stripe-generated PDF).
- [ ] **[S]** Free tier: 30 minutes/month.
- [ ] **[S]** Trial credits ($10 worth on signup).
- [ ] **[S]** Spend caps / budget alerts (auto-pause when limit reached).
- [ ] **[S]** Self-service plan upgrade UI.
- [ ] **[S]** Volume discount tiers + annual contracts.
- [ ] **[S]** Multi-currency (Stripe handles).
- [ ] **[S]** Tax handling (Stripe Tax).
- [ ] **[S]** Auto-recharge.
- [ ] **[S]** Dunning / payment retries.
- [ ] **[S]** Public pricing page (per-minute breakdown).
- [ ] **[S]** Pricing calculator (interactive — slider for minutes, shows monthly cost).
- [ ] **[S]** Cost optimization hints in dashboard ("switch from gpt-4-turbo to gpt-4o-mini, save 80%").

### 4.17 — Customer experience (P2)

- [ ] **[S]** Self-service onboarding wizard (5 steps: connect Twilio, create assistant, upload KB, assign number, test call).
- [ ] **[S]** In-app product tour (Userflow / Appcues).
- [ ] **[S]** In-app chat widget (Intercom).
- [ ] **[S]** Email support (formal SLA).
- [ ] **[S]** Migration importer from Vapi (export Vapi config → import to Convis).
- [ ] **[S]** Branding / white-label option (custom logo, colors, custom domain).
- [ ] **[S]** Co-branded customer portal (for resellers).

### 4.18 — Marketing & community (P2)

- [ ] **[XS]** Status page (Better Uptime).
- [ ] **[XS]** Public changelog.
- [ ] **[S]** Blog with technical content (RAG techniques, latency tuning, Indian-language voice — write for SEO).
- [ ] **[S]** Customer case studies (3–5 in 6 months).
- [ ] **[XS]** Customer logos page.
- [ ] **[S]** Pricing page.
- [ ] **[S]** "Why Convis" page comparing to Vapi/Bland/Retell — position the wedge (India / self-hostable).
- [ ] **[S]** SOC 2 + HIPAA badges + Trust Center page.
- [ ] **[S]** Discord/Slack community when 50+ active users.
- [ ] **[XS]** Open-source the SDKs on GitHub.
- [ ] **[S]** Quarterly webinars / workshops.
- [ ] **[XS]** Affiliate program (Rewardful or similar).

---

## Section 5 — Critical-path sequencing (dependency-aware)

Some tasks block others. Order matters:

```
[Vendor BAAs signed] ─────────────────────────────┐
                                                  ▼
[CI/CD shipped] ─→ [Repair existing tests] ─→ [Audit remaining 35 routes]
                                                  │
                                                  ▼
[Multi-tenant Twilio] ──→ [Onboard 2nd Twilio account] ──→ [Public launch]
                                                  ▲
[Encryption KMS+rotation] ────────────────────────┤
                                                  │
[Provider abstraction] ──→ [BYO LLM] ──→ [Anthropic/Groq added]
       │
       ▼
[Tool framework] ─→ [Tool sandbox] ─→ [HTTP tools] ─→ [Built-in tools (CRM/transfer/SMS)]

[Web SDK] ──→ [Click-to-call demo] ─→ [Mobile SDKs]
       │
       ▼
[Visual workflow builder] ──→ [Squads]

[Atlas Dedicated migration] ──→ [Customer KMS] ──→ [Enterprise tier ready]

[Status page + on-call runbook + load testing] ──→ [99.9% SLA claimable]

[Stripe metered billing] ──→ [Free tier + spend caps] ──→ [Self-service signup]
```

---

## Section 6 — 90-day roadmap (concrete)

### Days 1–14 — "Plug the bleeding"
1. ✅ (already done today) The 8 audit fixes shipped.
2. Repair the 17 broken livekit/* tests.
3. Ship CI workflows (`.github/workflows/api.yml` + `web.yml`).
4. Strip PII from CloudWatch.
5. Audit remaining 35 routes for IDOR / missing signatures.
6. Add Twilio webhook timestamp window check.
7. Status page wired.
8. On-call runbook drafted.
9. First 10 vitest tests on the dashboard.

### Days 15–45 — "Multi-tenant + observability"
1. Multi-tenant Twilio (auth_token per-assistant lookup).
2. Per-tenant rate limiting.
3. Audit log table + viewer.
4. Encryption KMS + rotation strategy.
5. Function-tool sandboxing groundwork.
6. Latency p50/p95/p99 dashboards.
7. Customer-facing per-call cost panel.
8. Conversation analysis rubrics (one rubric: "success/fail").
9. Migrate Mongo to Atlas Dedicated.
10. Sign vendor BAAs (parallel track).
11. SOC 2 prep starts (policies, vendor register).

### Days 46–90 — "Feature parity sprint"
1. Provider abstraction (STT/LLM/TTS).
2. Add Anthropic + Groq.
3. Web SDK (`@convis-ai/web`).
4. Generic tool framework + built-in tools (transfer, SMS, email, CRM).
5. Squads / multi-agent handoff (foundation).
6. Voice cloning wizard.
7. Server URL pre-call hook.
8. Webhook event stream (customer-facing).
9. Visual workflow builder (reactflow blocks).
10. Public docs portal (Mintlify).
11. Test suite for assistants (regression on prompt changes).
12. Voicemail detection + background audio + filler words.

### Months 4–9 — "Polish & enterprise readiness"
1. iOS + Android + RN SDKs (parallelizable).
2. SOC 2 Type II audit (3-month observation period after policies in place).
3. HIPAA BAAs signed across vendors.
4. Stripe metered billing (full flow: meters → invoices → spend caps → upgrade UI).
5. A/B testing infrastructure.
6. Custom LLM endpoint (BYO).
7. Number marketplace integration.
8. Auto-FAQ generation from KB.
9. Hybrid search + re-ranking on RAG.
10. Multi-region deployment + DR drill.
11. SSO / SAML / SCIM for enterprise.
12. Pen-test by Cure53 / Doyensec.

---

## Section 7 — Honest assessment

- **You will not catch up to Vapi feature-for-feature in 6 months with current team size.** That's fine. Don't try.
- **Pick a wedge and dominate it.** India-first multilingual + self-hostable + n8n/Jira/WhatsApp ecosystem is a viable wedge that Vapi cannot serve cleanly.
- **Get to compliance & multi-tenancy parity first** — those are existential blockers for enterprise deals; SDK breadth and feature breadth are competitive but not existential.
- **Vapi's biggest moats are**: (1) developer experience around the SDKs and docs, (2) the visual workflow builder, (3) the ecosystem of tool integrations. Each takes a quarter of focused engineering to match.
- **Convis's biggest risk is not Vapi**; it's burning runway trying to build platform-breadth before you've proven a single vertical works. Productize one vertical (dental receptionist, real-estate qualifier — pick one) end-to-end before chasing parity.
- **Compliance + observability are non-negotiable for selling to anyone serious.** They're also the dullest work. Schedule them; don't let "just one more cool feature" displace them.
- **If you get one thing right, make it the Web SDK + click-to-call demo.** That's the moment a prospect tries Convis live in their browser and decides whether you exist or not.

---

## Caveats on this document

- Vapi ships fast. Some features I listed as "Vapi has" may have shipped or sunset since the model's training cutoff. **Verify against vapi.ai/docs** before betting strategy on a specific row.
- Convis state reflects the codebase as of the audit at end of 2026-05-06 / start of 2026-05-07 — the just-shipped security fixes, dedupe, recording capture, modal fix, etc. Some features may have been built since.
- Effort estimates assume the current 1–2 engineer team velocity. Adjust for actual team size.
- Tasks are independent unless noted in Section 5; many can be parallelized across multiple developers.
