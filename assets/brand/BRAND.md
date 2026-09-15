# CheatVision brand

The mark is a crosshair: a crimson ring with four ticks around a charcoal disc
carrying the CV letterforms. Everything in the app's look is derived from it.

## Files

| file | use |
|---|---|
| `cheatvision_mark.png` | the mark, 512×512, transparent background: rail lockup, canvas idle screen, README |
| `cheatvision.ico` | the same mark at 16 to 256 px for the window, taskbar and shortcuts |

The mark keeps its light outer rim: on the app's charcoal it reads as a glow,
on white it disappears. Give it clear space of at least a quarter of its width
and never recolour it. The wordmark is set in type beside the mark, CHEAT in
white and VISION in brand red, because the wordmark layer in the supplied
artwork was not recoverable (its alpha never exceeds 71 of 255). No tagline.

## Palette

Sampled from the artwork: ring and letters `#CB2B30`, disc `#1E2327`. The
rest is derived. Names are the constants in `src/ui/theme.py`.

| name | hex | where |
|---|---|---|
| `ACCENT` | `#CB2B30` | brand red: card titles, VISION, checked buttons, hovered edges, the straightness trace |
| `ACCENT_BRIGHT` | `#E63946` | hover on a checked button, slider handle hover |
| `ACCENT_EDGE` | `#8E1F24` | resting border of buttons, combos, checkboxes |
| `ACCENT_DIM` | `#4A1417` | hover and selection fill |
| `ACCENT_DEEP` | `#2E0D0F` | pressed |
| `PANEL_ALT` | `#1E2327` | the disc: cards and controls |
| `PANEL` | `#15181B` | rail, inset metric rows, table, scrollbars |
| `SURFACE` | `#111315` | top and bottom bars |
| `BACKGROUND` | `#0B0C0E` | window, canvas idle |
| `HAIRLINE` | `#2A3036` | borders, splitter, header underline |
| `BORDER` | `#3B434A` | scrollbar handles |
| `TEXT_PRIMARY` = `OK` | `#EDEFF1` | text, and the healthy state: LIVE AIM, GATE live |
| `TEXT_MUTED` | `#8C949C` | keys, legends, table headers, idle text |
| `ALERT` | `#FF4D57` | a flag: incident class, FREEZE, starved FEED, the flagged track box |
| `WARNING` | `#F2A93B` | amber warnings in the status text |
| `TRACE` | `#7F8A94` | the tremor trace and its fill |
| `VOD_ACCENT` | `#D4A64A` | reserved for VOD-mode accents |

Rules. Red is the brand, so it is spent on edges and titles, not fills, until
something is flagged. The healthy state is white, never green. Tracked players
on the video are steel `#D9DEE3` with charcoal labels; only a flagged track
turns alert red with a white label.

OpenCV draws in BGR: steel `(227, 222, 217)`, alert `(87, 77, 255)`, charcoal
chip `(27, 24, 21)`, hairline `(54, 48, 42)`, chip text `(212, 206, 200)`.

## Type

- Brand, card titles, SIGNAL state: **Bahnschrift** (ships with Windows 10 and 11), demibold, tracked 2 to 4 px. Falls back to Segoe UI. Set in code via `brand_font()` because stylesheets cannot express tracking.
- Body and controls: Segoe UI, bold at 10 to 11 px.
- Numbers: Cascadia Mono, then Consolas.
