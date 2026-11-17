/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          200: 'rgb(var(--brand-accent-rgb) / <alpha-value>)',
          300: 'rgb(var(--brand-accent-rgb) / <alpha-value>)',
          400: 'rgb(var(--brand-accent-rgb) / <alpha-value>)',
          500: 'rgb(var(--brand-primary-rgb) / <alpha-value>)',
          600: 'rgb(var(--brand-primary-rgb) / <alpha-value>)',
          700: 'rgb(var(--brand-primary-rgb) / <alpha-value>)',
          950: 'rgb(var(--brand-primary-rgb) / <alpha-value>)',
        },
        // Charcoal command-center surfaces
        ink: {
          950: '#070709', 900: '#0c0c10', 850: '#101015', 800: '#15151c',
          750: '#1a1a22', 700: '#212129', 600: '#2c2c37', 500: '#3b3b48',
          400: '#5a5a6b',
        },
      },
      boxShadow: {
        'glow-brand': '0 0 22px rgb(var(--brand-primary-rgb) / 0.45), 0 0 50px rgb(var(--brand-primary-rgb) / 0.18)',
        'glow-brand-sm': '0 0 12px rgb(var(--brand-primary-rgb) / 0.45)',
        'glow-green': '0 0 14px rgba(34,197,94,0.55)',
        panel: '0 10px 40px -12px rgba(0,0,0,0.7)',
      },
      keyframes: {
        'pulse-glow': {
          '0%,100%': { boxShadow: '0 0 0 0 rgb(var(--brand-primary-rgb) / 0.5)' },
          '50%': { boxShadow: '0 0 0 10px rgb(var(--brand-primary-rgb) / 0)' },
        },
        'slide-up': {
          '0%': { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        'ring-expand': {
          '0%': { transform: 'scale(0.8)', opacity: '0.7' },
          '100%': { transform: 'scale(2.2)', opacity: '0' },
        },
      },
      animation: {
        'pulse-glow': 'pulse-glow 2s infinite',
        'slide-up': 'slide-up 0.35s ease-out both',
        shimmer: 'shimmer 2.5s linear infinite',
        'ring-expand': 'ring-expand 1.8s ease-out infinite',
      },
    },
  },
  plugins: [],
};
