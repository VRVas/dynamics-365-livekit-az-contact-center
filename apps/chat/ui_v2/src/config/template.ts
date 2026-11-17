import type { CSSProperties } from 'react';
import configuration from './template.json';

export type Locale = 'en' | 'ar';
export type LocalizedText = Record<Locale, string>;

export interface TemplateConfig {
  schemaVersion: 1;
  brand: {
    name: LocalizedText;
    assistantName: LocalizedText;
    logoUrl: string | null;
    colors: { primary: string; accent: string };
    layout: 'split' | 'chat-first' | 'voice-first';
  };
  locale: { default: Locale; supported: Locale[]; dialingCountryCode: string };
  features: { chat: boolean; voice: boolean; phone: boolean; profiles: boolean; agentConsole: boolean; agentAssist: boolean };
  welcome: { title: LocalizedText; description: LocalizedText };
  quickActions: { id: string; label: LocalizedText; prompt: LocalizedText }[];
}

// Validated by the deployment preparer before building the private checkout.
export const template = configuration as TemplateConfig;
export const DEFAULT_LOCALE = template.locale.default;
export const isSupportedLocale = (value: unknown): value is Locale =>
  template.locale.supported.some((locale) => locale === value);
const hex = template.brand.colors.primary.slice(1);
const expanded = hex.length === 3 ? hex.split('').map((digit) => digit + digit).join('') : hex;
const luminance = [0, 2, 4].map((offset) => {
  const channel = parseInt(expanded.slice(offset, offset + 2), 16) / 255;
  return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
}).reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);
export const brandStyles = {
  '--brand-primary': template.brand.colors.primary,
  '--brand-accent': template.brand.colors.accent,
  '--brand-on-primary': luminance > 0.179 ? '#000000' : '#ffffff',
} as CSSProperties;
