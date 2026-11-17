import type { CSSProperties } from "react";
import configuration from "./template.json";

type Locale = "en" | "ar";
type LocalizedText = Record<Locale, string>;
interface TemplateConfig {
  schemaVersion: 1;
  brand: {
    name: LocalizedText;
    assistantName: LocalizedText;
    logoUrl: string | null;
    colors: { primary: string; accent: string };
    layout: "split" | "chat-first" | "voice-first";
  };
  locale: { default: Locale; supported: Locale[]; dialingCountryCode: string };
  features: { chat: boolean; voice: boolean; phone: boolean; profiles: boolean; agentConsole: boolean; agentAssist: boolean };
  welcome: { title: LocalizedText; description: LocalizedText };
  quickActions: { id: string; label: LocalizedText; prompt: LocalizedText }[];
}

// Public presentation only; runtime deployment settings remain in the backend.
export const template = configuration as TemplateConfig;
export const locale = template.locale.default;
export const text = (value: LocalizedText) => value[locale];
export const assistTitle = locale === "ar" ? "مساعدة الموظف" : "Agent assist";

function channels(hex: string) {
  const raw = hex.slice(1);
  const expanded = raw.length === 3 ? raw.split("").map((digit) => digit + digit).join("") : raw;
  return [0, 2, 4].map((offset) => parseInt(expanded.slice(offset, offset + 2), 16) / 255);
}

function hsl(hex: string) {
  const [r, g, b] = channels(hex);
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const delta = max - min, lightness = (max + min) / 2;
  const saturation = delta ? delta / (1 - Math.abs(2 * lightness - 1)) : 0;
  const hue = !delta ? 0 : max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
  return `${(hue * 60 + 360) % 360} ${saturation * 100}% ${lightness * 100}%`;
}
const luminance = channels(template.brand.colors.primary).map((channel) =>
  channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
).reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);

export const brandStyles = {
  "--primary": hsl(template.brand.colors.primary),
  "--primary-foreground": luminance > 0.179 ? "0 0% 0%" : "0 0% 100%",
  "--ring": hsl(template.brand.colors.accent),
  "--brand-primary": template.brand.colors.primary,
  "--brand-accent": template.brand.colors.accent,
} as CSSProperties;
