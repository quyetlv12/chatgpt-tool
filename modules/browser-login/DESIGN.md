# Design System: shoptaikhoan auto tool

## 1. Visual Theme & Atmosphere

An operational cockpit for account automation: calm, dark, precise, and quietly premium. The density is balanced (6/10), with an offset asymmetric dashboard (variance 7/10) and fluid but restrained motion (5/10). The interface should feel like a secure control room rather than a marketing landing page.

## 2. Color Palette & Roles

- **Night Canvas** (#080F1F) — application background and deep page wells.
- **Slate Surface** (#101A2D) — cards, navigation, and elevated panels.
- **Slate Raised** (#17243A) — inputs, table headers, hover states.
- **Cloud Ink** (#F2F6FC) — primary text and headings.
- **Steel Mist** (#8C9AB2) — descriptions, labels, metadata.
- **Hairline Border** (#223451) — structural 1px lines and card boundaries.
- **Signal Blue** (#4D8DFF) — the single accent for active navigation, focus rings, and primary actions.
- **Success Green** (#24C98A) — positive status only; use as a semantic status color, never as a decorative gradient.
- **Warning Amber** (#F2B84B) — caution and pending states.
- **Error Coral** (#F06B72) — failures and destructive actions.

Do not use pure black, purple/neon glow, or multiple competing accent gradients.

## 3. Typography Rules

- **Display/body:** Geist, "Avenir Next", system sans fallback; compact, track-tight headings and relaxed 1.5 leading for copy.
- **Mono:** "SF Mono", "JetBrains Mono", Consolas; all counts, timestamps, URLs, and log lines.
- Minimum body size is 14px. Headings use weight and contrast for hierarchy, not oversized type.
- Inter and generic serif fonts are not used in this dashboard.

## 4. Component Stylings

- **Sidebar:** fixed 272px rail with a faint right hairline and a low-contrast radial wash. Navigation rows are 44px touch targets; the active row uses a blue-tinted surface and a 3px accent edge.
- **Topbar:** 64px utility row with breadcrumb, server pill, and account identity. Keep it visually quiet so the workflow owns attention.
- **Cards:** 16px radius, Slate Surface fill, 1px Hairline Border, and a soft tinted shadow. Card headers use a compact eyebrow and title pair.
- **Inputs:** labels above controls, 12px radius, Slate Raised fill, focus ring in Signal Blue, no floating labels.
- **Buttons:** 44px minimum height, tactile translateY(-1px) on hover and translateY(0) on press. Primary is solid Signal Blue; destructive is Coral-tinted outline.
- **Tables/logs:** dense rows with clear separators, monospace metadata, and inline semantic status chips.
- **Loaders:** progress bars and skeleton blocks matching the final layout; avoid generic spinners.

## 5. Layout Principles

Use a fixed sidebar plus a flexible main shell. The ChatGPT Web screen uses a CSS Grid split: a 0.82fr control column and a 1.35fr results column, with compact stat tiles above the right column. Keep content in a 1500px max-width frame and use generous 24–32px internal padding. Never overlap elements or rely on percentage calc hacks.

Below 768px, collapse the sidebar into a horizontal mobile rail, stack all grids to one column, and keep every control at least 44px high with no horizontal overflow.

## 6. Motion & Interaction

Use short ease-out transforms and opacity transitions. Active navigation and cards may reveal with a 220ms stagger; progress/status dots use a quiet 2s pulse. Animate transforms and opacity only, never layout dimensions. Motion should reinforce operational state, not compete with it.

## 7. Anti-Patterns (Banned)

- No emojis as interface icons; use CSS/icon glyphs and semantic labels.
- No Inter, generic serif, pure black, neon outer glows, or oversaturated gradients.
- No fake names, fake metrics, or decorative placeholder results.
- No three-equal-card marketing rows, centered hero layouts, or filler copy such as “scroll to explore”.
- No overlapping content, horizontal mobile scroll, or custom cursors.
