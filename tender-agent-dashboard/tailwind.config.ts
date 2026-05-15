// Thin wiring layer between Tailwind and lib/theme.ts.
// Do NOT add hex literals or font names here — everything visual flows from
// lib/theme.ts so a palette swap is one file's worth of changes.

import type { Config } from "tailwindcss";

import {
  animations,
  breakpoints,
  colors,
  fontFamilies,
  radii,
} from "./lib/theme";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx}",
    "./components/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      // `as any` is needed because lib/theme.ts uses `as const` for editor
      // autocomplete, which produces deeply-readonly types that Tailwind's
      // `RecursiveKeyValuePair` declares mutable. Pure type juggling — the
      // values themselves are correct.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      colors: colors as any,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      fontFamily: fontFamilies as any,
      borderRadius: radii,
      screens: breakpoints,
      animation: animations,
      keyframes: {
        pulse: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
        "trail-flow": {
          "0%": { strokeDasharray: "0 2200", strokeDashoffset: "0", opacity: "0" },
          "10%": { opacity: "1" },
          "50%": {
            strokeDasharray: "320 2200",
            strokeDashoffset: "-1600",
            opacity: "1",
          },
          "85%": { opacity: "1" },
          "100%": {
            strokeDasharray: "0 2200",
            strokeDashoffset: "-2200",
            opacity: "0",
          },
        },
        "trail-flow-2": {
          "0%": { strokeDasharray: "0 2400", strokeDashoffset: "0", opacity: "0" },
          "15%": { opacity: "0.8" },
          "50%": {
            strokeDasharray: "280 2400",
            strokeDashoffset: "-1800",
            opacity: "0.8",
          },
          "85%": { opacity: "0.6" },
          "100%": {
            strokeDasharray: "0 2400",
            strokeDashoffset: "-2400",
            opacity: "0",
          },
        },
        "trail-dot": {
          "0%": { offsetDistance: "0%", opacity: "0", r: "2" },
          "10%": { opacity: "1", r: "3" },
          "50%": { offsetDistance: "50%", opacity: "1", r: "3.5" },
          "85%": { opacity: "0.7", r: "2.5" },
          "100%": { offsetDistance: "100%", opacity: "0", r: "2" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
      backdropBlur: {
        xs: "4px",
        sm: "6px",
        md: "8px",
        lg: "10px",
        xl: "20px",
      },
    },
  },
  plugins: [],
};

export default config;
