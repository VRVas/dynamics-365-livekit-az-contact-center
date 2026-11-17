import type { Metadata } from "next";
import { Readex_Pro } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "@/components/theme-provider";
import { assistTitle, brandStyles, locale, template, text } from "@/config/template";

const readexPro = Readex_Pro({
  subsets: ["latin", "arabic"],
  variable: "--font-readex-pro",
  display: "swap",
});

export const metadata: Metadata = {
  title: `${text(template.brand.name)} | ${assistTitle}`,
  description: text(template.welcome.description),
  ...(template.brand.logoUrl ? { icons: { icon: template.brand.logoUrl } } : {}),
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang={locale} dir={locale === "ar" ? "rtl" : "ltr"} style={brandStyles} className={readexPro.variable} suppressHydrationWarning>
      <body className="font-sans antialiased">
        <ThemeProvider
          attribute="class"
          defaultTheme="dark"
          enableSystem
          disableTransitionOnChange
        >
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
