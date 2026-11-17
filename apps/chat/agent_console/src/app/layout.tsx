import type { Metadata } from 'next';
import { brandStyles, DEFAULT_LOCALE, template } from '@/config/template';
import { LocaleProvider } from '@/i18n/LocaleProvider';
import { translate } from '@/i18n/messages';
import './globals.css';

export const metadata: Metadata = {
  title: `${template.brand.name[DEFAULT_LOCALE]} | ${translate(DEFAULT_LOCALE, 'Agent console')}`,
  description: template.welcome.description[DEFAULT_LOCALE],
  ...(template.brand.logoUrl ? { icons: { icon: template.brand.logoUrl } } : {}),
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang={DEFAULT_LOCALE} dir={DEFAULT_LOCALE === 'ar' ? 'rtl' : 'ltr'} style={brandStyles}>
      <body className="antialiased bg-ink-950 text-white selection:bg-brand-600/40">
        <LocaleProvider>{children}</LocaleProvider>
      </body>
    </html>
  );
}
