# Vitals Design System

This guide is the single reference for how Vitals looks and behaves. Anyone adding a page, card, modal, or Deck feature (Antigravity, Codex, or a human) should be able to match the existing UI from this file alone.

Source of truth for values: `frontend/src/styles.css` (`:root` and `:root[data-theme="dark"]`). If this document and the CSS disagree, the CSS wins and this document should be fixed.

## 1. Aesthetic direction

Vitals is a calm, instrument-grade control surface. Two materials carry the whole app:

- **Frosted glass** for the everyday app (Overview, Control, Scratchpad, Settings, dialogs). Surfaces are translucent panels with a bright top rim, a soft bottom rim, and a diffuse shadow. Content sits on top of an ambient tinted background or the user's wallpaper.
- **Dark hardware** for Vitals Deck. The Deck console is a physical dark device in every theme: a matte chassis, a recessed key well, and OLED keycaps that glow in their accent color. It never inherits the light glass surface.

Core principles:

1. **Numbers first.** Readings are the largest element in a card, always tabular (`font-variant-numeric: tabular-nums`), and never shift width when they update.
2. **Quiet chrome.** Card titles are small and muted; the data is loud. Borders are 1px and low-contrast.
3. **One accent.** System blue (`--blue` / `--link`) is the only interactive color. Semantic tones (green, orange, red, purple) describe data, not actions.
4. **Motion confirms, never decorates.** Short ease-out transitions (150–240 ms) on state change; no looping animation except the live-status pulse and the digital clock breathe, both disabled under `prefers-reduced-motion`.
5. **Bilingual by construction.** Every layout must survive `zh-CN` (short, dense) and `en-US` (long, wide) strings without clipping.

### Kept vs. rebuilt (v3.1 pass)

Kept, because it already worked well:

- The glass surface recipe (`.surface`), ambient background, and wallpaper overlay mechanics.
- `Segmented` with its container-local highlight (measured with `offsetLeft/offsetTop` inside the group). This avoids the Framer Motion `layoutId` drift inside height-changing modals and must stay this way.
- The `Modal` component built on native `<dialog>` with a modal stack, focus return, and Escape routing.
- The Bento grid model (`1×1` and `2×1` wide cards with `data-width="wide"` and `.preview-split`).
- The fullscreen Now Playing / AMLL lyrics and Focus Clock layouts.

Rebuilt or tightened:

- **Tokens:** added spacing, radius, motion, type-scale, accent-fill, focus-ring, and Deck chassis tokens. Hard-coded `#0074f3`, `cubic-bezier(...)` literals, and `18px` radii in shared rules now reference tokens.
- **Deck console:** the board no longer derives from `--surface`, which made it look washed out in Light mode. It now uses fixed `--deck-chassis-*` tokens, a recessed key well, and a glass reflection layer on each OLED key.
- **Bento cards:** larger 24px radius, muted small-caps-style titles, a hairline ring on the icon tile, and a little more breathing room.
- **Reduced motion:** a global `prefers-reduced-motion: reduce` guard now neutralizes every animation and transition, including those in feature CSS files that previously had no guard.
- **Numerals:** tabular figures are applied by default to every reading class, not only to elements that remembered to add `.numeric`.
- **Modal header:** layout moved from Tailwind utility classes into `.modal-header` so the primitive has no hidden utility dependency.

## 2. Token reference

All tokens live on `:root`. Dark mode overrides a subset on `:root[data-theme="dark"]`. The `system` theme resolves to one of these two at runtime (see `appearance.jsx`). Never hard-code a color that has a token.

### Color and surface

| Token | Light | Dark | Use |
|---|---|---|---|
| `--background` | `#f5f5f7` | `#000` | Page canvas |
| `--surface` / `--surface-rgb` | `#fff` / `255 255 255` | `#1c1c1e` / `32 32 36` | Opaque fills; `rgb(var(--surface-rgb) / α)` for glass |
| `--text` | `#181a23` | `#f5f5f7` | Primary text |
| `--muted` | `#686a76` | `#b0b0b6` | Secondary text, card titles |
| `--unit` | `#8e8e93` | (same) | Units after numbers (`%`, `°C`, `RPM`) |
| `--border` | 8% slate | 12% white | Card and divider hairlines |
| `--field-border` | 12% slate | 14% white | Inputs, buttons |
| `--track` | 12% gray | 9% white | Progress tracks, segmented background, quiet fills |
| `--selected` | 92% white | 16% white | Active segment pill |
| `--blue` / `--link` | `#007aff` / `#0069d9` | — / `#66aaff` | The single interactive accent |
| `--accent-solid` / `-hover` | `#0074f3` / `#0069df` | `#0a84ff` / `#2b95ff` | Filled primary buttons |
| `--on-accent` | `#fff` | `#fff` | Text on solid accent |
| `--green` `--orange` `--red` `--purple` | iOS system tones | (same) | Data tones only |
| `--error` | `#b9231e` | `#ff8178` | Error text |
| `--amber-text` / `--amber-fill` | | | Warnings and notices |
| `--focus-ring` | 64% blue | | `:focus-visible` outline |

Tone classes set `--accent` for a subtree: `.tone-blue`, `.tone-green`, `.tone-orange`, `.tone-red`, `.tone-purple`. Icon tiles and progress fills read `--accent`.

### Glass

| Token | Purpose |
|---|---|
| `--glass-alpha` | Surface opacity (0.76 light, 0.72 dark; the user's Appearance opacity slider overrides it) |
| `--glass-edge`, `--glass-edge-soft` | Bright top inner rim |
| `--glass-rim-bottom` | Dark bottom inner rim |
| `--glass-sheen`, `--glass-pill-sheen` | Diagonal and vertical highlight gradients |
| `--backdrop-blur`, `--backdrop-saturation` | Backdrop filter (user's "strength" slider overrides) |
| `--ambient-bg` | Tinted radial wash behind content when there is no wallpaper |
| `--shadow`, `--shadow-hover` | Three-layer elevation |
| `--shadow-modal` | Floating dialogs (Settings, AI Chat, base `.modal`) |
| `--shadow-thumb` | Slider thumbs, toggle knobs, QR tile |
| `--shadow-accent` | Glow under solid-accent fills (primary button, user chat bubble, send button) |
| `--well`, `--well-border` | Inset fill and border for inputs, textareas, nested panels |

The canonical surface is:

```css
background-color: rgb(var(--surface-rgb) / var(--glass-alpha));
background-image: var(--glass-sheen);
border: 1px solid var(--border);
box-shadow: inset 0 1px 0 var(--glass-edge), inset 0 -1px 0 var(--glass-rim-bottom), var(--shadow);
backdrop-filter: blur(var(--backdrop-blur)) saturate(var(--backdrop-saturation));
```

Use the `.surface` class rather than copying this.

### Wallpaper mode

`.app-frame.has-wallpaper` disables `--ambient-bg`, puts the image in `.wallpaper`, and darkens or lightens it with `--overlay-opacity` over `--backdrop-rgb`. Under a wallpaper:

- Every `.surface` and `.app-header` keeps a backdrop blur, and falls back to 88% opacity when `backdrop-filter` is unsupported.
- Free-floating text that is not on a surface (page headings, `.source-note`) gets its own small glass chip. Any new free-floating text must do the same, or it will be unreadable on a busy image.

### Deck hardware

| Token | Value | Use |
|---|---|---|
| `--deck-chassis-top` / `-bottom` | `#1d222c` / `#0b0e14` | Console body gradient |
| `--deck-chassis-edge` | 9% white | Console border |
| `--deck-accent` | set by `[data-deck-color]` | Key glow color |

Deck palette (`data-deck-color`): `slate #8290a6`, `blue #3b8ef5`, `teal #22b8c7`, `green #2ec273`, `amber #f0972c`, `pink #ec68a5`, `purple #9b6ef3`, `red #ef5868`. Add new colors here and in `deck.css` together.

## 3. Typography, numbers, spacing, radius, motion

**Font stack:** `-apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", "Microsoft YaHei UI", sans-serif`. Do not add web fonts; native fonts keep CJK and Latin metrics consistent per platform.

| Token | Size | Use |
|---|---|---|
| `--text-caption` | 12px | Labels, `dt`, timestamps |
| `--text-body` | 14px | Default UI text, buttons |
| `--text-title` | 16px | Card and section titles |
| `--text-heading` | clamp(17–21px) | Panel headers |
| `--text-display` | clamp(30–42px) | Page `h1` |

Weights: 650 for buttons and labels, 700–745 for titles, 760–780 for readings and page headings. Large readings use negative tracking (`-0.04em` to `-0.055em`); units after them drop to `--unit` color, 600 weight, and normal tracking.

**Numbers:** every reading class gets tabular figures automatically. For anything new that shows a changing number, add `.numeric`. Format through `format.js` (`measure`, `number`, `time`, `sleepTime`) and `localizedNumber` / `localizedDate` from `i18n/messages.js`, never `toFixed` plus string concatenation. Missing values render as `—`.

**Spacing (8px base):** `--space-xs 4`, `--space-sm 8`, `--space-md 12`, `--space-lg 16`, `--space-xl 24`, `--space-2xl 32`. Grid gaps are 12px (Bento) to 16px (panels); card padding is 14–20px; mobile drops one step.

**Radius:** `--radius-sm 8` (inputs, small chips), `--radius-md 12` (buttons, icon tiles, list rows), `--radius-lg 18` (panels, dialogs), `--radius-xl 24` (Bento cards, detail dialogs, AI chat), `--radius-pill 999` (segments, badges, pills). Deck keys use their own clamp(18–24px); the console uses 30px.

**Motion:**

| Token | Value | Use |
|---|---|---|
| `--ease-out` | `cubic-bezier(.22, 1, .36, 1)` | Default for everything |
| `--ease-in-out` | `cubic-bezier(.25, .75, .25, 1)` | Layout shifts (lyrics split) |
| `--duration-fast` | 150ms | Hover, color |
| `--duration-standard` | 180ms | Toggles, small transforms |
| `--duration-modal` | 180ms | Dialog enter and exit (matches `modalDuration` in `ui.jsx`) |
| `--duration-page` | 220ms | Page and tab transitions |

Press feedback is `scale: 0.97` (Framer `whileTap` on `Button` and `IconButton`). Cards lift `translate: 0 -3px` on hover only on `(hover: hover)` devices. Deck keys travel down 4px on press.

**Reduced motion:** a global rule in `styles.css` collapses all animation and transition durations when `prefers-reduced-motion: reduce` is set. JS-driven motion must also check `useReducedMotion()` (as `Modal` and `Segmented` do).

**Scrollbars:** thin, transparent track, thumb at 18% `--text` (34% on hover). This is global; do not restyle per component.

## 4. Component recipes

### `ui.jsx` primitives

| Component | Use | Notes |
|---|---|---|
| `Button` | Any text button | Adds `.button` and press scale. Variants by class: `primary-button`, `quiet-button`, `delete-button`. |
| `IconButton` | Icon-only button or link | `label` is required and becomes `aria-label` and `title`. Pass `href` for a link. |
| `Segmented` | 2–5 mutually exclusive options; `tabs` for tablists | Highlight is measured inside the group. Never replace it with `layoutId`. Options use `get label()` so they re-translate on locale change. |
| `Modal` | Every dialog | Native `<dialog>` and stack-aware. Must sit inside `<AnimatePresence>`. Needs a unique `id`; title becomes `aria-labelledby`. Put extra header controls in `headerActions`. |
| `DetailLink` | Makes a whole card open a detail dialog | Absolutely fills the card; the card needs `.detail-tile`. |
| `DetailValues` | Label/value grid inside dialogs | Values are tabular automatically. |
| `ProgressBar` | Linear meter | `tone` picks the accent; non-finite values hide it from assistive tech. |

### Bento card (Overview)

```jsx
<section className="surface overview-widget detail-tile" data-widget="mywidget" data-width={wide ? 'wide' : undefined}>
  <header>
    <span className="metric-icon tone-purple"><Icon size={16} aria-hidden="true" /></span>
    <h2>{t('我的卡片')}</h2>
  </header>
  <div className="widget-content">
    {wide
      ? <div className="preview-split"><div className="preview-primary">…</div><div className="preview-secondary">…</div></div>
      : <p className="widget-value numeric">42<small> {t('单位')}</small></p>}
  </div>
  <DetailLink title={t('我的卡片')} onClick={open} />
</section>
```

Rules:

- Grid rows are a fixed 184px (168px on phones). Content must fit or truncate; never let a card grow.
- `1×1` shows one hero value and at most two supporting lines. `2×1` uses `.preview-split` with the hero on the left and a list or chart on the right.
- Register the card in `overviewCards.js` so it shows up in the Settings ordering and width controls.
- Fetch with `useOverviewResource(path, intervalMs)` and render `—` while loading. Do not show spinners inside cards.

### Panels (Control, Monitor)

`<section className="surface panel">` with `<div className="panel-header"><h2>…</h2>…</div>`. Two-column layouts use `.host-control-grid` or `.media-control-grid` and collapse to one column at 720px.

### Deck keys

A key is `button.deck-key` with `data-deck-color`, plus optional `data-active`, `data-selected`, `data-has-cover`, and `data-empty`. Layering, bottom to top: cover image (z 0), glass reflection `::before` (z 1), cover scrim `::after` (z 1), glyph (z 1), value and name (z 2), LED (z 3). Labels clamp to two lines. Live keys show `.deck-key-value` above `.deck-key-name`. Keep keys square (`aspect-ratio: 1`) at every grid size, `3×2` through `6×3`.

### Where styles live

| File | Contents |
|---|---|
| `styles.css` | Tokens, base elements, primitives, Overview, Monitor, fullscreen views |
| `views.css` | v3.1 restyle layer for Control, Scratchpad, Settings, AI Chat, plus shared form controls. Imported after `styles.css` in `main.jsx`, so it wins by source order. |
| `deck.css`, `deckPlugins.css` | Vitals Deck console, editor, plugin store |
| `setupWizard.css` | First-run wizard (lazy-loaded, styled in its own file so it isn't overridden by load order) |
| `overviewWidgets.css`, `expensesWidget.css`, `launchers.css`, `desktopChrome.css`, `ScreenshotControl.css` | Feature-scoped rules |

Put view-level overrides in the file that owns the view. Don't add new global overrides to `styles.css` after `views.css` has been introduced.

### Inputs (the "well")

Every text input, select and textarea uses the well recipe: `--well` background, `--well-border` 1px border, `--radius-sm` (12–18px for big composers), a 1px inner top shadow, and a focus state of a blue border plus a 3px `color-mix(--blue 22%)` ring instead of the outline. `views.css` applies this to all known form containers; new forms should use `.widget-form`, `.host-setup-field` or `.settings-row` to inherit it.

### Tiles and chat

- **Action tiles** (quick controls, launchers): 44px tall, `--radius-md`, `--track` fill, no border; hover adds 10% text tint; pressed/active uses a 14% blue fill with a 28% blue hairline. Destructive tiles use `--error` text on a 10% red fill.
- **Chat bubbles:** assistant bubbles are `--track` with an 18/18/18/6 radius; user bubbles are solid `--accent-solid` with `--on-accent` text and an 18/18/6/18 radius. The send button is a round solid-accent button.

### Modals and forms

- Standard dialog: default `.modal` (480px wide). Detail dialog: `className="overview-dialog detail-dialog"` (695px, or 850px for weather and health). Fullscreen: `.monitor-dialog`, `.clock-dialog`, `.now-playing-dialog`.
- Forms: `.widget-form` with `<label>` wrapping the text and the input. Paired fields use `.widget-form-pair`, which collapses at 370px. Inputs are at least 38px tall and use `font-size: 16px` on touch (prevents iOS zoom).
- Settings rows: `.settings-row` (label left, control right; wraps when it holds a `Segmented`). Switches use `<input type="checkbox" role="switch" className="toggle-switch">`.

### Empty, loading, error

- Empty: `.section-empty` (muted 13px) or `.overview-empty` (icon plus text plus an optional action link). Say what will appear and how to make it appear.
- Loading: `—` in values; `.chart-loading` for chart slots; `Suspense` fallbacks are plain muted text.
- Error: `.feedback.error` with `role="alert"`, placed directly below the control that failed. Notices that need attention but are not errors use `.overview-notice` (amber).

## 5. Responsive and bilingual guardrails

Breakpoints used across the app: **1120** (weather three-column), **1000**, **850** (Bento to 2 columns), **720** (most grids to 1 column, Deck editor becomes a bottom sheet), **700** (header tabs wrap to a second row, AI chat becomes a bottom sheet), **620** (Bento to 1 column), **530**, **440**, **390/370/365** (tightest phone tweaks).

Platform chrome:

- **Windows** (`data-desktop="windows"`): header reserves 146px on the right for `.desktop-caption-controls` (— □ ✕, 45px each, close turns `#c42b1c`). The header and its inner grid carry `data-tauri-drag-region`.
- **macOS** (`data-desktop="macos"`): header reserves 94px on the left for the traffic lights; the Deck adds 42px top padding.
- **iOS/iPadOS:** respect `env(safe-area-inset-*)` on every fixed or fullscreen surface. Use `100dvh`, not `100vh`.

Bilingual rules:

1. Every user-visible string goes through `t('中文原文')`. The Chinese source string is the key. Add the English translation to the message catalog in the same change.
2. Static option arrays use getters (`get label() { return t('…'); }`) so they update without a reload.
3. Design for English width. English strings run 1.5–2.5× wider than Chinese. Test both locales at 375px, 768px, and 1280px.
4. Text containers get `min-width: 0` plus either `overflow-wrap: anywhere` (body) or ellipsis (single-line titles and names). Buttons in rows either wrap (`white-space: normal`, as in `:root:lang(en) .quick-control-row .button`) or sit in an `auto-fit` grid.
5. Never set a fixed width on a button or pill that contains text.

## 6. Do / Don't checklist

**Do**

- [ ] Use `.surface` for every card and panel, and `Modal` for every dialog.
- [ ] Reference tokens (`var(--…)`) for color, radius, shadow, and motion.
- [ ] Make readings the largest text in the card and give them tabular figures.
- [ ] Use the `tone-*` classes for data color and `--link` for anything clickable.
- [ ] Wrap every string in `t()` and check both locales at phone, iPad, and desktop widths.
- [ ] Add `min-width: 0` to grid and flex children that contain text.
- [ ] Give icon-only buttons a `label`.
- [ ] Respect `prefers-reduced-motion` in any JS animation (`useReducedMotion`).
- [ ] Keep Deck UI on the dark chassis tokens in all themes.
- [ ] Run `npm --prefix frontend run build` before finishing.

**Don't**

- [ ] Don't hard-code hex colors, `cubic-bezier` literals, or one-off shadows in shared rules.
- [ ] Don't use Framer Motion `layoutId` for selection highlights; it drifts inside modals that change height.
- [ ] Don't add a second accent color for actions.
- [ ] Don't let Bento cards change height or show spinners.
- [ ] Don't place free-floating text over a wallpaper without a glass chip.
- [ ] Don't use `100vh` or ignore safe-area insets on fullscreen views.
- [ ] Don't derive Deck console or key colors from `--surface` or `--background`.
- [ ] Don't add web fonts or icon sets other than `lucide-react` (Deck icons come from `deckIcons.js`).
- [ ] Don't change API paths, SSE/WebSocket payload handling, or the telemetry sampling hooks from the UI layer.
