# Genera Systems — Design System Reference

> Extracted verbatim from the marketing site at `C:\Code\GeneraSystems` for the
> purpose of porting the visual language to a separate Next.js 15 + Tailwind 4
> dashboard. Where this doc says a value is a hex or px, it is the literal string
> found in source; nothing here is paraphrased.

---

## Source stack (orientation)

- **Framework**: ASP.NET Core 8.0 Razor Pages (`Microsoft.NET.Sdk.Web`, `net8.0`).
  See `C:\Code\GeneraSystems\GeneraSystems.csproj` and
  `C:\Code\GeneraSystems\Program.cs`.
- **Routing**: Razor Pages in `C:\Code\GeneraSystems\Pages\*.cshtml` mounted at
  `/`, `/About`, `/Blog`, `/Consulting`, `/Contact`, `/Cookies`, `/DemoLab`,
  `/Genera-CIMS`, `/Index`, `/Privacy`, `/Technology`, `/Terms`. Shared layout
  at `Pages\Shared\_Layout.cshtml`.
- **Styling system**:
  - **Bootstrap 5** loaded as a precompiled stylesheet at
    `~/lib/bootstrap/dist/css/bootstrap.min.css` (provides container, grid,
    `.row`, `.col-md-*`, `.col-lg-*`, `.g-4`, `.h-100`, `.mt-*`, `.mb-*`, `.py-*`
    spacing utilities, navbar JS, etc.).
  - **Custom plain CSS** in a single file `~/css/site.css` (~1147 lines). All
    visual identity lives there. No CSS variables — colours are hard-coded hex.
    No Tailwind, no SCSS, no PostCSS, no design tokens file.
  - **ASP.NET scoped-style file** `~/GeneraSystems.styles.css` is linked but
    empty/auto-generated at build.
- **JS**: jQuery + Bootstrap bundle + a near-empty `~/js/site.js`. No build
  pipeline.
- **Concept-preview demo** at `wwwroot\demos\cims-filename-validation.html` is a
  **separate, lighter palette** built with **Tailwind CDN** and CSS variables
  (`--navy`, `--steel`, `--amber`, etc.). See §8 — *use this only as inspiration
  for in-app dashboard chrome, not for the marketing surface*.

> **Heads up**: the layout references `~/images/logo.png` and
> `~/images/favicon.png` but neither file is present in `wwwroot/Images/`. Only
> the hero `.webp` images and the root `favicon.ico` ship. The port will need a
> logo asset created or sourced.

---

## 0. Genera signature — the three must-keep moves

1. **Deep-navy gradient with a faint dot-grid + green glow corner.** Body background
   is `#07111a` layered with two 1px white-alpha grid lines (40px × 40px), a
   green radial glow top-left at `rgba(57,216,111,0.12)`, and a 135° navy
   gradient. This is the canvas every page sits on.
2. **Pill kicker → giant tight-tracked display title → green primary CTA.** Every
   page hero uses an uppercase, letter-spaced `.hero-kicker` pill in mint-green
   on dark, then a `clamp(3rem, 5vw, 5.2rem)` headline at `font-weight: 800`,
   `letter-spacing: -0.03em`, line-height `0.98`, followed by a `#39d86f` "engage"
   button next to a translucent-white-outline secondary.
3. **Translucent dark cards on the dark canvas.** `.feature-card` uses
   `rgba(17,23,32,0.78)`, hairline white-alpha border, `border-radius: 16px`, and
   a soft `0 12px 30px rgba(0,0,0,0.22)` lift. Cards nest inside cards (a
   wrapping `feature-card` containing a grid of inner `feature-card h-100`s).
   This nested-card rhythm is the dominant content idiom.

---

## 1. Brand fundamentals

### Wordmark and logo

- Logo image: `~/images/logo.png` (referenced in
  `Pages\Shared\_Layout.cshtml:71`). **Asset not present in repo** — must be
  recreated for the port.
- Favicon (PNG): `~/images/favicon.png` (referenced in
  `_Layout.cshtml:18`). Asset not present in repo.
- Favicon (ICO, actually shipped): `C:\Code\GeneraSystems\wwwroot\favicon.ico`.
- Logo presentation in navbar:
  ```html
  <a class="navbar-brand" asp-area="" asp-page="/Index">
      <img src="~/images/logo.png" alt="Genera Systems Logo" />
      <span>Genera Systems</span>
  </a>
  ```
  - Mark on the left, wordmark on the right.
  - `.navbar-brand img { height: 40px; width: auto; }`
  - `.navbar-brand { gap: 10px; font-weight: 700; color: #ffffff !important; }`
  - Wordmark is just bold sans set in `Arial` against the dark navbar.

### Wordmark treatment

- Title case: **"Genera Systems"** (never all-caps in the wordmark itself).
- All-caps is reserved for the `.hero-kicker` pill and small uppercase labels
  (`.demo-stat-label`, `.formula-title`).
- `<title>` template (from `_Layout.cshtml`): `"@pageTitle | Genera Systems"`.

### Tone keywords (extracted from copy)

- *Governance*, *assurance*, *compliance*, *evidence trail*, *enforced*, *auditable*,
  *single source of truth*, *built in / not bolted on*, *practitioner*, *codified*.
- Voice in body copy: confident, declarative, slightly polemical
  (e.g. "Governance built into the build.", "No marketing fluff — the same
  rigour that goes into the software.").
- Brand schema (`_Layout.cshtml:38–50`) self-describes as "UK construction
  assurance platform and consulting practice".

---

## 2. Colour system

All colours are **literal hex / rgba values pulled from
`wwwroot/css/site.css`**. No design tokens / no CSS variables on the marketing
site. Group below is editorial — the source defines none.

### Brand / accent (primary)

| Role | Value | Used for |
|---|---|---|
| Genera green (signature) | `#39d86f` | `.btn-hero-primary` background, navbar link hover, `.cookie-buttons button:first-child`, `.pipeline-number` (at 14% alpha) |
| Genera green — hover | `#55e583` | `.btn-hero-primary:hover` |
| Genera green — soft fill | `rgba(57,216,111,0.14)` | `.pipeline-number` background |
| Genera green — alpha tints | `rgba(57,216,111,0.12)` / `0.10)` / `0.08)` / `0.22)` / `0.45)` / `0.5)` / `0.7)` | radial glows, button shadow `box-shadow: 0 12px 30px rgba(57,216,111,0.22)`, navbar toggler borders, hero glow gradients |
| Mint highlight | `#9ff2b2` | `.hero-kicker` text colour, `.pipeline-number` text, footer link colour |
| Mint border alpha | `rgba(159,242,178,0.22)` | `.hero-kicker` border, `.blog-card-link:hover .blog-card` border |
| Mint pale (button text on green) | `#08110a` | text colour on `.btn-hero-primary` and `cookie-buttons button:first-child` |

### Neutrals — surface and chrome

| Role | Value | Used for |
|---|---|---|
| Page background base | `#07111a` | `body` background, `.footer` background |
| Navbar background | `rgba(5, 10, 15, 0.92)` | `.navbar` (with `backdrop-filter: blur(10px)`) |
| Navbar bottom border | `rgba(255,255,255,0.08)` | `.navbar`, `.footer` top border |
| Mobile-nav background | `rgba(8, 14, 22, 0.96)` | `.navbar-collapse` on `<=991.98px` |
| Cookie banner background | `rgba(8,14,22,0.96)` | `.cookie-banner` |
| Card surface | `rgba(17,23,32,0.78)` | `.feature-card` |
| Soft panel surface | `rgba(255,255,255,0.03)` | `.demo-stat`, `.demo-panel`, `.pipeline-step`, `.article-diagram` |
| Soft panel — alt | `rgba(255,255,255,0.04)` | `.formula-block` |
| Placeholder surface | `rgba(255,255,255,0.02)` | `.demo-placeholder` |
| Subtle hover surface | `rgba(255,255,255,0.05)` | `.btn-hero-secondary`, mobile nav link hover |
| Hover surface (stronger) | `rgba(255,255,255,0.08)` | `.cookie-buttons button:last-child`, `.btn-hero-secondary:hover` (close-to: `0.09`) |
| Hairline border | `rgba(255,255,255,0.08)` | `.feature-card`, `.demo-stat`, `.demo-panel`, `.pipeline-step` |
| Border — stronger | `rgba(255,255,255,0.10)` | `.cookie-banner`, `.demo-hero-video-wrap` |
| Border — strongest | `rgba(255,255,255,0.18)` | `.navbar-toggler` (default), `.demo-placeholder` dashed |
| Outline button border | `rgba(255,255,255,0.34)` | `.btn-hero-secondary` |
| Outline button border — hover | `rgba(255,255,255,0.55)` | `.btn-hero-secondary:hover` |
| Decorative info-blue glow | `rgba(80,180,255,0.18)` / `rgba(90,190,255,0.14)` / `rgba(120,220,255,0.05)` / `rgba(115,235,255,0.10)` | `.hero-bottom-glow`, `.home-section` top gradient, helix sweep |
| Decorative panel border (blue) | `rgba(111,195,255,0.12)` / `0.22)` | `.article-diagram`, `.formula-block` |

### Text colours

| Role | Value | Used for |
|---|---|---|
| Body text (default) | `#e5edf5` | `body` |
| Heading text | `#f8fafc` | `.feature-card h1–h4`, `.container h1–h4`, `.demo-stat-value`, `.demo-panel h4`, `.article-content h2`, cookie reject button text |
| Hero title | `#f8fafc` | `.hero-full-title` |
| Hero body | `#dbe5ef` | `.hero-full-text`, cookie banner `<p>` |
| Card body | `#cbd5e1` | `.feature-card p/li`, `.container p/li/.lead`, `.demo-panel p`, footer `color` |
| Article body | `#d7e1ec` | `.article-content p/li` |
| Muted label | `#9fb3c8` | `.diagram-caption`, `.formula-title`, `.demo-stat-label`, `.demo-placeholder` |
| Navbar link (idle) | `#d6deea` | `.navbar .nav-link` |
| Navbar link (hover) | `#39d86f` | `.navbar .nav-link:hover/:focus` |
| Navbar brand text | `#ffffff` | `.navbar-brand` |
| Helix rung stroke | `rgba(210,247,255,0.85)` | `.helix-rung` |
| Helix signal dot | `#dffaff` | `.signal-dot` fill |
| Article formula body | `#e6f3ff` | `.formula-body` |
| Formula note | `#b9c9d8` | `.formula-note` |

### Semantic colours

The marketing site **does not define success / warning / error tokens**. There
is no green-tick / red-cross usage in `site.css`. Semantic feedback colours come
in only on the CIMS demo page (see §8 below) and are local to that file:

| Role | Value (demo only) |
|---|---|
| Pass / success | `#16A34A` (`--pass`), light tint `#DCFCE7` bg / `#14532D` text / `#86EFAC` border |
| Warn | `#D97706` (`--warn`), light tint `#FEF3C7` bg / `#78350F` text / `#FCD34D` border |
| Fail / error | `#DC2626` (`--fail`), light tint `#FEE2E2` bg / `#7F1D1D` text / `#FCA5A5` border |
| Info chip (rose) | `bg-rose-50` `#fff1f2` / text `text-rose-700` / border `border-rose-200` (Tailwind palette via CDN) |

> Recommended for the tender-discovery dashboard: lift the green from the
> marketing palette (`#39d86f` / mint `#9ff2b2`) for *positive* states, and adopt
> the demo's amber `#F5A623` / red `#DC2626` for warn / fail to match the
> Genera-CIMS dashboard chrome.

### Dark vs light mode

- **Marketing site is dark-only.** No light-mode theme. No `prefers-color-scheme`
  rule. No light-token alternates.
- The CIMS demo (§8) is the only light-surface artefact: it uses `bg-slate-50`
  page bg, white card surfaces, with brand-amber `#F5A623` and brand-navy
  `#1B2B4B` as the dark anchors.

---

## 3. Typography

### Families

```css
body {
    font-family: Arial, Helvetica, sans-serif;
}
```

- **The marketing site uses system Arial.** No `@font-face`, no Google Fonts, no
  `next/font`, no webfont. Headings inherit from `body`.
- No serif typeface. **No Fraunces. No Inter.** That is the truth of the source.
- The CIMS demo declares a stack `ui-sans-serif, system-ui, -apple-system,
  "Segoe UI", sans-serif` for body and `ui-monospace, "SF Mono", Menlo,
  Consolas, monospace` for `.mono`. These are local to the demo file.

> If the tender-discovery dashboard wants more distinctive typography, **Inter**
> (or `ui-sans-serif, system-ui` like the demo) is the safest upgrade that
> preserves the existing geometry. There is no source-defined display face to
> port over.

### Display / heading scale

Heading sizes are mostly Bootstrap defaults (i.e. `h1` 2.5rem, `h2` 2rem,
`h3` 1.75rem, `h4` 1.5rem) **except** where overridden in `site.css`:

| Selector | font-size | line-height | weight | letter-spacing | colour |
|---|---|---|---|---|---|
| `.hero-full-title` | `clamp(3rem, 5vw, 5.2rem)` | `0.98` | `800` | `-0.03em` | `#f8fafc` |
| `.hero-full-title` (`<=768px`) | `2.2rem` | `1.08` | (inherits 800) | (inherits) | (inherits) |
| `.hero-full-text` | `1.2rem` | `1.75` | (default) | — | `#dbe5ef` |
| `.hero-full-text` (`<=992px`) | `1.05rem` | `1.65` | — | — | — |
| `.hero-full-text` (`<=768px`) | `0.95rem` | `1.6` | — | — | — |
| `.hero-kicker` | `0.85rem` | (default) | `700` | `0.08em` | `#9ff2b2` |
| `.feature-card p / li` | (default) | `1.7` | — | — | `#cbd5e1` |
| `.article-content p` | `1.08rem` | `1.9` | — | — | `#d7e1ec` |
| `.article-content li` | (default) | `1.8` | — | — | `#d7e1ec` |
| `.diagram-caption` | `0.96rem` | (default) | — | — | `#9fb3c8` |
| `.formula-title` | `0.82rem` | (default) | (default) | `0.08em`, `text-transform: uppercase` | `#9fb3c8` |
| `.formula-body` | `1.35rem` | `1.4` | `600` | — | `#e6f3ff` |
| `.demo-stat-label` | `0.82rem` | (default) | (default) | `0.06em`, `text-transform: uppercase` | `#9fb3c8` |
| `.demo-stat-value` | `1.5rem` | `1.25` | `700` | — | `#f8fafc` |
| `.demo-stat-value.small-value` | `1rem` | `1.25` | `700` | — | `#f8fafc` |
| Footer `<p>` | (Bootstrap) | (default) | — | — | `#cbd5e1` |

### Weight usage

- `300/400` — never explicitly set; Bootstrap defaults apply to body and most
  paragraphs.
- `600` — `.formula-body` only.
- `700` — `.hero-kicker`, `.navbar-brand`, `.demo-stat-value`, `.pipeline-number`,
  `.cookie-buttons button`, all `.btn-hero-*`.
- `800` — `.hero-full-title` only. This is the brand display weight.

### Uppercase / letter-spacing idiom

Three places: `.hero-kicker` (`letter-spacing: 0.08em`, `font-weight: 700`),
`.formula-title` (`letter-spacing: 0.08em`, `text-transform: uppercase`,
`#9fb3c8`), `.demo-stat-label` (`letter-spacing: 0.06em`, `text-transform:
uppercase`, `#9fb3c8`). Use uppercase eyebrow text whenever you need a label
above a heading or stat — it is the recurring labelling pattern.

---

## 4. Spacing & layout

### Spacing scale (in use)

Bootstrap 5 utility scale is in active use in the markup: `mb-3 / mb-4 / mb-5`,
`mt-3 / mt-4`, `py-3 / py-5`, `g-4` for gutters. Bootstrap's spacer scale
(`$spacer = 1rem`): `1=0.25rem`, `2=0.5rem`, `3=1rem`, `4=1.5rem`, `5=3rem`.

Custom pixel values in `site.css` (literals, ordered roughly low→high):
- `6px` (navbar toggler padding, navbar nav gap)
- `8px` (hero kicker padding-y, hero button gap-inner)
- `10px` (multiple — navbar brand gap, mobile nav link padding-y, hero button
  border-radius, cookie-button padding-y, formula-title margin-bottom)
- `12px` (multiple — card heading margin-bottom, card li margin-bottom,
  formula-block radius, navbar-toggler radius, formula-note margin-top)
- `14px` (multiple — navbar collapse margin-top, pipeline-number margin-bottom,
  cookie-banner radius, demo-stat radius, hero-demo-video radius)
- `16px` (multiple — hero button gap, demo-placeholder margin-top, cookie-banner
  padding, article-h2 margin-bottom, feature-card radius)
- `18px` (multiple — demo-stat padding, pipeline-grid gap, article-content p
  margin-bottom, cookie-banner shadow)
- `20px` (multiple — article-diagram padding, demo-panel padding, hero-kicker
  margin-bottom, formula-block padding-y, hero-full-content margin-right,
  article-content ul padding-left)
- `22px` (hero-full-title margin-bottom, article-content ul padding-left)
- `24px` (multiple — feature-card padding, formula-block padding-x,
  article-diagram top-margin pair)
- `28px` (hero button padding-x)
- `32px` (hero-full-text margin-bottom, article-diagram margin)
- `36px` (article h2 margin-top)
- `40px` (multiple — navbar-brand img height, article-content padding,
  hero-bottom margins)
- `42px` (pipeline-number diameter, navbar-toggler height)
- `54px` (navbar-toggler width)
- `60px` / `70px` / `80px` / `90px` (hero/section vertical padding stops)
- `180px` / `220px` (hero-bottom decoration heights)
- `420px` (`.demo-hero-media` width, `demo-hero-shell` right column)
- `520px` (mobile cap for demo-hero-media)
- `560px` (centred description in CIMS preview launcher)
- `620px` (demo-hero-copy max-width)
- `700px` (`.hero-full-text` max-width)
- `760px` (`.hero-full-content` max-width)
- `980px` (`.article-content` max-width)

> When porting to Tailwind 4, map these to the default 4px scale wherever
> possible. `42px = 10.5` doesn't fit cleanly — keep as arbitrary value
> `w-[42px]` for pipeline numbers / toggler.

### Container & grid

- **Grid system**: Bootstrap 5 12-column responsive grid. Markup uses `container`,
  `.row.g-4` (rows with `1.5rem` gutters), `col-md-6`, `col-md-4`, `col-md-3`,
  `col-lg-3`, `col-lg-6`.
- **Custom CSS Grid** is used in three named places:
  - `.demo-hero-shell` — `grid-template-columns: minmax(0, 1fr) 420px; gap: 56px`,
    collapses to single column at `<=992px`.
  - `.pipeline-grid` — `grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 18px`, drops to `repeat(3, ...)` at `<=1200px`, then `repeat(2, ...)` at
    `<=992px`, then `1fr` at `<=768px`.
  - The Bootstrap `.container` widths (defaults: 540 / 720 / 960 / 1140 / 1320 px
    at the standard breakpoints) are not overridden.

### Breakpoints

- Custom breakpoints in `site.css`:
  - `@media (max-width: 768px)` — mobile.
  - `@media (max-width: 992px)` — tablet.
  - `@media (max-width: 991.98px)` — Bootstrap navbar collapse breakpoint
    (mobile-nav drawer styling).
  - `@media (max-width: 1200px)` — pipeline grid step-down.
- Bootstrap 5 default breakpoints are also live: `sm 576px`, `md 768px`,
  `lg 992px`, `xl 1200px`, `xxl 1400px`.

### Section rhythm

- Hero section: `min-height: 100vh`, full-bleed, `display: flex; align-items: center`.
- Content section (`.home-section`): `padding-top: 60px; padding-bottom: 40px`,
  plus an internal `.container.py-5`. So a typical content section starts ~108px
  below the hero.
- Vertical spacing between cards: `mb-5` (= 3rem) is the standard.

---

## 5. Component patterns

### 5.1 Navbar (top nav)

```html
<header>
    <nav class="navbar navbar-expand-lg border-bottom box-shadow mb-3">
        <div class="container">
            <a class="navbar-brand" asp-page="/Index">
                <img src="~/images/logo.png" alt="Genera Systems Logo" />
                <span>Genera Systems</span>
            </a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse"
                    data-bs-target="#mainNav" aria-label="Toggle navigation">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div id="mainNav" class="navbar-collapse collapse">
                <ul class="navbar-nav ms-auto">
                    <li class="nav-item"><a class="nav-link" asp-page="/Index">Home</a></li>
                    <!-- ...Platform, Products, CIMS, Consulting, Insights, About, Contact -->
                </ul>
            </div>
        </div>
    </nav>
</header>
```

Key styles (`site.css`):
```css
.navbar {
    position: relative; z-index: 1000;
    background: rgba(5, 10, 15, 0.92) !important;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
}
.navbar-brand { display: flex; align-items: center; gap: 10px; font-weight: 700; color: #ffffff !important; }
.navbar-brand img { height: 40px; width: auto; }
.navbar .nav-link { color: #d6deea !important; transition: color 0.2s ease; }
.navbar .nav-link:hover, .navbar .nav-link:focus { color: #39d86f !important; }
```

Mobile drawer (`<=991.98px`):
```css
.navbar-collapse {
    margin-top: 14px; padding: 14px 16px; border-radius: 14px;
    background: rgba(8, 14, 22, 0.96);
    border: 1px solid rgba(255,255,255,0.08);
}
.navbar-nav { gap: 6px; }
.navbar .nav-link { display: block; padding: 10px 12px; border-radius: 10px; }
.navbar .nav-link:hover { background: rgba(255,255,255,0.05); }
```

Hamburger toggler (custom — replaces Bootstrap's SVG icon):
```css
.navbar-toggler {
    width: 54px; height: 42px; padding: 0;
    border: 1px solid rgba(57,216,111,0.45);
    border-radius: 12px;
    background: rgba(8, 14, 22, 0.92);
    display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer;
}
.navbar-toggler:focus { box-shadow: none; border-color: rgba(57,216,111,0.7); }
.navbar-toggler-icon {
    background-image: none !important;
    width: 22px; height: 14px; position: relative; display: inline-block;
    border-top: 2px solid #ffffff;
}
.navbar-toggler-icon::before, .navbar-toggler-icon::after {
    content: ""; position: absolute; left: 0; width: 22px;
    border-top: 2px solid #ffffff;
}
.navbar-toggler-icon::before { top: 5px; }
.navbar-toggler-icon::after { top: 10px; }
```

Nav items (in order): Home · Platform · Products · CIMS · Consulting · Insights · About · Contact.

### 5.2 Buttons

Two button classes only — `.btn-hero-primary` and `.btn-hero-secondary` —
reused everywhere a CTA appears (including outside heroes, e.g. inside cards).

```css
.btn-hero-primary,
.btn-hero-secondary {
    display: inline-flex; align-items: center; justify-content: center;
    min-width: 190px;
    padding: 15px 28px;
    border-radius: 10px;
    text-decoration: none;
    font-weight: 700;
    transition: all 0.2s ease;
}
.btn-hero-primary {
    background: #39d86f;
    color: #08110a;
    box-shadow: 0 12px 30px rgba(57,216,111,0.22);
}
.btn-hero-primary:hover,
.btn-hero-primary:focus {
    background: #55e583; color: #08110a;
}
.btn-hero-secondary {
    border: 1px solid rgba(255,255,255,0.34);
    background: rgba(255,255,255,0.05);
    color: #ffffff;
}
.btn-hero-secondary:hover,
.btn-hero-secondary:focus {
    border-color: rgba(255,255,255,0.55);
    background: rgba(255,255,255,0.09);
    color: #ffffff;
}
```

Group wrapper:
```css
.hero-full-buttons {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
}
```

Mobile (`<=768px`): button group becomes column-stretched, individual buttons go
full-width:
```css
.hero-full-buttons { flex-direction: column; align-items: stretch; width: 100%; }
.btn-hero-primary, .btn-hero-secondary { width: 100%; min-width: 0; }
```

**No ghost / tertiary / icon button class exists.** No size variants. No
disabled style is declared.

Cookie banner buttons reproduce the same colour logic in miniature:
```css
.cookie-buttons button {
    border: none;
    border-radius: 10px;
    padding: 10px 16px;
    font-weight: 700;
    cursor: pointer;
}
.cookie-buttons button:first-child {
    background: #39d86f; color: #08110a;
}
.cookie-buttons button:last-child {
    background: rgba(255,255,255,0.08);
    color: #f8fafc;
    border: 1px solid rgba(255,255,255,0.14);
}
```

### 5.3 Cards

The primary content container is `.feature-card`. The site also nests
`feature-card` inside `feature-card` (outer = section wrapper, inner = item).

```css
.feature-card {
    height: 100%;
    background: rgba(17,23,32,0.78);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    padding: 24px;
    box-shadow: 0 12px 30px rgba(0,0,0,0.22);
}
.feature-card h1, .feature-card h2, .feature-card h3, .feature-card h4 {
    color: #f8fafc; margin-bottom: 12px;
}
.feature-card p, .feature-card li { color: #cbd5e1; line-height: 1.7; }
.feature-card ul { margin-top: 12px; margin-bottom: 0; padding-left: 20px; }
.feature-card li { margin-bottom: 10px; }
```

Variant cards (used in `/DemoLab`, `/Genera-CIMS`):

```css
.demo-panel {
    height: 100%; padding: 20px; border-radius: 16px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
}
.demo-panel h4 { margin-bottom: 10px; color: #f8fafc; }
.demo-panel p { color: #cbd5e1; line-height: 1.7; }
```

```css
.demo-stat {
    height: 100%; padding: 18px; border-radius: 14px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
}
.demo-stat-label {
    font-size: 0.82rem; letter-spacing: 0.06em; text-transform: uppercase;
    color: #9fb3c8; margin-bottom: 8px;
}
.demo-stat-value {
    color: #f8fafc; font-size: 1.5rem; font-weight: 700; line-height: 1.25;
}
.demo-stat-value.small-value { font-size: 1rem; }
```

```css
.pipeline-step {
    height: 100%; padding: 20px; border-radius: 16px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
}
.pipeline-number {
    display: inline-flex; align-items: center; justify-content: center;
    width: 42px; height: 42px;
    margin-bottom: 14px;
    border-radius: 50%;
    background: rgba(57,216,111,0.14);
    color: #9ff2b2;
    font-weight: 700;
}
```

Blog card (hover lift):
```css
.blog-card-link { display: block; text-decoration: none; color: inherit; }
.blog-card-link:hover .blog-card,
.blog-card-link:focus .blog-card {
    transform: translateY(-4px);
    border-color: rgba(159,242,178,0.22);
    box-shadow: 0 14px 34px rgba(0,0,0,0.28);
}
.blog-card { transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease; }
```

> **Heads up:** `.blog-card`, `.blog-tag`, `.contact-card`, `.contact-label`,
> `.contact-value` are referenced in markup but **have no rules in
> `site.css`**. They currently fall back to inherited/Bootstrap defaults. If
> porting, define them — or remove the hooks.

Article-content "diagram" / "formula" callouts:
```css
.article-diagram {
    margin: 32px 0; padding: 20px;
    border-radius: 18px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(111,195,255,0.12);
    box-shadow: 0 10px 24px rgba(0,0,0,0.18);
}
.formula-block {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(111,195,255,0.22);
    border-radius: 18px;
    padding: 20px 24px;
    margin: 24px 0;
}
```

### 5.4 Forms

The marketing site **does not include any form components** in `site.css`. The
Contact page exposes `mailto:` and a LinkedIn link only — no input fields, no
labels. There is no input / textarea / select / checkbox / radio styling to
port. If the dashboard needs forms, follow the card aesthetic:
- Dark surface `rgba(255,255,255,0.03)` background.
- `1px solid rgba(255,255,255,0.08)` border.
- `border-radius: 10px` (matches button radius).
- Focus state by analogy with `.navbar-toggler:focus`:
  `border-color: rgba(57,216,111,0.5–0.7); box-shadow: none;`.
- Label text colour `#9fb3c8`, upper-cased, `letter-spacing: 0.06–0.08em`.

### 5.5 Hero section pattern (page-level)

Markup (every primary page uses this exact shell):

```html
<section class="hero-full hero-[home|about|blog|demo|contact|technology|consulting]">
    <div class="hero-full-overlay"></div>

    <div class="hero-full-content">
        <div class="hero-kicker">UPPERCASE · KICKER · TEXT</div>
        <h1 class="hero-full-title">Headline line one<br />headline line two.</h1>
        <p class="hero-full-text">One supporting paragraph, ~1–3 sentences.</p>
        <div class="hero-full-buttons">
            <a class="btn-hero-primary" href="#x">Primary CTA</a>
            <a class="btn-hero-secondary" href="#y">Secondary CTA</a>
        </div>
    </div>

    <div class="hero-bottom-glow"></div>
    <div class="hero-bottom-shine"></div>
</section>
```

Hero shell (full-bleed, full-viewport-height, dark overlay over background image):

```css
.hero-full {
    position: relative; z-index: 1;
    width: 100%; min-height: 100vh;
    margin: 0; padding: 0;
    background-size: cover;
    background-position: center center;
    background-repeat: no-repeat;
    display: flex; align-items: center;
    overflow: hidden;
}
.hero-full-overlay {
    position: absolute; inset: 0;
    z-index: 1; pointer-events: none;
    background:
        linear-gradient(to right, rgba(5,10,15,0.82) 0%, rgba(5,10,15,0.58) 36%, rgba(5,10,15,0.30) 58%, rgba(5,10,15,0.62) 100%),
        linear-gradient(to bottom, rgba(5,10,15,0.28) 0%, rgba(5,10,15,0.12) 45%, rgba(5,10,15,0.56) 100%);
}
.hero-full-content {
    position: relative; z-index: 3;
    max-width: 760px;
    margin-left: 8vw;       /* offset from left edge — defines the asymmetric layout */
    margin-right: 20px;
    padding-top: 80px;
    padding-bottom: 60px;
}
```

The pill kicker:
```css
.hero-kicker {
    display: inline-block;
    margin-bottom: 20px;
    padding: 8px 14px;
    border: 1px solid rgba(159,242,178,0.22);
    border-radius: 999px;
    background: rgba(8,16,24,0.35);
    color: #9ff2b2;
    font-size: 0.85rem;
    font-weight: 700;
    letter-spacing: 0.08em;
}
```

Decorative bottom-of-hero glow + diagonal shine:
```css
.hero-bottom-glow {
    position: absolute; left: 0; right: 0; bottom: -1px;
    height: 220px; z-index: 2; pointer-events: none;
    background: radial-gradient(
        ellipse at center,
        rgba(80,180,255,0.18) 0%,
        rgba(57,216,111,0.10) 22%,
        rgba(255,255,255,0.04) 38%,
        rgba(7,11,16,0) 72%
    );
}
.hero-bottom-shine {
    position: absolute; left: -20%; bottom: 8%;
    width: 140%; height: 180px;
    z-index: 2; pointer-events: none;
    background: linear-gradient(90deg,
        rgba(255,255,255,0) 0%,
        rgba(120,220,255,0.05) 20%,
        rgba(255,255,255,0.12) 50%,
        rgba(57,216,111,0.08) 72%,
        rgba(255,255,255,0) 100%
    );
    filter: blur(24px);
    transform: rotate(-2deg);
    opacity: 0.9;
}
```

Demo Lab hero variant (overlay lightened so the video alongside is brighter):
```css
.hero-demo .hero-full-overlay {
    background:
        linear-gradient(to right, rgba(5,10,15,0.72) 0%, rgba(5,10,15,0.42) 36%, rgba(5,10,15,0.18) 58%, rgba(5,10,15,0.38) 100%),
        linear-gradient(to bottom, rgba(5,10,15,0.16) 0%, rgba(5,10,15,0.08) 45%, rgba(5,10,15,0.24) 100%);
}
```

Per-page hero backgrounds (literal):
```css
.hero-home       { background: url('/Images/hero-optimization-engine.webp') center center / cover no-repeat !important; }
.hero-about      { background-image: url('/images/hero-system-architecture.webp'); }
.hero-blog       { background-image: url('/images/hero-optimization-researc.webp'); }
.hero-demo       { background-image: url('/Images/hero-demo-lab.webp'); }
.hero-contact    { background-image: url('/images/hero-network-connection.webp'); }
.hero-technology { background-image: url('/images/hero-algorithm-technology.webp'); }
.hero-consulting { background-image: url('/images/hero-system-architecture.webp'); }
```

Hero images live at `C:\Code\GeneraSystems\wwwroot\Images\hero-*.webp` (~280 KB
to ~800 KB each). They are dark, painterly, abstract construction / network /
algorithm renderings. The dashboard should adopt the same dark-photograph-with-
overlay pattern.

### 5.6 Tag / pill / badge

There is exactly one pill component fully styled in the source — the
`.hero-kicker` (see §5.5). It is reused inline inside cards by overriding
padding: `<p class="hero-kicker" style="padding-left: 0;">…</p>` to align it
with the card edge.

`.blog-tag` is referenced in markup but undefined.

The CIMS demo (§8) shows a useful in-product badge pattern using Tailwind
classes — e.g. status pill: `inline-flex items-center gap-1.5 text-xs px-2
py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-200` — adapt
for tender-status chips in the dashboard.

### 5.7 Helix intro overlay

A one-shot animated SVG overlay rendered on every page (suppressed if
`ViewData["DisableHelixIntro"]` is true). Two glowing helical strands draw
across the viewport (~2.8–3.1s) with rungs and signal-dots that travel along an
`offset-path`. Wrapper:

```css
.helix-intro {
    position: fixed; inset: 0; z-index: 2000;
    pointer-events: none; overflow: hidden; opacity: 0;
    animation:
        helixIntroFadeIn 0.5s ease-out forwards,
        helixIntroFadeOut 1.6s ease 4.8s forwards;
}
```

Strands use `stroke: url(#helixBlueA)` / `#helixBlueB` gradients and
`filter: url(#helixGlowStrong)` — defined in the inline SVG in
`_Layout.cshtml`. Eight signal dots (`.s1`–`.s8`) move along two cubic Bézier
`offset-path` curves at staggered offset-distance starts (0%, 12%, 16%, 28%,
34%, 45%, 52%, …) with both `signalPulse` and `moveAn` / `moveBn` keyframes.

Total intro time: 6.8 s (then JS removes the node). This is signature visual
chrome — port it to the dashboard sign-in / first-load only, not on every nav.

### 5.8 Footer

```html
<footer class="footer border-top">
    <div class="container py-3">
        &copy; 2026 Genera Systems &middot; Founded by Eduard Szigeti &middot;
        <a asp-page="/Privacy">Privacy</a> |
        <a asp-page="/Cookies">Cookies</a> |
        <a asp-page="/Terms">Terms</a>
    </div>
</footer>
```

```css
.footer {
    z-index: 20;
    background: #07111a;
    color: #cbd5e1 !important;
    border-top: 1px solid rgba(255,255,255,0.08);
}
.footer a { color: #9ff2b2; text-decoration: none; }
.footer a:hover { color: #ffffff; }
```

One-line footer. No columns, no logo, no newsletter, no socials. Mint links on
dark.

### 5.9 Cookie banner

Bottom-floating dialog, accept / reject buttons:

```css
.cookie-banner {
    position: fixed;
    bottom: 20px; left: 20px; right: 20px;
    display: none;
    justify-content: space-between; align-items: center;
    gap: 16px;
    padding: 16px 18px;
    border-radius: 14px;
    background: rgba(8,14,22,0.96);
    border: 1px solid rgba(255,255,255,0.10);
    box-shadow: 0 16px 40px rgba(0,0,0,0.35);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    z-index: 9999;
}
.cookie-banner p { margin: 0; color: #dbe5ef; line-height: 1.6; }
.cookie-buttons { display: flex; gap: 10px; flex-shrink: 0; }
```

(Tablet) at `<=992px`: stacks vertically (`flex-direction: column;
align-items: flex-start;`) and buttons grow to full width.

---

## 6. Visual language

### Border radii (every value used)

| Value | Examples |
|---|---|
| `10px` | `.btn-hero-*`, `.navbar .nav-link` (mobile), `.cookie-buttons button`, `.navbar-toggler` (default — initial rule), `.navbar-collapse > .nav-link` (mobile) |
| `12px` | `.navbar-toggler` (overridden later block) |
| `14px` | `.navbar-collapse` (mobile), `.cookie-banner`, `.demo-stat`, `.demo-placeholder`, `.hero-demo-video` |
| `16px` | `.feature-card`, `.demo-panel`, `.pipeline-step` |
| `18px` | `.article-diagram`, `.formula-block` |
| `20px` | `.demo-hero-video-wrap` |
| `50%` | `.pipeline-number` (circle) |
| `999px` | `.hero-kicker` (pill) |

### Shadows / elevation

| Token (informal) | Value | Where |
|---|---|---|
| Card | `0 12px 30px rgba(0,0,0,0.22)` | `.feature-card` |
| Article diagram | `0 10px 24px rgba(0,0,0,0.18)` | `.article-diagram` |
| Blog hover | `0 14px 34px rgba(0,0,0,0.28)` | `.blog-card-link:hover .blog-card` |
| Demo video frame | `0 18px 40px rgba(0,0,0,0.28)` | `.demo-hero-video-wrap` |
| Cookie banner | `0 16px 40px rgba(0,0,0,0.35)` | `.cookie-banner` |
| Hero primary CTA | `0 12px 30px rgba(57,216,111,0.22)` | `.btn-hero-primary` (green-tinted) |
| Hero title text shadow | `0 10px 28px rgba(0,0,0,0.32)` | `.hero-full-title` |
| Hero body text shadow | `0 4px 18px rgba(0,0,0,0.28)` | `.hero-full-text` |

### Backdrop filters

- `.navbar` → `backdrop-filter: blur(10px)`
- `.cookie-banner` → `backdrop-filter: blur(10px)`
- `.demo-hero-video-wrap` → `backdrop-filter: none` (explicit reset to defeat
  inherited blur)

### Image treatments

- All hero backgrounds are `.webp`, `background-size: cover`, `background-
  position: center center`, no border-radius (full-bleed).
- Demo / inline media (`.hero-demo-video`) uses
  `border-radius: 14px; aspect-ratio: 16 / 9; background: #000;` inside a
  wrapping frame `.demo-hero-video-wrap` (8px padding, 20px outer radius, dark
  navy `#0b1118` frame, hairline white border).
- Mobile (`<=768px`) reduces `.hero-demo-video` to `border-radius: 10px`.

### Icons

- **No icon library.** No Lucide / Heroicons / Feather imports anywhere. Razor
  pages use raw HTML entities for arrows (`&rarr;`, `&larr;`, `&middot;`,
  `&rsaquo;`) and `<sup>&reg;</sup>` for PMBOK references.
- The hamburger icon is **drawn from CSS borders**, not an SVG (see §5.1).
- The helix intro overlay is the only SVG visual (`_Layout.cshtml`, inline).
- The CIMS demo embeds inline SVG path icons (warning triangle, etc.) — see
  `wwwroot/demos/cims-filename-validation.html`. Treat as inspiration for in-app
  iconography choices.

### Animation / transition

- Standard hover/focus transition duration is **`0.2s ease`** across the site:
  - `.navbar .nav-link { transition: color 0.2s ease; }`
  - `.btn-hero-* { transition: all 0.2s ease; }`
  - `.blog-card { transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease; }`
  - `.field-chip { transition: background .15s ease, color .15s ease, border-color .15s ease; }` (demo only)
- Blog-card hover lift: `transform: translateY(-4px)`.
- Helix intro keyframes (excerpt):
  ```css
  @keyframes helixDrawSlow {
      0%   { stroke-dashoffset: 2200; opacity: 0.15; }
      25%  { opacity: 0.95; }
      100% { stroke-dashoffset: 0; opacity: 1; }
  }
  @keyframes helixSweepDiagonal {
      0%   { transform: translate(38%, -38%) rotate(-32deg); opacity: 0; }
      18%  { opacity: 1; }
      55%  { opacity: 1; }
      100% { transform: translate(-42%, 42%) rotate(-32deg); opacity: 0; }
  }
  @keyframes signalPulse {
      0%   { opacity: 0; transform: scale(0.6); }
      10%  { opacity: 1; transform: scale(1); }
      50%  { opacity: 0.95; transform: scale(1.15); }
      90%  { opacity: 0.9; transform: scale(0.9); }
      100% { opacity: 0; transform: scale(0.6); }
  }
  ```
- Easing curve used by helix strands and the diagonal sweep:
  `cubic-bezier(.2,.8,.2,1)`. Worth adopting as the dashboard's "expressive"
  ease (and `ease`/`ease-out` for everything else).
- Cursor convention (override):
  ```css
  html, body, * { cursor: default; }
  a, button, .navbar-toggler, .nav-link { cursor: pointer; }
  ```

---

## 7. Page structures

### 7.1 Homepage (`Pages/Index.cshtml`)

Order:
1. Hero with `hero-home` background (`/Images/hero-optimization-engine.webp`),
   kicker `"CONSTRUCTION ASSURANCE · GOVERNANCE PLATFORM · UK PROJECTS"`,
   title `"Governance built into the build."`, two CTAs:
   `Engage Consulting` (primary) → `/Consulting`, `Explore the Platform`
   (secondary) → `/Technology`.
2. `.home-section` with `.container.py-5`.
3. Single full-width `.feature-card.mb-5` — "What Genera Systems Does".
4. Two-up `.row.g-4.mb-5` of `.feature-card.h-100` — "The Problem" / "The
   Solution".
5. Wrapping `.feature-card.mb-5` titled "Three Products. One Platform Family.",
   containing `.row.g-4.mt-2` of three inner `.feature-card.h-100` — Genera QA &
   HS&E, Genera CIMS, Genera Schedule. Each inner card has a `<strong>Status:</strong>`
   line at the bottom.
6. Two-up `.row.g-4.mb-5` — "Built by a Project Manager" / "Start with
   Consulting" (second card has an inline CTA group `.hero-full-buttons.mt-3`).
7. Final `.feature-card` — "Who This Is For" with two `<ul>` columns.

### 7.2 Inner page hero (every non-homepage page)

Identical to homepage hero shell but with a page-specific background image and
its own copy. CTAs always link either to an in-page anchor (`#section-id`) or to
another Razor page.

Inner page body structure is consistently:

```html
<section class="home-section">
    <div class="container py-5">
        <section id="…" class="mb-5">
            <div class="feature-card">…</div>
        </section>
        <!-- repeated -->
    </div>
</section>
```

`.mb-5` is the rhythm between top-level sections; everything sits inside an
outer `.feature-card`, with optional inner grids of cards.

### 7.3 DemoLab / Products page idioms

- "Stats row" idiom: a `.row.g-4` of four `.col-md-3 > .demo-stat` cards. Use
  for headline metrics (e.g. "DCMA 14-point", "1,000 tasks").
- "Panel row" idiom: `.row.g-4` of `.col-lg-6 > .demo-panel`, with each panel
  containing an `<h4>`, a `<p>`, and a `.demo-placeholder` for a future
  visualisation.
- `.demo-placeholder` is a dashed-border content slot:
  ```css
  .demo-placeholder {
      min-height: 220px;
      margin-top: 16px;
      display: flex; align-items: center; justify-content: center;
      border-radius: 14px;
      border: 1px dashed rgba(255,255,255,0.18);
      background: rgba(255,255,255,0.02);
      color: #9fb3c8;
      text-align: center;
      padding: 20px;
  }
  ```
- "Pipeline" idiom: five-column `.pipeline-grid` of numbered `.pipeline-step`
  cards.

### 7.4 DemoLab hero (asymmetric — copy + video)

Only this page uses an asymmetric hero layout via custom CSS Grid:

```css
.demo-hero-shell {
    position: relative; z-index: 3;
    min-height: 100vh;
    display: grid;
    grid-template-columns: minmax(0, 1fr) 420px;
    align-items: center;
    gap: 56px;
    padding-top: 90px;
    padding-bottom: 70px;
}
.demo-hero-copy { min-width: 0; max-width: 620px; }
.demo-hero-media { width: 420px; justify-self: end; }
.demo-hero-video-wrap {
    position: relative; z-index: 12;
    width: 100%; padding: 8px;
    border-radius: 20px;
    background: #0b1118;
    border: 1px solid rgba(255,255,255,0.10);
    box-shadow: 0 18px 40px rgba(0,0,0,0.28);
    backdrop-filter: none;
}
.hero-demo-video {
    position: relative; z-index: 13;
    display: block; width: 100%; max-width: 100%;
    aspect-ratio: 16 / 9; height: auto;
    border-radius: 14px;
    background: #000;
}
```

At `<=992px` it collapses to one column with `gap: 32px`.

### 7.5 Article / long-form page chrome

Used by blog post pages (under `Articles/` and any future blog `.cshtml`):

```css
.article-content {
    max-width: 980px; margin: 0 auto; padding: 40px;
}
.article-content h2 { margin-top: 36px; margin-bottom: 16px; color: #f8fafc; }
.article-content p  { font-size: 1.08rem; line-height: 1.9; color: #d7e1ec; margin-bottom: 18px; }
.article-content ul { margin-bottom: 20px; padding-left: 22px; }
.article-content li { margin-bottom: 10px; line-height: 1.8; color: #d7e1ec; }
```

Long-form is **wider line-height and a slightly larger paragraph size** than
card text. Diagrams (`.article-diagram`) and formulas (`.formula-block`) are
inline-callouts with cool-blue borders.

### 7.6 Body canvas (under every page)

The body itself is the dark canvas; every section is layered over it:

```css
body {
    display: flex; flex-direction: column;
    min-height: 100vh;
    font-family: Arial, Helvetica, sans-serif;
    color: #e5edf5;
    background-color: #07111a;
    background-image:
        linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px),
        radial-gradient(circle at 12% 18%, rgba(57,216,111,0.12), transparent 18%),
        linear-gradient(135deg, #07111a 0%, #0c1620 45%, #07111a 100%);
    background-size: 40px 40px, 40px 40px, auto, auto;
}
```

The `.home-section` adds its own radial-glow top gradient that meets the body
canvas seamlessly:

```css
.home-section {
    position: relative; z-index: 3;
    padding-top: 60px; padding-bottom: 40px;
    background:
        radial-gradient(ellipse at top center,
            rgba(90,190,255,0.14) 0%,
            rgba(57,216,111,0.08) 14%,
            rgba(255,255,255,0.03) 24%,
            rgba(7,11,16,0.86) 42%,
            rgba(7,11,16,0.97) 62%,
            rgba(7,11,16,1) 100%
        ),
        linear-gradient(to bottom,
            rgba(10,16,24,0.10) 0%,
            rgba(7,11,16,0.92) 26%,
            rgba(7,11,16,1) 100%
        );
}
```

---

## 8. The CIMS demo palette (separate, lighter, in-product)

The marketing-site palette is dark-mode only. The product mock at
`wwwroot/demos/cims-filename-validation.html` is the only place a *light*,
*dashboard-style* surface is rendered. Because the tender-discovery dashboard is
the same kind of in-product surface, lift these tokens for the **app shell**
while keeping the marketing surfaces dark.

```css
:root {
    --navy:  #1B2B4B;   /* primary dark accent — sidebar / header bar */
    --steel: #1B4F8A;   /* secondary blue — avatar chips, mid emphasis */
    --amber: #F5A623;   /* accent / "active" highlight */
    --ink:   #1A1F2E;   /* default body text */
    --mute:  #64748B;   /* muted text (slate-500) */
    --pass:  #16A34A;
    --warn:  #D97706;
    --fail:  #DC2626;
}
body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink); }
.mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
```

Tailwind utility colours actively used (CDN, no config):
- Page bg: `bg-slate-50`
- Card surface: `bg-white` with `ring-1 ring-slate-200`
- Subtle stripe: `bg-slate-50`, `border-slate-200`
- Sidebar item idle: `text-slate-600 hover:bg-slate-100`
- Sidebar item active: `font-medium text-navy bg-white border-l-4 border-amber-400`
  with a small `text-[10px] uppercase tracking-wider text-amber-600` "Active"
  label.
- Status pill (HRB): `bg-rose-50 text-rose-700 border border-rose-200`.
- Field-chip pass: `#DCFCE7` bg / `#14532D` text / `#86EFAC` border.
- Field-chip warn: `#FEF3C7` bg / `#78350F` text / `#FCD34D` border.
- Field-chip fail: `#FEE2E2` bg / `#7F1D1D` text / `#FCA5A5` border.

The hero / "brand-navy" sections inside the demo apply a 32 × 32 px transparent
white grid background for texture:

```css
.hero-grid {
    background-image:
        linear-gradient(to right,  rgba(255,255,255,.05) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(255,255,255,.05) 1px, transparent 1px);
    background-size: 32px 32px;
}
```

Recommended port mapping:
- **Marketing-style landing screen** (sign-in, "no tenders yet" empty states):
  use the dark canvas from §7.6 + Genera-green CTA.
- **In-app dashboard chrome** (tender list, filters, detail pane): light surface
  with navy `#1B2B4B` headers, amber `#F5A623` actives, white cards with
  `ring-1 ring-slate-200` and the demo's status-chip palette.

---

## 9. Quick port-to-Tailwind-4 cheatsheet

Drop these as the starting design tokens for the Next.js 15 dashboard:

```css
@theme {
    /* Genera brand */
    --color-genera-green:        #39d86f;
    --color-genera-green-hover:  #55e583;
    --color-genera-green-text:   #08110a;   /* text on green */
    --color-genera-mint:         #9ff2b2;

    /* Marketing dark canvas */
    --color-canvas-base:         #07111a;
    --color-canvas-gradient-mid: #0c1620;
    --color-surface-card:        rgb(17 23 32 / 0.78);
    --color-surface-soft:        rgb(255 255 255 / 0.03);
    --color-surface-softer:      rgb(255 255 255 / 0.02);
    --color-border-hair:         rgb(255 255 255 / 0.08);
    --color-border-strong:       rgb(255 255 255 / 0.18);

    /* Text */
    --color-fg-heading:          #f8fafc;
    --color-fg-body:             #cbd5e1;
    --color-fg-article:          #d7e1ec;
    --color-fg-muted:            #9fb3c8;
    --color-fg-nav-idle:         #d6deea;

    /* In-product (light) — from CIMS demo */
    --color-product-navy:        #1B2B4B;
    --color-product-steel:       #1B4F8A;
    --color-product-amber:       #F5A623;
    --color-product-ink:         #1A1F2E;

    /* Semantic */
    --color-success:             #16A34A;
    --color-warn:                #D97706;
    --color-fail:                #DC2626;

    /* Radii */
    --radius-btn:                10px;
    --radius-toggler:            12px;
    --radius-banner:             14px;
    --radius-card:               16px;
    --radius-callout:            18px;
    --radius-pill:               999px;

    /* Elevation */
    --shadow-card:               0 12px 30px rgb(0 0 0 / 0.22);
    --shadow-card-hover:         0 14px 34px rgb(0 0 0 / 0.28);
    --shadow-banner:             0 16px 40px rgb(0 0 0 / 0.35);
    --shadow-green-glow:         0 12px 30px rgb(57 216 111 / 0.22);

    /* Type — display headline */
    --font-display-size:         clamp(3rem, 5vw, 5.2rem);
    --font-display-line:         0.98;
    --font-display-weight:       800;
    --font-display-tracking:     -0.03em;
}
```

Recommended font stack to replace bare Arial:
`Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`.

---

## 10. File index (for the porter)

- `C:\Code\GeneraSystems\Pages\Shared\_Layout.cshtml` — shared shell, head meta,
  navbar, footer, cookie banner, helix intro SVG, GA4 consent script.
- `C:\Code\GeneraSystems\wwwroot\css\site.css` — **the only design-system
  source of truth on the marketing site**.
- `C:\Code\GeneraSystems\wwwroot\demos\cims-filename-validation.html` —
  separate in-product palette / chrome example, Tailwind CDN + CSS variables.
- `C:\Code\GeneraSystems\Pages\Index.cshtml` — homepage hero + section pattern.
- `C:\Code\GeneraSystems\Pages\DemoLab.cshtml` — stat / panel / pipeline /
  asymmetric-hero patterns.
- `C:\Code\GeneraSystems\Pages\Genera-CIMS.cshtml` — nested-card grid, concept-
  preview launcher.
- `C:\Code\GeneraSystems\Pages\Contact.cshtml` — `.contact-card` / `.contact-
  label` / `.contact-value` hooks (currently unstyled).
- `C:\Code\GeneraSystems\Pages\Blog.cshtml` — `.blog-tag` (currently unstyled),
  `.blog-card-link` hover-lift pattern.
- `C:\Code\GeneraSystems\wwwroot\Images\hero-*.webp` — page-specific hero
  backgrounds.

---

## 11. Known gaps / oddities (for the porter)

1. **Missing logo and PNG favicon assets.** The layout references
   `~/images/logo.png` and `~/images/favicon.png` but neither exists in
   `wwwroot/Images/`. Only `wwwroot/favicon.ico` and the hero `.webp`s ship.
   The port will need a new wordmark/logo asset.
2. **No webfont, no design face.** The marketing site renders headings in plain
   `Arial`. There is no Inter, no Fraunces, no Roboto, no `@font-face`. If the
   dashboard wants a more distinctive face, that decision is **net-new** — it
   does not exist in the source to faithfully port.
3. **No CSS variables / design tokens on the marketing surface.** Every colour,
   radius, and shadow is a literal value duplicated inline. Easy to port to
   Tailwind 4 `@theme` (see §9) but expect many literal hex repeats while
   reading source.
4. **Two distinct palettes coexist.** Marketing dark (`#07111a` + `#39d86f`
   green) vs CIMS-demo light (`#1B2B4B` navy + `#F5A623` amber). They don't
   share tokens. The dashboard needs an explicit decision on which one is the
   in-app shell; recommended split is in §8.
5. **Several class hooks defined in markup but unstyled.** `.blog-card`,
   `.blog-tag`, `.contact-card`, `.contact-label`, `.contact-value` have no
   matching rules in `site.css`. They currently render as default Bootstrap.
6. **Two clashing `.navbar-toggler` rule blocks.** The file declares the toggler
   twice (lines ~36 and ~79 of `site.css`) with different padding, border, and
   radius. The second wins via source order. Tidy when porting.
7. **`!important` overrides** on `.navbar { background … !important; }`,
   `.navbar-brand { color: #ffffff !important; }`, `.navbar .nav-link
   { color: … !important; }`, `.footer { color: #cbd5e1 !important; }`,
   `.hero-home { background: … !important; }`. Needed only because of Bootstrap
   specificity; can be dropped when not extending Bootstrap.
8. **Mixed-case asset paths.** Some hero classes reference `/Images/...` and
   others reference `/images/...` (e.g. `.hero-home` vs `.hero-about`). Works on
   case-insensitive filesystems (Windows / IIS) but will 404 on case-sensitive
   hosts. Normalise when porting.
9. **No form, table, modal, toast, tooltip, or skeleton-loader styles.** The
   marketing site has none of these. The dashboard will need to invent them
   from the established radii / shadows / surface alphas — guidance in §5.4 and
   §8.
10. **Helix intro is heavy.** ~6.8 s timeline, runs on every page load by
    default. Suppressed with `ViewData["DisableHelixIntro"] = true`. For the
    dashboard, reserve it for first-paint of the login or root route, never
    on every navigation.
11. **No semantic icon set.** No Lucide / Heroicons / Feather references
    anywhere. The dashboard will need to pick one — Lucide is the safest match
    for the geometry on display.
12. **No dark/light toggle.** The marketing site is dark-only and has no system
    for switching. The dashboard's light in-product shell would be a net-new
    convention layered on top of (not replacing) the marketing dark canvas.
