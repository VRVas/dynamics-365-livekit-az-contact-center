import type { Metadata } from "next";
import { Readex_Pro } from "next/font/google";
import { LocaleProvider } from "@/i18n/LocaleProvider";
import { brandStyles, DEFAULT_LOCALE, template } from "@/config/template";
import "./globals.css";

const readexPro = Readex_Pro({
  subsets: ["latin", "arabic"],
  variable: "--font-readex-pro",
  display: "swap",
});

export const metadata: Metadata = {
  title: `${template.brand.name[DEFAULT_LOCALE]} | ${template.brand.assistantName[DEFAULT_LOCALE]}`,
  description: template.welcome.description[DEFAULT_LOCALE],
  ...(template.brand.logoUrl ? { icons: { icon: template.brand.logoUrl } } : {}),
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang={DEFAULT_LOCALE} dir={DEFAULT_LOCALE === 'ar' ? 'rtl' : 'ltr'} style={brandStyles} className={`${readexPro.variable} dark`}>
      <body className="antialiased text-white min-h-screen">
        <div className="aurora" aria-hidden>
          <span />
        </div>
        <div className="grain" aria-hidden />
        <LocaleProvider><div className="relative z-10">{children}</div></LocaleProvider>
      </body>
    </html>
  );
}
