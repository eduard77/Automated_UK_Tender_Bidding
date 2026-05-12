// Thin wiring layer between Tailwind and lib/theme.ts.
//
// Do NOT add hex literals or font names here — everything visual flows from
// lib/theme.ts so a palette swap is one file's worth of changes.

import type { Config } from "tailwindcss";

import {
  breakpoints,
  colors,
  fontFamilies,
  radii,
  spacing,
} from "./lib/theme";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx}",
    "./components/**/*.{js,ts,jsx,tsx}",
  ],
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
      spacing,
      borderRadius: radii,
      screens: breakpoints,
    },
  },
  plugins: [],
};

export default config;
