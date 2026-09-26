# Theme

Theme source: `drpl-frontend/src/index.css` and `drpl-frontend/tailwind.config.js`.

## Compact token summary

- Color model: HSL CSS variables for background, foreground, card, popover, primary, secondary, muted, accent, destructive, border, input, and ring.
- Brand accent: blue in the current product; redesign uses emerald as the primary recommendation/success accent while retaining blue for navigation and data visualization.
- Typography: system sans-serif stack; compact enterprise scale with 12–16px body/UI labels and 20–32px screen/data emphasis.
- Radius: existing `--radius: 0.5rem`; redesign card radius 12–16px and controls 8–10px.
- Shadows: restrained, low-contrast elevation only on interactive/floating surfaces.
- Dark mode: class-based `.dark` variables with layered charcoal surfaces and accessible contrast.
- Breakpoints: Tailwind defaults; persistent desktop sidebar from `md`, multi-column dashboard from `lg`.

## Motion

Controls 150–200ms; cards 180–240ms; drawers/view changes 220–280ms. All motion respects `prefers-reduced-motion`.

