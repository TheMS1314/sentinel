/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["selector", '[data-theme="dark"]'],
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: "rgb(var(--color-bg) / <alpha-value>)",
        surface: "rgb(var(--color-surface) / <alpha-value>)",
        surface2: "rgb(var(--color-surface2) / <alpha-value>)",
        surface3: "rgb(var(--color-surface3) / <alpha-value>)",
        border: "rgb(var(--color-border) / <alpha-value>)",
        borderlight: "rgb(var(--color-borderlight) / <alpha-value>)",
        text: "rgb(var(--color-text) / <alpha-value>)",
        textmuted: "rgb(var(--color-textmuted) / <alpha-value>)",
        textfaint: "rgb(var(--color-textfaint) / <alpha-value>)",
        approve: "rgb(var(--color-approve) / <alpha-value>)",
        hold: "rgb(var(--color-hold) / <alpha-value>)",
        block: "rgb(var(--color-block) / <alpha-value>)",
        accent: "rgb(var(--color-accent) / <alpha-value>)",
      },
      fontFamily: {
        sans: ["IBM Plex Sans", "system-ui", "sans-serif"],
        mono: ["IBM Plex Mono", "monospace"],
      },
      boxShadow: {
        card: "var(--shadow-card)",
        raised: "var(--shadow-raised)",
      },
      keyframes: {
        slidein: {
          "0%": { opacity: "0", transform: "translateY(-6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        pulsedot: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
      },
      animation: {
        slidein: "slidein 0.3s cubic-bezier(0.16, 1, 0.3, 1)",
        pulsedot: "pulsedot 2s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
