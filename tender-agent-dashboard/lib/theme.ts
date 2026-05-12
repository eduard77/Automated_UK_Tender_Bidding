// ============================================================================
// Design tokens — single source of truth for the dashboard's visual language.
// ============================================================================
//
// **To restyle the dashboard to match a parent site, edit this file.**
// Everything else flows from these tokens:
//
//   - tailwind.config.ts imports `colors` + `fontFamilies` + `spacing` +
//     `radii` + `breakpoints` and feeds them into the Tailwind theme. All
//     `bg-*` / `text-*` / `border-*` / `font-*` utilities resolve through
//     here.
//   - app/globals.css applies the same colours via `@apply` and the Tailwind
//     `theme()` function — no hex literals for brand colours.
//   - app/layout.tsx imports `brand.themeColor` for the PWA viewport meta tag.
//
// Two places intentionally NOT driven by this file because they're static
// JSON / CSS-import contexts (they can't `import` from TypeScript):
//
//   1. public/manifest.json — `theme_color` and `background_color`. Edit
//      manually to match `brand.themeColor` below.
//   2. app/globals.css `@import url(...)` of Google Fonts. Edit `fontImportUrl`
//      below AND the matching `@import` line in globals.css.
//
// The list above is the COMPLETE swap surface. Anything else changing under
// the same palette is a bug.

// ----------------------------------------------------------------------------
// Colours
// ----------------------------------------------------------------------------

export const colors = {
  // Page background ("ink" because dark editorial press).
  ink: {
    DEFAULT: "#0E1116",
    soft: "#1B1F26",
  },
  // Default foreground.
  bone: "#F5F1E8",
  // Destructive / error.
  oxblood: "#7A1F1F",
  // Reserve for future positive-state surfaces (not currently used).
  moss: "#3F4D2C",
  // Positive / "on" state (push bell when subscribed, status badges).
  sage: "#A6B596",
  // Primary accent / call-to-action.
  rust: "#B5471A",
} as const;

// ----------------------------------------------------------------------------
// Fonts
// ----------------------------------------------------------------------------
//
// First entry of each stack is the "designer" font (Google Fonts); the rest
// are sensible fallbacks for when the network drops the request.

export const fontFamilies = {
  display: ["Fraunces", "Georgia", "serif"],
  body: ["Inter", "system-ui", "sans-serif"],
  mono: ["JetBrains Mono", "ui-monospace", "monospace"],
} as const;

// Google Fonts URL imported by app/globals.css. CSS @import can't reference
// TypeScript — when the font set changes, edit BOTH this string and the
// @import line in globals.css. They're documented together at the top of
// globals.css.
export const fontImportUrl =
  "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,700;9..144,900&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap";

// ----------------------------------------------------------------------------
// Spacing, radii, breakpoints
// ----------------------------------------------------------------------------
//
// All three currently use Tailwind's defaults — nothing custom yet. The slots
// are reserved so a palette swap can introduce its own scale without hunting
// for the right hook.

export const spacing = {
  // e.g. "page-gutter": "5rem",
} as const;

export const radii = {
  // e.g. "card": "0.25rem",
} as const;

export const breakpoints = {
  // e.g. xl: "1280px",
} as const;

// ----------------------------------------------------------------------------
// Brand-level constants used outside Tailwind (PWA meta, OG cards, etc.).
// ----------------------------------------------------------------------------

export const brand = {
  // Viewport theme-colour (app/layout.tsx) + PWA theme/background colour
  // (public/manifest.json). The manifest copy is hardcoded; edit manually
  // when this changes.
  themeColor: colors.ink.DEFAULT,
  backgroundColor: colors.ink.DEFAULT,
} as const;

// Type re-exports for callers that want strict palette typing.
export type ColorName = keyof typeof colors;
export type FontFamilyName = keyof typeof fontFamilies;
