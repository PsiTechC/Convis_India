// Provider configuration — LiveKit stack only:
//   ASR:  Deepgram
//   LLM:  OpenAI
//   TTS:  ElevenLabs
// All pricing data accurate as of January 2025.

export const ENHANCED_TTS_VOICES = {
  elevenlabs: [
    // Female voices - American
    { value: 'EXAVITQu4vr4xnSDxMaL', label: 'Sarah - American Female (Young)', gender: 'female', accent: 'American' },
    { value: 'FGY2WhTYpPnrIDTdsKH5', label: 'Laura - American Female (Young)', gender: 'female', accent: 'American' },
    { value: 'cgSgspJ2msm6clMCkdW9', label: 'Jessica - American Female (Young)', gender: 'female', accent: 'American' },
    { value: 'XrExE9yKIg1WjnnlVkGX', label: 'Matilda - American Female (Middle-aged)', gender: 'female', accent: 'American' },
    { value: 'pFZP5JQG7iQjIQuC4Bku', label: 'Lily - Female (Middle-aged)', gender: 'female', accent: 'American' },
    // Female voices - British
    { value: 'Xb7hH8MSUJpSbSDYk0k2', label: 'Alice - British Female (Middle-aged)', gender: 'female', accent: 'British' },
    { value: 'FrzKLwOr0y3qieiphjs2', label: 'Paula - British Female (Young)', gender: 'female', accent: 'British' },
    // Male voices - American
    { value: '2EiwWnXFnvU5JabPnv8n', label: 'Clyde - American Male (Middle-aged)', gender: 'male', accent: 'American' },
    { value: 'CwhRBWXzGAHq8TQ4Fs17', label: 'Roger - American Male (Middle-aged)', gender: 'male', accent: 'American' },
    { value: 'TX3LPaxmHKxFdv7VOQHJ', label: 'Liam - American Male (Young)', gender: 'male', accent: 'American' },
    { value: 'SOYHLrjzK2X1ezoPC6cr', label: 'Harry - American Male (Young)', gender: 'male', accent: 'American' },
    { value: 'bIHbv24MWmeRgasZH58o', label: 'Will - American Male (Young)', gender: 'male', accent: 'American' },
    { value: 'cjVigY5qzO86Huf0OWal', label: 'Eric - American Male (Middle-aged)', gender: 'male', accent: 'American' },
    { value: 'iP95p4xoKVk53GoZ742B', label: 'Chris - American Male (Middle-aged)', gender: 'male', accent: 'American' },
    { value: 'nPczCjzI2devNBz1zQrb', label: 'Brian - American Male (Middle-aged)', gender: 'male', accent: 'American' },
    { value: 'pqHfZKP75CvOlQylNhV4', label: 'Bill - American Male (Old)', gender: 'male', accent: 'American' },
    // Male voices - British
    { value: 'JBFqnCBsd6RMkjVDRZzb', label: 'George - British Male (Middle-aged)', gender: 'male', accent: 'British' },
    { value: 'onwK4e9ZLuTAKqWW03F9', label: 'Daniel - British Male (Middle-aged)', gender: 'male', accent: 'British' },
    { value: 'N2lVS1w4EtoT3dr4eOWO', label: 'Callum - Male (Middle-aged)', gender: 'male', accent: 'British' },
    // Male voices - Other
    { value: 'IKne3meq5aSn9XLyUdCD', label: 'Charlie - Australian Male (Young)', gender: 'male', accent: 'Australian' },
    // Neutral voices
    { value: 'SAz9YHcvj6GT2YYXdXww', label: 'River - American Neutral (Middle-aged)', gender: 'neutral', accent: 'American' },
    // Indian Hindi voices
    { value: 'broqrJkktxd1CclKTudW', label: 'Anika - Hindi Customer Care Agent (Female)', gender: 'female', accent: 'Indian' },
    { value: 'ni6cdqyS9wBvic5LPA7M', label: 'Tara - Hindi Conversational (Female)', gender: 'female', accent: 'Indian' },
    { value: 'SZfY4K69FwXus87eayHK', label: 'Nikita - Hindi Youthful (Female)', gender: 'female', accent: 'Indian' },
    { value: '1qEiC6qsybMkmnNdVMbK', label: 'Monika - Hindi Modulated (Female)', gender: 'female', accent: 'Indian' },
    { value: 'KSsyodh37PbfWy29kPtx', label: 'Kishan - Hindi Narrator (Male)', gender: 'male', accent: 'Indian' },
    { value: '6MoEUz34rbRrmmyxgRm4', label: 'Manav - Hindi Conversational (Male)', gender: 'male', accent: 'Indian' },
  ],
};

// ASR models — Deepgram only
export const ENHANCED_ASR_MODELS = {
  deepgram: [
    { value: 'nova-2', label: 'Nova-2 (Latest, Most Accurate)', cost: 0.0043, latency: 75, costPerMin: 0.0043 },
    { value: 'nova-3', label: 'Nova-3 (Beta, Improved)', cost: 0.0059, latency: 80, costPerMin: 0.0059 },
  ],
};

// TTS models — ElevenLabs only
export const ENHANCED_TTS_MODELS = {
  elevenlabs: [
    // Flash Models - Ultra Low Latency (~75ms) - Best for Real-time/Voice Agents
    { value: 'eleven_flash_v2_5', label: 'Flash V2.5 - Ultra Fast (~75ms, 32 Languages)', cost: 0.09, latency: 75, costPerChar: 0.00009 },
    { value: 'eleven_flash_v2', label: 'Flash V2 - Ultra Fast (~75ms, English Only)', cost: 0.09, latency: 75, costPerChar: 0.00009 },
    // Turbo Models - Low Latency with Better Quality
    { value: 'eleven_turbo_v2_5', label: 'Turbo V2.5 - Fast & High Quality (32 Languages)', cost: 0.09, latency: 130, costPerChar: 0.00009 },
    { value: 'eleven_turbo_v2', label: 'Turbo V2 - Fast & High Quality (English Only)', cost: 0.09, latency: 150, costPerChar: 0.00009 },
    // Standard Models - Best Quality
    { value: 'eleven_multilingual_v2', label: 'Multilingual V2 - Best Quality (29 Languages)', cost: 0.18, latency: 180, costPerChar: 0.00018 },
    // Eleven V3 - Most Expressive
    { value: 'eleven_v3', label: 'Eleven V3 - Most Expressive (70+ Languages, Higher Latency)', cost: 0.18, latency: 300, costPerChar: 0.00018 },
  ],
};

// LLM models — OpenAI only
export const ENHANCED_LLM_MODELS = {
  openai: [
    { value: 'gpt-4o-mini', label: 'GPT-4O Mini - Cheapest & Fastest ($0.15 in / $0.60 out per 1M tokens)', costInput: 0.15, costOutput: 0.60, latency: 400, cost: '0.000375', speed: 'Fastest' },
    { value: 'gpt-3.5-turbo', label: 'GPT-3.5 Turbo - Very Fast ($0.50 in / $1.50 out per 1M tokens)', costInput: 0.50, costOutput: 1.50, latency: 300, cost: '0.001', speed: 'Very Fast' },
    { value: 'gpt-4o', label: 'GPT-4O - Balanced Performance ($5.00 in / $20.00 out per 1M tokens)', costInput: 5.00, costOutput: 20.00, latency: 800, cost: '0.0125', speed: 'Fast' },
    { value: 'gpt-4-turbo', label: 'GPT-4 Turbo - High Quality ($10.00 in / $30.00 out per 1M tokens)', costInput: 10.00, costOutput: 30.00, latency: 1000, cost: '0.02', speed: 'Moderate' },
    { value: 'o1-mini', label: 'O1 Mini - Advanced Reasoning ($3.00 in / $12.00 out per 1M tokens)', costInput: 3.00, costOutput: 12.00, latency: 1200, cost: '0.0075', speed: 'Advanced Reasoning' },
  ],
};

// Twilio Cost (for outbound SIP telephony through LiveKit)
export const TWILIO_COST_PER_MIN = {
  usd: 0.014,
  inr: 5.5,
};

// Fetch ElevenLabs voices dynamically from the user's account.
export async function fetchElevenLabsVoices(
  userId: string
): Promise<Array<{ value: string; label: string; gender: string; accent: string }>> {
  try {
    const response = await fetch(
      `${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/api/voices/elevenlabs/sync?user_id=${userId}`
    );
    if (!response.ok) {
      console.warn('Failed to fetch ElevenLabs voices, using defaults');
      return ENHANCED_TTS_VOICES.elevenlabs;
    }
    const data = await response.json();
    if (data.success && data.voices) {
      return data.voices.map(
        (voice: { id: string; name: string; accent: string; gender: string; age_group?: string }) => ({
          value: voice.id,
          label: `${voice.name} - ${voice.accent} ${voice.gender.charAt(0).toUpperCase() + voice.gender.slice(1)}${
            voice.age_group ? ` (${voice.age_group})` : ''
          }`,
          gender: voice.gender,
          accent: voice.accent,
        })
      );
    }
    return ENHANCED_TTS_VOICES.elevenlabs;
  } catch (error) {
    console.error('Error fetching ElevenLabs voices:', error);
    return ENHANCED_TTS_VOICES.elevenlabs;
  }
}

// Get all TTS voices, optionally with a live ElevenLabs sync.
export async function getTTSVoices(
  userId?: string,
  syncElevenLabs: boolean = false
): Promise<typeof ENHANCED_TTS_VOICES> {
  if (!syncElevenLabs || !userId) return ENHANCED_TTS_VOICES;
  const live = await fetchElevenLabsVoices(userId);
  return { ...ENHANCED_TTS_VOICES, elevenlabs: live };
}
