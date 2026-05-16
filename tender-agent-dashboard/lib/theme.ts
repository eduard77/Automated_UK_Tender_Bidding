// ============================================================================
// Design tokens — single source of truth for the dashboard's visual language.
// Elevated Genera theme — dark canvas, mint accent, Fraunces display.
// ============================================================================
//
// Mirrors C:\Code\PublicTender\docs\mockups\genera-tenders-mockup-v2.html.
// Editing this file restyles the dashboard. The complete swap surface:
//
//   - tailwind.config.ts imports `colors` / `fontFamilies` / `radii` /
//     `breakpoints` and threads them into Tailwind's theme.
//   - app/globals.css references the same tokens via `theme()`.
//   - app/layout.tsx imports `brand.themeColor` for the viewport meta tag.
//   - public/manifest.json mirrors `brand.themeColor` + `brand.backgroundColor`
//     as static JSON (edit manually when this file changes).
//   - app/globals.css's Google Fonts `@import` mirrors `fontImportUrl`.

// ----------------------------------------------------------------------------
// Colours
// ----------------------------------------------------------------------------

export const colors = {
  // Page background — the deep navy canvas.
  bg: {
    DEFAULT: "#07111a",
    elevated: "rgba(17, 23, 32, 0.78)",
    "elevated-strong": "rgba(17, 23, 32, 0.92)",
    "elevated-priority": "rgba(28, 22, 16, 0.7)",
  },
  // Primary accent — the mint green that says "genera".
  mint: {
    DEFAULT: "#39d86f",
    pale: "#9ff2b2",
  },
  // Decorative blue light-trail tokens (used by AtmosphereBackground only).
  "blue-trace": {
    DEFAULT: "rgba(110, 180, 255, 0.5)",
    bright: "rgba(160, 210, 255, 0.9)",
  },
  // Foreground scale — white at three alphas.
  text: {
    DEFAULT: "rgba(255, 255, 255, 0.94)",
    muted: "rgba(255, 255, 255, 0.62)",
    dim: "rgba(255, 255, 255, 0.42)",
  },
  // Borders — white at two alphas.
  border: {
    DEFAULT: "rgba(255, 255, 255, 0.08)",
    strong: "rgba(255, 255, 255, 0.16)",
  },
  // Semantic.
  amber: "#f5a623",
  // Legacy state tokens kept so existing pre-redesign references compile;
  // every component below is restyled, but these are convenient semantic
  // labels for the few places error/danger needs a name.
  danger: "#f87171",
  success: "#39d86f",
} as const;

// ----------------------------------------------------------------------------
// Fonts
// ----------------------------------------------------------------------------

export const fontFamilies = {
  display: ["Fraunces", "Georgia", "serif"],
  body: ["Inter", "-apple-system", "system-ui", "sans-serif"],
  mono: ["JetBrains Mono", "SF Mono", "Consolas", "ui-monospace", "monospace"],
} as const;

// Mirror of the Google Fonts URL imported by app/globals.css. CSS @import
// can't reference TypeScript — edit BOTH when the font set changes.
export const fontImportUrl =
  "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,500;9..144,600&family=Inter:wght@400;450;500;600&family=JetBrains+Mono:wght@400;500&display=swap";

// ----------------------------------------------------------------------------
// Spacing, radii, breakpoints
// ----------------------------------------------------------------------------

export const spacing = {
  // Use Tailwind defaults; bespoke "page-gutter" extracted from the mockup.
  "page-gutter": "48px",
  "page-gutter-md": "24px",
} as const;

export const radii = {
  xs: "3px",
  sm: "5px",
  md: "8px",
  lg: "12px",
  xl: "18px",
  "2xl": "24px",
  pill: "100px",
} as const;

export const breakpoints = {
  sm: "380px",
  md: "760px",
  lg: "1100px",
  xl: "1440px",
  "2xl": "1920px",
} as const;

// ----------------------------------------------------------------------------
// Animation tokens (CSS keyframes themselves live in app/globals.css).
// ----------------------------------------------------------------------------

export const animations = {
  pulse: "pulse 2.4s ease-in-out infinite",
  "trail-flow": "trail-flow 14s ease-in-out infinite",
  "trail-flow-2": "trail-flow-2 18s ease-in-out infinite 3s",
  "trail-dot": "trail-dot 14s ease-in-out infinite",
  shimmer: "shimmer 1.6s linear infinite",
} as const;

// ----------------------------------------------------------------------------
// Brand-level constants used outside Tailwind (PWA meta, OG cards, etc.).
// ----------------------------------------------------------------------------

export const brand = {
  themeColor: colors.bg.DEFAULT,
  backgroundColor: colors.bg.DEFAULT,
  wordmark: {
    name: "genera",
    slash: "/",
    product: "tenders",
  },
} as const;

// Type re-exports for callers that want strict palette typing.
export type ColorName = keyof typeof colors;
export type FontFamilyName = keyof typeof fontFamilies;
