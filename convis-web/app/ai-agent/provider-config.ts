// Provider configuration — Convis-India stack (full Sarvam, no fallback).
//   ASR:  Sarvam Saaras v3 (mode=transcribe — keeps source language)
//   LLM:  Sarvam-105b (with /nothink injected by backend to disable reasoning)
//   TTS:  Sarvam Bulbul v3 (default) — v2 opt-in for v2-only voices like anushka
//
// Pricing data is approximate and used for the cost calculator only.

export const ENHANCED_TTS_VOICES = {
  sarvam: [
    // ── bulbul:v2 (the safe default model) ──────────────────────────────
    // Female
    { value: 'anushka', label: 'Anushka — Female (bulbul:v2, default)', gender: 'female', accent: 'Indian', model: 'bulbul:v2' },
    { value: 'manisha', label: 'Manisha — Female (bulbul:v2)', gender: 'female', accent: 'Indian', model: 'bulbul:v2' },
    { value: 'vidya', label: 'Vidya — Female (bulbul:v2)', gender: 'female', accent: 'Indian', model: 'bulbul:v2' },
    { value: 'arya', label: 'Arya — Female (bulbul:v2)', gender: 'female', accent: 'Indian', model: 'bulbul:v2' },
    // Male
    { value: 'abhilash', label: 'Abhilash — Male (bulbul:v2)', gender: 'male', accent: 'Indian', model: 'bulbul:v2' },
    { value: 'karun', label: 'Karun — Male (bulbul:v2)', gender: 'male', accent: 'Indian', model: 'bulbul:v2' },
    { value: 'hitesh', label: 'Hitesh — Male (bulbul:v2)', gender: 'male', accent: 'Indian', model: 'bulbul:v2' },

    // ── bulbul:v3 / v3-beta (opt-in only — server rejects plugin defaults) ──
    // Customer-care + conversational voices unique to v3.
    { value: 'shubh', label: 'Shubh — Male (bulbul:v3, customer care)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'aditya', label: 'Aditya — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'rahul', label: 'Rahul — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'rohan', label: 'Rohan — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'amit', label: 'Amit — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'dev', label: 'Dev — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'ratan', label: 'Ratan — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'varun', label: 'Varun — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'manan', label: 'Manan — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'sumit', label: 'Sumit — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'kabir', label: 'Kabir — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'aayan', label: 'Aayan — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'ashutosh', label: 'Ashutosh — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'advait', label: 'Advait — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'anand', label: 'Anand — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'tarun', label: 'Tarun — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'sunny', label: 'Sunny — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'mani', label: 'Mani — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'gokul', label: 'Gokul — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'vijay', label: 'Vijay — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'mohit', label: 'Mohit — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'rehan', label: 'Rehan — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'soham', label: 'Soham — Male (bulbul:v3)', gender: 'male', accent: 'Indian', model: 'bulbul:v3' },
    // Female (v3)
    { value: 'ritu', label: 'Ritu — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'priya', label: 'Priya — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'neha', label: 'Neha — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'pooja', label: 'Pooja — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'simran', label: 'Simran — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'kavya', label: 'Kavya — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'ishita', label: 'Ishita — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'shreya', label: 'Shreya — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'roopa', label: 'Roopa — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'tanya', label: 'Tanya — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'shruti', label: 'Shruti — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'suhani', label: 'Suhani — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'kavitha', label: 'Kavitha — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
    { value: 'rupali', label: 'Rupali — Female (bulbul:v3)', gender: 'female', accent: 'Indian', model: 'bulbul:v3' },
  ],
};

// ASR models — Sarvam (ASR migration 2026-05-28). saaras:v3 is the flagship
// streaming Indic ASR — 22 languages, native code-switching, mode=transcribe
// keeps source language. saarika:v2.5 covers 11 languages and is slightly
// cheaper. saaras:v2.5 is translate-only (forces English output) — listed for
// explicit use cases like "Hindi audio → English transcript for reporting"
// but NOT recommended for voice agents (LLM/TTS need source-language input).
export const ENHANCED_ASR_MODELS = {
  sarvam: [
    { value: 'saaras:v3', label: 'Saaras v3 — Default (22 langs, transcribe, streaming)', cost: 0.005, latency: 400, costPerMin: 0.005 },
    { value: 'saarika:v2.5', label: 'Saarika v2.5 (11 langs, cheaper, streaming)', cost: 0.003, latency: 350, costPerMin: 0.003 },
    { value: 'saaras:v2.5', label: 'Saaras v2.5 — Translate-only (output in English, not for voice agents)', cost: 0.005, latency: 400, costPerMin: 0.005 },
  ],
};

// Sarvam ASR languages (BCP-47 India-locale + auto-detect).
export const SARVAM_ASR_LANGUAGES = [
  { value: 'en-IN', label: 'English (India) — default' },
  { value: 'unknown', label: 'Auto-detect (multilingual / code-switching)' },
  { value: 'hi-IN', label: 'Hindi' },
  { value: 'bn-IN', label: 'Bengali' },
  { value: 'gu-IN', label: 'Gujarati' },
  { value: 'kn-IN', label: 'Kannada' },
  { value: 'ml-IN', label: 'Malayalam' },
  { value: 'mr-IN', label: 'Marathi' },
  { value: 'od-IN', label: 'Odia' },
  { value: 'pa-IN', label: 'Punjabi' },
  { value: 'ta-IN', label: 'Tamil' },
  { value: 'te-IN', label: 'Telugu' },
  // saaras:v3-only (extended Indic set)
  { value: 'as-IN', label: 'Assamese (saaras:v3 only)' },
  { value: 'ur-IN', label: 'Urdu (saaras:v3 only)' },
  { value: 'ne-IN', label: 'Nepali (saaras:v3 only)' },
];

// TTS models — Sarvam Bulbul. v3 is the default (streaming-capable, 30 voices,
// flagship). v2 stays available for assistants that need v2-only voices
// (anushka, manisha, vidya, arya, abhilash, karun, hitesh) — those voices do
// NOT work on v3, the backend coercion downgrades the speaker to v2's default
// "anushka" if the model is forced back to v2.
export const ENHANCED_TTS_MODELS = {
  sarvam: [
    { value: 'bulbul:v3', label: 'Bulbul v3 — Default (30 voices, streaming, customer-care + conversational)', cost: 0.15, latency: 250, costPerChar: 0.00015 },
    { value: 'bulbul:v3-beta', label: 'Bulbul v3-beta (25 voices)', cost: 0.15, latency: 250, costPerChar: 0.00015 },
    { value: 'bulbul:v2', label: 'Bulbul v2 — Legacy (7 voices incl. anushka — opt-in if you need those voices)', cost: 0.12, latency: 200, costPerChar: 0.00012 },
  ],
};

// Supported TTS languages (BCP-47, India-locale). Bulbul's 11-language set.
export const SARVAM_TTS_LANGUAGES = [
  { value: 'en-IN', label: 'English (India) — default' },
  { value: 'hi-IN', label: 'Hindi' },
  { value: 'bn-IN', label: 'Bengali' },
  { value: 'gu-IN', label: 'Gujarati' },
  { value: 'kn-IN', label: 'Kannada' },
  { value: 'ml-IN', label: 'Malayalam' },
  { value: 'mr-IN', label: 'Marathi' },
  { value: 'od-IN', label: 'Odia' },
  { value: 'pa-IN', label: 'Punjabi' },
  { value: 'ta-IN', label: 'Tamil' },
  { value: 'te-IN', label: 'Telugu' },
];

// LLM models — Sarvam (LLM migration 2026-05-23). sarvam-105b is the flagship
// (105B-param Indic-tuned MoE); sarvam-m is the lighter/faster alternative.
// Cost figures are approximate and used by the cost calculator only.
//
// IMPORTANT: sarvam-105b runs in "thinking" mode by default. The backend
// injects "/nothink" at the start of every system prompt to disable this and
// keep TTFT under ~3s. Do NOT remove /nothink without re-benchmarking.
export const ENHANCED_LLM_MODELS = {
  sarvam: [
    { value: 'sarvam-105b', label: 'Sarvam-105b — Flagship (default, Indic-tuned, /nothink mode)', costInput: 0.30, costOutput: 1.20, latency: 700, cost: '0.0008', speed: 'Fast' },
    { value: 'sarvam-m', label: 'Sarvam-M — Lighter & Faster (24B, lower cost)', costInput: 0.10, costOutput: 0.40, latency: 400, cost: '0.0003', speed: 'Very Fast' },
    { value: 'sarvam-30b', label: 'Sarvam-30b — Mid-tier', costInput: 0.15, costOutput: 0.60, latency: 500, cost: '0.0004', speed: 'Fast' },
  ],
};

// Twilio Cost (for outbound SIP telephony through LiveKit). Pre-Vobiz migration.
export const TWILIO_COST_PER_MIN = {
  usd: 0.014,
  inr: 5.5,
};

// Static voice list for Sarvam — no per-user account sync needed (the speaker
// set is fixed across all Sarvam API keys, unlike ElevenLabs' per-account voice
// libraries). Kept as an async function with the same signature as the old
// ElevenLabs fetcher so the assistant-edit form's calling code doesn't need
// changes.
export async function getTTSVoices(
  _userId?: string,
  _syncRemote: boolean = false
): Promise<typeof ENHANCED_TTS_VOICES> {
  return ENHANCED_TTS_VOICES;
}
