export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, '') || '';
export const VOICE_BASE_URL = process.env.NEXT_PUBLIC_VOICE_URL?.replace(/\/$/, '') || '';
export const VOICES = ['mul_nourah', 'mul_abdullah', 'mul_sara', 'mul_abdulaziz', 'mul_fadi', 'mul_yassin', 'mul_youssef'] as const;
