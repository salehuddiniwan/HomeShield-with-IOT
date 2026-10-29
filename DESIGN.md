---
name: HomeShield IoT
description: A calm, tactile home console. Soft raised controls on one porcelain surface.
colors:
  porcelain: "#e6ebf2"
  porcelain-deep: "#dce3ec"
  porcelain-well: "#dfe5ee"
  raised-light: "#eff4fa"
  raised-shade: "#e0e6ef"
  slate-ink: "#1e293b"
  slate-ink-2: "#43526a"
  slate-ink-3: "#57667e"
  royal-blue: "#2d68fe"
  royal-blue-grad-start: "#2f64ec"
  royal-blue-grad-end: "#1d4ed8"
  royal-blue-text: "#1d4ed8"
  royal-blue-wash: "rgba(45,104,254,0.11)"
  menu-blue-start: "#2656dc"
  menu-blue-end: "#1b42bb"
  emerald: "#16a34a"
  emerald-grad-start: "#34d399"
  emerald-text: "#0c662e"
  emerald-wash: "rgba(22,163,74,0.12)"
  emerald-line: "rgba(22,163,74,0.35)"
  amber: "#d97706"
  amber-text: "#874a04"
  amber-wash: "rgba(217,119,6,0.12)"
  amber-line: "rgba(217,119,6,0.35)"
  crimson: "#e11d48"
  crimson-text: "#ad1038"
  night-ground: "#1a1f2b"
  night-surface-2: "#232a38"
  night-well: "#161a24"
  night-raised-light: "#222a39"
  night-raised-shade: "#191e2a"
  night-ink: "#e6ebf4"
  night-ink-2: "#aeb9cc"
  night-ink-3: "#8f9bb1"
  night-royal-blue-text: "#93b1ff"
  night-amber-text: "#f1b45a"
  night-emerald-text: "#5fd394"
  night-crimson-text: "#ff8aa0"
  night-menu-blue-start: "#1e3a8a"
  night-menu-blue-end: "#172554"
  crimson-ink-strong: "#9f1239"
  crimson-ink-deep: "#881337"
  crimson-wash: "rgba(225,29,72,0.10)"
  crimson-line: "rgba(225,29,72,0.32)"
  crimson-blush-light: "#fff1f3"
  crimson-blush-shade: "#f5dfe4"
  amber-warm-light: "#f8f4ec"
  amber-warm-shade: "#ede6d8"
  emerald-home-light: "#e3f5ea"
  emerald-home-shade: "#cfeedb"
  screen-night: "#0f172a"
  screen-slate: "#1e293b"
  screen-text: "#cbd5e1"
  screen-danger: "#ff6b85"
  device-lamp: "#facc15"
  device-lamp-ink: "#713f12"
  device-air: "#38bdf8"
  device-air-ink: "#082f49"
  device-power: "#a78bfa"
  device-power-ink: "#3b0764"
  device-open: "#fb923c"
  device-open-ink: "#431407"
  device-sound: "#fb7185"
  device-sound-ink: "#4c0519"
  hairline: "rgba(163,177,198,0.28)"
  hairline-mid: "rgba(148,163,184,0.42)"
  specular-edge: "rgba(255,255,255,0.72)"
typography:
  display:
    fontFamily: "Chakra Petch, sans-serif"
    fontSize: "clamp(2.5rem, min(6vw, 10vh), 4.75rem)"
    fontWeight: 700
    lineHeight: 1
    fontFeature: "tnum"
  headline:
    fontFamily: "Rubik, system-ui, -apple-system, sans-serif"
    fontSize: "1.6rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  page-title:
    fontFamily: "Rubik, system-ui, -apple-system, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Rubik, system-ui, -apple-system, sans-serif"
    fontSize: "1rem"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  data:
    fontFamily: "Rubik, system-ui, -apple-system, sans-serif"
    fontSize: "1.65rem"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
    fontFeature: "tnum"
  body:
    fontFamily: "Rubik, system-ui, -apple-system, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.55
  ui:
    fontFamily: "Rubik, system-ui, -apple-system, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 500
    lineHeight: 1.45
  meta:
    fontFamily: "Rubik, system-ui, -apple-system, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.45
  label:
    fontFamily: "Chakra Petch, sans-serif"
    fontSize: "0.6875rem"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "0.06em"
    fontFeature: "tnum"
  numeral:
    fontFamily: "Chakra Petch, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "0.4px"
    fontFeature: "tnum"
rounded:
  tag: "6px"
  field: "10px"
  button: "11px"
  control: "12px"
  screen: "14px"
  tile: "16px"
  card: "18px"
  dialog: "22px"
  modal: "24px"
  pill: "999px"
spacing:
  hair: "4px"
  xs: "8px"
  sm: "12px"
  md: "16px"
  grid: "18px"
  lg: "20px"
  content-y: "22px"
  content-x: "26px"
components:
  button:
    backgroundColor: "{colors.raised-light}"
    textColor: "{colors.slate-ink}"
    typography: "{typography.meta}"
    rounded: "{rounded.button}"
    padding: "8px 15px"
  button-hover:
    textColor: "{colors.royal-blue-text}"
  button-primary:
    backgroundColor: "{colors.royal-blue}"
    textColor: "#ffffff"
    typography: "{typography.meta}"
    rounded: "{rounded.button}"
    padding: "8px 15px"
  button-danger:
    backgroundColor: "{colors.crimson-blush-light}"
    textColor: "{colors.crimson-text}"
    rounded: "{rounded.button}"
    padding: "8px 15px"
  button-danger-hover:
    backgroundColor: "{colors.crimson}"
    textColor: "#ffffff"
  button-sm:
    rounded: "{rounded.field}"
    padding: "6px 12px"
  input:
    backgroundColor: "{colors.porcelain-well}"
    textColor: "{colors.slate-ink}"
    typography: "{typography.ui}"
    rounded: "{rounded.button}"
    padding: "9px 12px"
  sidebar:
    backgroundColor: "{colors.menu-blue-start}"
    textColor: "#ffffff"
  nav-item:
    backgroundColor: "transparent"
    textColor: "rgba(255,255,255,0.9)"
    typography: "{typography.ui}"
    rounded: "{rounded.control}"
    padding: "10px 14px"
  nav-item-active:
    backgroundColor: "#ffffff"
    textColor: "{colors.royal-blue-text}"
  card:
    backgroundColor: "{colors.raised-light}"
    textColor: "{colors.slate-ink}"
    rounded: "{rounded.card}"
    padding: "18px 20px"
  pill-tag-success:
    backgroundColor: "{colors.emerald-wash}"
    textColor: "{colors.emerald-text}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "3px 8px"
  pill-tag-warn:
    backgroundColor: "{colors.amber-wash}"
    textColor: "{colors.amber-text}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "3px 8px"
  pill-tag-danger:
    backgroundColor: "{colors.crimson-wash}"
    textColor: "{colors.crimson-text}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "2px 8px"
  segmented-option-selected:
    backgroundColor: "{colors.raised-light}"
    textColor: "{colors.royal-blue-text}"
    typography: "{typography.label}"
    rounded: "{rounded.field}"
    padding: "6px 12px"
  camera-screen:
    backgroundColor: "{colors.screen-night}"
    textColor: "{colors.screen-text}"
    rounded: "{rounded.screen}"
  dialog:
    backgroundColor: "{colors.raised-light}"
    textColor: "{colors.slate-ink}"
    rounded: "{rounded.modal}"
    padding: "26px 28px"
---

# Design System: HomeShield IoT

## Overview

**Creative North Star: "The Porcelain Console"**

HomeShield reads as one well-made household device: a single porcelain-blue ground (#e6ebf2) that is also the surface every card, button and pill is pressed out of. Apart from the royal-blue menu down the left, nothing sits on a contrasting panel; depth comes only from paired soft shadows, a cool shade to the lower right and a white light to the upper left, so controls look moulded from the same material. Raised means "this is a thing you can touch or read as a unit"; inset means "this is a well you type into, a track, or the chosen option in a group".

The world is calm on purpose. The household verdict, metric cards and incident rows use the same soft surface, and colour enters only as a signal: royal blue for the brand and the one primary action, emerald for "on / fine", amber for "look at this", crimson for "act now". Camera video is the one dark object in the system, always held in a recessed dark rounded screen, so the feed reads as a screen set into the console and never as a security-monitor wall. This world replaces the old dark navy departure-board look and comes from the user's pinned Claude Design export "HomeShield Dashboard" (Neumorphism Soft UI).

Density is moderate: a fixed 236px sidebar, a 64px top bar, 18px card radii and 16 to 20px gaps. On phones the sidebar becomes a drawer, the top-bar chips fold into one status pill, and the Live page keeps incidents at the bottom, after the cameras.

**Key Characteristics:**
- One porcelain ground that doubles as the surface; the only contrasting panels are the royal-blue menu and the blue sign-in panel.
- Depth by paired light/dark soft shadows only: raised for cards and buttons, inset for fields, tracks and pressed states.
- Royal blue (#2d68fe) for the brand mark, the primary action and focus; emerald, amber and crimson only as signals.
- Words in a signal colour always use the deepened text partner (4.5:1 or more), never the fill.
- Camera video always sits in a dark recessed rounded screen.
- Rubik in sentence case for words; Chakra Petch for numerals, tags and table heads.
- One authored motion moment: the split-flap flip.

## Colors

A cool porcelain neutral carries everything; four saturated hues appear only as brand and status signals, each paired with a deeper partner for text.

### Primary
- **Royal Blue** (royal-blue): the logo tile (sign-in and favicon, behind a white mark), the active-nav bar, slider fills, radio marks, focus outlines, selection tint and the caret. As a fill it is the gradient royal-blue-grad-start to royal-blue-grad-end (135deg) on the primary button, Start monitoring, the avatar, slider thumbs, the first add-step badge, and the sign-in panel behind the card (two-column layout only; phones keep porcelain).
- **Royal Blue Ink** (royal-blue-text): every word or icon stroke that is "blue": active nav labels, link buttons, selected tabs and segments, slider values, GPIO numbers, timeline times, inline icons in inset wells.
- **Royal Blue Wash** (royal-blue-wash): the background of blue tags (source tag, admin role, "up" badge) and the selected table row. On that row tertiary words step up to slate-ink-2 and the "up" badge takes a raised face, so both keep 4.5:1.

### Secondary (status signals)
- **Emerald** (emerald) with **Emerald Ink** (emerald-text): fill for online dots, the "on" switch, signal bars, run-success dots and the Home mode pill (emerald-grad-start to emerald); ink for "on", "live", "seen now", high-confidence readings and safe-zone tags.
- **Warm Amber** (amber) with **Amber Ink** (amber-text): fill for attention lamps, warning dots, the camera alert strip and the camera-alert outline; ink for warnings, mid-confidence readings, open incidents, caps-lock and pin-why notes.
- **Coral Crimson** (crimson) with **Crimson Ink** (crimson-text): fill for critical lamps, the danger badge, the live feed badge and danger hover; ink for critical incident labels, danger readings, offline states and destructive button text. **Crimson Blush** (crimson-blush-light to crimson-blush-shade, 145deg) is the raised surface of an alerting card or danger button.
- Each hue also has a **wash** (10 to 12% alpha) for tag and note backgrounds and a **line** (32 to 35% alpha) for the border of a tinted note or alerting card.
- **Warm Porcelain** (amber-warm-light to amber-warm-shade, 145deg) is the face of the attention verdict, and **Home Green** (emerald-home-light to emerald-home-shade, 145deg) the face of the At home mode button. With crimson blush they are the only tinted faces; night swaps all three for dark tints of the same hues.

- **Menu Blue** (menu-blue-start to menu-blue-end, top to bottom): the ground of the left menu; night deepens it to night-menu-blue-start to night-menu-blue-end. Words on it are white (6.09:1 at its lightest), 90% white for idle items and 82% white for section labels and small captions, all 4.6:1 or more. Frosted white (14%) is the raised face on it and a navy well (rgba(8,20,70,0.22)) the inset face, so only pure white sits on the frost and smaller words sit in the wells.

### Neutral
- **Porcelain** (porcelain): the page ground and the base surface. It is also the background of the top bar and sticky incident actions.
- **Raised Light to Raised Shade** (raised-light to raised-shade, 145deg): the gradient face of every raised object.
- **Porcelain Well** (porcelain-well): the face of every inset object (fields, tracks, segmented groups, fact tiles, inset rows). **Porcelain Deep** (porcelain-deep) is the unfilled slider track and disabled submit.
- **Slate Ink** (slate-ink): primary text, values and headings. **Slate Ink 2** (slate-ink-2): secondary text, descriptions, field labels, idle nav. **Slate Ink 3** (slate-ink-3): tertiary text, table heads, timestamps, placeholders, idle dots.
- **Hairline** (hairline) and **Hairline Mid** (hairline-mid): 1px dividers inside cards and dashed empty-slot borders. **Specular Edge** (specular-edge): the 1px white rim on every raised object.
- **Screen Night to Screen Slate** (screen-slate to screen-night, 145deg): the dark camera screen. **Screen Text** (screen-text) and **Screen Danger** (screen-danger) are the only text colours allowed on it.

### Named Rules
**The Deepened Partner Rule.** Words take a text token: slate-ink, slate-ink-2, slate-ink-3, royal-blue-text, emerald-text, amber-text or crimson-text, all at 4.5:1 or more on the ground, the wells and their own wash. Fills, dots, borders and shadows take royal-blue, emerald, amber or crimson. Never colour words with a fill token.

**The Dark Screen Rule.** Camera video, snapshots and clips always sit in a dark rounded screen (screen-slate to screen-night). The dark is reserved for video; no other surface goes dark. Text on the screen uses screen-text, and danger text on it uses screen-danger.

**The Royal Mark Rule.** The HomeShield logo is a white mark on a solid royal-blue (#2d68fe) rounded tile everywhere it appears, in day and night alike. On the royal-blue menu the tile is frosted white over the menu blue, with a white rim, so it doesn't sink into the blue behind it. It is never recoloured to a status hue, a gradient or a dark variant.

### Night
HomeShield follows the phone or laptop's dark mode (`prefers-color-scheme: dark`). Night is the same console after dark, not an inverted day: a deep slate ground (night-ground, also the card surface), raised surfaces from a slightly lighter slate (night-raised-light to night-raised-shade) lit by a faint slate highlight and a near-black shade, pressed wells in night-well, and light inks for words (night-ink, night-ink-2, night-ink-3, and the night signal inks), all 4.6:1 or more on every night surface and wash. Camera screens go a step darker than the page. Signal fills, the primary gradient, the device colours and the blue sign-in panel stay as they are by day, and the left menu deepens from royal blue to navy (night-menu-blue-start to night-menu-blue-end); a lit device card keeps the day's dark inks and calms towards porcelain, never towards slate. In the stylesheet every light literal is a variable whose fallback is its day value, so the day theme is unchanged and only night sets the names. A round theme button at the top right of the top bar (on phones, at the top right of the slide-out menu, because the phone top bar is full) opens a small menu: Day, Night, or Same as this device (the default). The choice is remembered per browser and applied before the page paints, so night can be forced on a day-mode device and the other way round.

## Typography

**Display / Numeral Font:** Chakra Petch (400, 600, 700; sans-serif fallback), self-hosted
**Body Font:** Rubik (variable 400 to 700; system-ui fallback), self-hosted

**Character:** Rubik is round and friendly for words in sentence case; Chakra Petch's squared, instrument-like figures carry the numbers, tags and column heads, so the console reads like a device with a readout rather than a monitoring wall.

### Hierarchy
- **Display** (Chakra Petch 700, clamp(2.5rem, min(6vw, 10vh), 4.75rem), line-height 1, tabular): the sign-in split-flap clock only.
- **Headline** (Rubik 700, 1.6rem, -0.02em): the sign-in card title.
- **Page title** (Rubik 700, 1.25rem, -0.01em, sentence case): the top-bar page name; drops to 1rem on phones.
- **Title** (Rubik 700, 0.95 to 1.1rem, -0.01em): section titles, verdict line, card and dialog headings.
- **Data** (Rubik 700, 1.65rem, -0.02em, tabular): metric card values. Device and sensor values, incident counts and phone metrics share Data small (1.25rem); the sensor chart value (2.4rem, 2rem on phones) and the sign-in flaps are the only one-off display sizes.
- **Body** (Rubik 400, 0.875rem, line-height 1.55): running text; descriptions cap at 52 to 60ch.
- **UI** (Rubik 500 to 600, 0.8125rem): nav items, row cells, tab labels, most control text.
- **Meta** (Rubik 600, 0.75rem): button text, captions, secondary lines.
- **Label** (Chakra Petch 700, 0.6875rem, 0.06em, uppercase): table and board column heads, state tags, segmented options, sub-group dividers. Form field labels use the same size and case in Rubik 600.
- **Numeral** (Chakra Petch 600 to 700, 0.75 to 0.8125rem, 0.4px, tabular): clock times, dates, readings, RSSI, chip values.

### Named Rules
**The Two Voices Rule.** Rubik speaks the words, in sentence case; Chakra Petch speaks the numbers and the short uppercase tags. Headings are never set in Chakra Petch, and paragraphs are never uppercase.

**The Steady Figures Rule.** Every live number (metrics, clocks, readings, counts) uses tabular figures so values do not jitter as they update.

## Layout

A fixed app shell: a 236px sidebar at left, a 64px top bar, and content padded 22px by 26px. Content is built from grids of raised cards: the Live page's weather row (16px gap), metric cards in a row on the Devices page (18px gap), device and sensor grids of auto-fill tiles (min 196 to 220px, 16px gap), and two-column work layouts with a fixed side column (310 to 360px) beside a fluid main column. On Devices and Automations the shorter column keeps pace with the longer one: it sticks 16px under the top bar, or, when it is taller than the window, scrolls until its end is in view and sticks there, so it never leaves a void and nothing in it is out of reach (desktop only; the columns stack below 1100px and 1200px). On Live, the hero camera takes min(720px, 58%) beside a wrapping column of thumbnails and the Add camera panel, with a draggable inset divider between them. On the Sensors page the chart card stretches to the height of the thresholds and responses column beside it, and the chart (or its table) fills the card, never shorter than 300px (220px on phones).

Breakpoints are 1200px and 1100px (side columns stack), 900px (sidebar becomes an off-canvas drawer behind a burger, metrics go two-up, settings nav becomes a horizontal scroller), and 600px (phone: top-bar chips fold into one status pill, board rows become stacked cards, Live keeps weather and detectors, then cameras, with incidents at the bottom, as on desktop, whatever the verdict says; Devices puts the scenes in one row you swipe (edge to edge, the next card peeking, Edit scenes at its end) and the room tabs on one line that scrolls, dialogs become bottom sheets). On coarse pointers every control is at least 44px tall and fields use 1rem text (segmented device buttons, the automatic-response rows, and the person card's bin through a 4px wider hit area included). A ticked automatic-response step saves at once and its toast names it in quotes with its level; a step turned off offers Undo. The sign-in page is a two-column board and card that collapses into one stacked column below 720px.

## Elevation & Depth

Depth is entirely neumorphic: every object is moulded from the porcelain ground and lit from the upper left. A raised object pairs a cool shade below right with a white light above left, plus a 1px inner specular highlight and a 1px white rim. An inset object reverses the pair inside its well. The camera screen is the deepest inset in the system, a dark well with a white lower-right lip. Colour never replaces depth; tinted surfaces (crimson blush, amber wash) still carry the same shadows.

### Shadow Vocabulary
- **Raised** (`box-shadow: 7px 7px 15px rgba(163,177,198,0.58), -7px -7px 15px rgba(255,255,255,0.92), inset 1px 1px 0 rgba(255,255,255,0.78)`): cards, metric cards, tables, verdict band.
- **Raised small** (`box-shadow: 4px 4px 9px rgba(163,177,198,0.52), -4px -4px 9px rgba(255,255,255,0.88), inset 1px 1px 0 rgba(255,255,255,0.75)`): buttons, chips, pills, active nav, selected tab or segment, logo tile.
- **Raised large** (`box-shadow: 12px 12px 26px rgba(154,169,192,0.62), -10px -10px 24px rgba(255,255,255,0.95), inset 1px 1px 0 rgba(255,255,255,0.85)`): card hover lift, sign-in card on phones, dialogs. On the blue sign-in panel the card instead takes a plain soft drop shadow (`0 28px 56px -16px rgba(15,23,42,0.50), 0 4px 12px rgba(15,23,42,0.18)`), since the light/shade pair only works on porcelain.
- **Inset** (`box-shadow: inset 4px 4px 8px rgba(163,177,198,0.62), inset -4px -4px 8px rgba(255,255,255,0.9)`): focused field, chart well.
- **Inset small** (`box-shadow: inset 2px 2px 5px rgba(163,177,198,0.55), inset -2px -2px 5px rgba(255,255,255,0.88)`): fields at rest, tracks, segmented groups, icon wells, fact tiles, pressed buttons, offline cards.
- **Primary glow** (`box-shadow: 4px 6px 14px rgba(37,99,235,0.32), -2px -2px 6px rgba(255,255,255,0.8)`): the royal-blue gradient fill; hover deepens to `0 8px 18px rgba(37,99,235,0.4)`.
- **Camera screen** (`box-shadow: inset 4px 4px 12px rgba(0,0,0,0.5), 2px 2px 0 rgba(255,255,255,0.8)`): the hero and shared camera screens; thumbnails use `inset 3px 3px 8px rgba(0,0,0,0.45)`.

### Named Rules
**The Paired Light Rule.** Every card, button, field and pill gets its depth from a paired light/dark soft shadow lit from the upper left. Single-direction soft shades are kept for the fixed shell edges (sidebar, top bar, save bar) and for the small coloured glow under a solid or gradient fill (primary, danger badge, slider thumb, avatar); no dark hard offsets anywhere.

**The Press-In Rule.** Pressing goes inward: a raised button takes the inset-small shadow on :active, and a chosen option is shown either raised out of an inset group (tabs, segments) or pressed into a raised list (settings nav, selected automation).

**The Lift Rule.** Clickable cards rise 2px and take the raised-large shadow on hover; buttons rise 1px. Offline objects sink to inset-small with a dashed rim and do not lift.

## Shapes

Everything is softly rounded and nests: controls 12px, buttons 11px, fields 10 to 11px, camera screens 14px, small tiles 16px, cards 18px, dialogs 22px, the modal and sign-in card 24px, and every tag, chip, status pill and switch fully round (999px). Inner radii are always smaller than the object holding them. Rims are 1px: white on raised objects, a faint white on wells, a 32 to 35% signal line on alerting objects, dashed for empty slots and offline cards. Status dots are 6 to 8px circles with a 3px halo ring in their own wash.

## Components

### Buttons
Tactile pebbles pressed out of the console.
- **Shape:** gently rounded (11px; 10px for small).
- **Default:** raised-light gradient face, slate-ink text, Rubik 600 at 0.75rem, 8px by 15px, 1px specular rim, raised-small shadow.
- **Hover / Active:** text turns royal-blue-text, lifts 1px and takes the raised shadow; on press it returns to rest and goes inset-small.
- **Primary:** the royal-blue gradient with white text and the primary glow; hover brightens 6%. One per view: Start monitoring, Add camera, Sign in, Save.
- **Danger:** crimson blush face with crimson-text; hover fills solid crimson with white text. Used for Delete, Remove and Clear history, always set apart from the everyday actions beside it (Delete at the left of the automation editor's actions, Remove at the end of a node's).
- **Focus:** a 2px royal-blue outline, 2px offset, on every focusable element.

### Chips and Tags
- **Status pills:** fully round signal tags in Chakra Petch label type: wash background with the matching ink (ON, LIVE, OFF, SAFE, DANGER, ADMIN). A solid crimson fill with white text is reserved for the alerting device pill and the live feed badge.
- **Phone status pill:** a round button: it opens the most urgent incident (the freshest open one), or Live when nothing is open.
- **Theme button:** a 40px round raised button with the current choice's icon (sun, moon, or a half-filled circle for Same as this device); frosted white on the blue sign-in panel.
- **Top-bar chips:** raised-small 12px tiles with a status dot, a tiny uppercase label and a Chakra Petch value; on phones they become one raised round status pill, and from 601 to 1100px the People chip steps aside for the page title.
- **Mode pill:** the round "At home ▾ / Away ▾" button, its chevron saying it can change. Home is a quiet emerald wash (#e3f5ea to #cfeedb) with emerald-text ink and an emerald rim; Away, the armed state, is the filled royal-blue gradient with white text.

### Cards / Containers
- **Corner Style:** 18px.
- **Background:** the raised-light to raised-shade gradient with a 1px specular rim.
- **Shadow Strategy:** raised at rest, raised-large on hover for clickable cards (see Elevation & Depth).
- **Alerting:** crimson blush face with a crimson line rim; warnings take an amber line rim only.
- **Offline:** porcelain-well face, inset-small, dashed rim; the name drops to slate-ink-2 and the value and icon to slate-ink-3. Quiet states use tone, never opacity, so their text still reads at 4.5:1.
- **Device on:** a device that is on fills its whole card with its kind's colour, a 145deg gradient from the 300 to the 400 stop: device-lamp for lights and the lamp (#fde047 to #facc15), device-air for fans (#7dd3fc to #38bdf8), device-power for sockets and relays (#c4b5fd to #a78bfa), device-open for a door unlocked (#fdba74 to #fb923c) and device-sound for the buzzer sounding (#fda4af to #fb7185). Name and value stay slate-ink; the room, since and pin lines take the kind's ink (device-*-ink); the icon tile turns frosted white, and the switch track, slider thumb and focus ring turn slate-ink. The shadow keeps the upper-left light but tints its shade with the hue. The lamp's colour deepens with its brightness (a 40% mix with porcelain at the lowest step, full at 100%). Colour follows the state the board confirmed, never the pending switch, and offline cards never colour.
- **Internal Padding:** 16 to 20px; dividers inside are 1px hairlines.

### Inputs / Fields
- **Style:** porcelain-well face, inset-small, 1px faint white rim, 10 to 12px radius, 9px by 12px padding, Rubik at 0.8125rem.
- **Focus:** the rim turns royal blue; sign-in fields also deepen to the full inset and add a 2px royal-blue halo at 18%.
- **Error:** the rim turns crimson (amber on sign-in) and a crimson-text note sits below.
- **Switch:** an inset round track with a raised knob; on, the track fills with the emerald gradient and the knob turns white.
- **Slider:** a 6px inset track filled royal blue to the value, with an 18px royal-blue gradient thumb ringed in white.
- **Segmented control:** an inset 12px group; the selected option is raised with royal-blue-text.
- **Pop-up windows:** every form window and confirmation is menu blue (navy at night), like the left menu: white words, frosted raised controls, navy wells for fields, white focus rings, and a soft drop shadow instead of the paired one. A chosen option (segment, icon tile, day) is a white pill with royal-blue-text; Leave stays frosted with a white ring. The main button is white with royal-blue-text, and a destructive one white with crimson-text, crimson when hovered. The incident window stays porcelain, so its signal colours and the camera screen read as they do on the page.
- **Blue card:** a card on a page may take the pop-up look (`.blue-card`): Register a person on People, Add camera on Live feeds, Device activity on Devices, the sensor settings (Thresholds) card on Sensors, Active zones on Zones, the Rules list on Automations, and Add an ESP32 node on ESP32 nodes. Same tokens, same white-pill choices and white main button, with a card's lighter drop shadow; a disabled main button turns frosted. Its category is a three-way segment and its face source is choice buttons (cameras two to a row, Photo file on its own row); a photo is shown whole in the preview (contain), a camera fills it (cover), and a photo dragged over the preview gets a dashed white outline. Inside a blue card an off switch keeps a white knob in a navy well, zone tags are light pills with their colour in the words (rose Danger, green Safe, amber Fireplace), the selected rule is a navy well, the first add-a-node step number is the white pill, and an inline link is underlined. An open dropdown on a blue card lists white with slate words (navy with white words at night), as in a pop-up.
- **Gold card:** the same idea in golden yellow (`.gold-card`): ESP32 nodes on Devices and Automatic response on Sensors. By day a 180deg gradient from #fcd34d to #fbbf24 with deep-brown words (#3b2506, #5a3a0b, #6b4712: 6:1 and up on the gold), brown-tinted wells, frosted raised faces, a deep-brown checked box and links in #713f12; warning, danger and OK words take #7c2d12, #9f1239 and #14532d. At night it turns deep gold (#9a5b06 to #7a4304) with white words, like the blue card turning navy.
- **Toast:** the message in the bottom-right corner takes the pop-up look too (menu blue, navy at night, white words, a card's shadow). Its action button (Lock now, Close now, Undo) is a white pill with blue words; an error toast keeps the blue with a 2px rose ring and pale rose words.
- **Choice buttons:** short single-choice lists are buttons, not dropdowns: a raised 12px button per option (name in Rubik 600, a slate-ink-2 detail line under it), and the chosen one pressed in. A neutral choice (a camera) takes a royal-blue rim and royal-blue-text; a signal choice (Danger, Safe or Fireplace zone, in crimson, emerald and amber) takes its own line rim and ink, with a dot in its fill colour on every option.
- **Day buttons:** the days of a schedule are seven equal buttons on one row in label type (MON to SUN), any number pressed in with royal-blue-text and a royal-blue rim; none pressed means every day.
- **Sun times:** a time can be a clock time, sunrise or sunset. For sunrise and sunset the time box starts at today's sunrise or sunset and takes any time within 4 hours of it; a note says how far from the sun that is, and it is saved as minutes before or after, so it moves with the sun through the year.
- **Choice tiles:** a 44px raised square per option with a 20px line icon (the scene icons); the chosen one is pressed in with royal-blue-text and a 1px royal-blue rim. The option's name is its tooltip and accessible name.
- **Scene steps:** each device is a segmented row in label type, sorted A to Z: Leave first and chosen by default (its raised face keeps slate-ink-2, so only real choices turn blue), then On/Off, Lock/Unlock or Open/Close. A dimmer adds a − 50% + segment in 10% steps; pressing − or + chooses it. A fan with speed control offers Off, Low, Medium and High.
- **Person card** (People page, `.person-card`): a portrait card in auto-fill columns of one width, 190px with a 16px gap, so registered people and intruders match (two a row on phones). The top is a 4:5 picture panel in the category's colour, a 180deg gradient: blue #2f63e6 to #1b42bb (Adult), gold #f5b92c to #dc940a (Elderly), green #25a85d to #137a3d (Child), rose #f0506e to #be123c (an intruder), slate #a3afc2 to #6b7a90 (a dismissed intruder); at night each deepens (navy, deep gold, forest, wine, dark slate). The photo is inset 10px from the top and sides, flush at the bottom, with 8px top corners, so the colour frames it. With no photo, a white head-and-shoulders silhouette (white fading to #dbe2ea) rises from the bottom. A 36px white round bin sits top right on the picture (Remove; Delete on a dismissed intruder), crimson on hover, with a white and category-colour double ring on keyboard focus. Below, on the card face: the name (Rubik 700, 1.0625rem; crimson for an intruder), a Chakra Petch uppercase category tag on its tint (Elderly amber, Adult blue, Child green, Intruder crimson, Dismissed inset grey), and the seen line in text-2 at fs-meta, where "at <camera>" wraps as one piece ("Seen 5 min ago / at Living room"), with a 7px green dot when seen in the last 10 minutes (crimson on an intruder). An intruder card ends with Register and Dismiss buttons, which the register form replaces in place.
- **Level devices:** a dimmable light and a fan with speed control carry the same switch as a light at the top right (off, then back on at its last level). Below the state, the light keeps its slider and the fan has a three-way segment, Low / Medium / High (40, 70 and 100 %), with the chosen speed raised; a speed fan that is on takes device-air like any fan.
- **Scene recommendations:** each scene icon recommends settings for the devices the home has now, by kind and by words in the device's name and room (bedroom, living room, kitchen, dining, porch / outside, TV, exhaust, gas). A new scene starts with no icon picked and a hint; picking one fills the rows, plus the name and a summary description when those are empty or still auto-filled, and a note says how many devices it set. In a saved scene, or once rows were changed by hand, a 'Use recommended for X' button appears instead, so nothing is overwritten without asking. A recommendation never unlocks a door, opens a valve or switches off a fridge, router or camera socket.

### Navigation
- **Sidebar:** a menu-blue panel (navy at night) with a faint white right edge and a soft right-hand shade. Section labels are 0.6875rem uppercase in 82% white. Items sit in a nav landmark named Main and are Rubik 500 at 0.8125rem in 90% white with a 16px line icon, 12px radius; hover frosts the item, and the active item is a white pill with royal-blue-text at 600 and a 3.5px royal-blue bar with a soft halo on its left edge. Focus rings on the menu are white. The bottom holds navy status wells (status dots ringed in white so they read on blue), the user tile with a white avatar and a frosted Sign out (white, with crimson-text, on hover), and Start monitoring as a white button with royal-blue-text; while something needs someone it turns frosted so the verdict's action stays the only primary.
- **Settings nav:** active items press in (inset-small) instead of rising; on phones it becomes a horizontal scroller with faded edges.
- **Mobile:** the sidebar slides in as a drawer over a blurred slate backdrop.

### Verdict Band
The household verdict opens every overview: a raised 18px band with a 12px lamp, a Rubik title and a secondary line. Calm is an emerald lamp on porcelain; attention is an amber lamp on a warm porcelain gradient; critical is a crimson lamp on crimson blush with a crimson-tinted raised shadow and a slow lamp pulse; its title is crimson-ink-strong and its line crimson-ink-deep. On a narrow screen or with large text the action moves under the words, right-aligned, instead of squeezing them. With incidents open, the title names the most urgent one and its place ("Gas level high · Kitchen"), never a count; the line says when ("Happening now", "Since 19 Oct 18:26") and how many more are open ("4 more open"), then buzzer and monitoring notes; the action is **What to do**, which opens that incident at its What to do section.

**When the house needs someone.** While the verdict is attention or critical, its action is the page's one primary button: the royal-blue gradient for attention, a crimson gradient (#e11d48 to #be123c, white text) when critical. Setup buttons (Start monitoring, Add camera, Add device, Add ESP32 node, New automation) step back to the default raised button until it passes, and lit device cards calm to a 38% colour mix during a critical incident so the problem outranks the furniture. Stopped monitoring is said by the verdict; the detector pills say PAUSED in slate-ink-3 and keep their names in slate-ink-2 (OFF still marks a detector switched off in Settings).

### Weather Row
The Live page opens with the outdoors, from Open-Meteo for the home location (Settings → Location, shared with sunrise and sunset in automations): a weather card (town in Rubik 700, the temperature in Chakra Petch 700 at 2.75rem, the condition's line icon with its words and H/L at the right, then the next six hours as time, icon and temperature), a sun card (the next sunrise or sunset in Chakra Petch, the day as a curve with a dashed horizon, the daylight part in slate-ink-2 and a royal-blue dot ringed in white where the sun is now, the other time below), a visibility card (km, a word for it, then haze from PM2.5 and the US AQI in emerald, amber or crimson ink), and the three detector pills stacked. Weather icons are the app's 24px line family (sun, moon, partly cloudy by day and night, cloud, fog, drizzle, rain, storm, snow). Below 1200px the detectors become a row of three under the cards; below 700px the weather card spans the width, sun and visibility sit side by side and the detectors stack. The sun card needs no internet (the server works the times out); without internet the weather card says so and keeps the last answer with an "As of" time. The whole row stays as it is whatever the verdict says (the user asked for every card to stay).

### Incident Board
Rows are 12px soft-rounded strips inside a raised table card, with Chakra Petch times and readings and Rubik location and event text; the event label takes the deepened signal ink. Hover raises the row. Handled incidents sit below an inset "Recently handled" divider in slate-ink-2. Critical rows also carry a 5% crimson tint. The thick 3.5px coloured left edge on rows (crimson, amber, emerald) is carried from the user's pinned export, which is its only justification; the user has been offered a replacement, so it is kept on this board and not extended to new components.

**Closing an incident.** While an incident is still happening its window says "Still happening. Once you acknowledge it, more of the same won't alert again unless it gets worse." above the buttons, Acknowledge is a default button and View live leads for a camera. A fresh critical row's button is Review, which opens the incident window so What to do is read before Acknowledge; other rows keep a one-click Acknowledge. Closing one (Acknowledge or False alarm, from the board, the window or a sensor alert) shows a 10-second message naming it, with Undo: for two minutes the person who closed it can reopen it, and the buzzer sounds again if closing it had stopped it. Gas advice ends by naming what HomeShield already did ("HomeShield already turned the exhaust fan on and closed the gas valve: leave them as they are."), so What to do never contradicts What HomeShield did. A buzzer test asks first.

**Status colour.** Every dot and tint says what its value says. A metric card's dot takes its value's tone (emerald fine, amber look, crimson now, device-lamp for lights on, a slate dot for a plain count such as people in view); top-bar chip dots follow their values too. An open incident with nothing new for 12 hours is still open but no longer news: it moves under a "Still open from earlier" divider with a slate-ink-3 left edge, plain slate-ink text and a "Still open" tag on phones, and only a fresh critical incident turns the verdict, the open counts and the phone status pill crimson. Old incidents alone don't make the house urgent: the verdict stays calm (a slate lamp, "No new incidents" or the monitoring state, never "All clear"), is shown on Live only, and its line ends "5 older incidents are still open; acknowledge them once you've checked." With two or more, the divider carries an Acknowledge all N button (default raised, small) whose one Undo reopens them all. The Sensors incident table uses the same words and tones: OPEN in signal colour, STILL OPEN muted, ACKNOWLEDGED, FALSE ALARM, with readings in signal ink only while fresh. A week with no incidents is one raised line on Incidents ("No new incidents in the last 7 days. 5 from earlier are still open.") instead of eight zero tiles. Unknown faces and motion while away share the stat "Unknown faces & motion", whose caption gives the split.

### Tables
Node, pin, sensor-incident and user tables sit in a raised card with Chakra Petch heads and 1px hairline rows. A row that opens something keeps its whole-row click, and a real button on its main words (the node name, the incident title) carries the keyboard and screen reader; a refresh keeps focus on the same row. The selected row takes royal-blue-wash with a 3px royal-blue inset bar on its left.

### Camera Screen
The hero, thumbnail and shared camera screens are dark recessed wells (14px; 11px on thumbnails) set inside a raised card, with a frosted round LIVE pill (dark glass, white Chakra Petch, crimson dot) top left and offline or reconnecting text in screen-text. An alerting camera takes a 2px amber or crimson outline and a full-width signal strip along the bottom of the screen.

### Split-flap Readout
The sign-in clock is a row of raised 14px flap tiles with a hairline split across the middle, in Chakra Petch 700. New characters flip in (340ms, ease-out-expo, staggered), and the characters of a newly arrived incident row flip in the same way (180ms). This is the system's one authored motion moment, and it is switched off under prefers-reduced-motion. On phones the incident window rises as a bottom sheet (220ms slide-up), a transition rather than a second authored moment, also off under reduced motion. Other motion is state feedback only: 150 to 200ms colour, shadow and lift transitions, and a slow opacity pulse on critical or sounding status dots, also stopped under reduced motion.

## Do's and Don'ts

### Do:
- **Do** build every new surface from the porcelain ground with paired light/dark soft shadows: raised for things you touch or read as a unit, inset for fields, tracks and pressed or chosen states.
- **Do** fill a device card with its kind's colour only while the device is on (or a door is unlocked, or the buzzer is sounding); the card's words still say the state, so colour is never the only cue.
- **Do** colour every dot and tint from its value, never its position, and let an open incident with nothing new for 12 hours fall quiet under "Still open from earlier"; only fresh critical incidents turn things crimson.
- **Do** let the verdict's action be the only primary while something needs someone; setup actions wait as secondary buttons.
- **Do** keep every label at 11px or more and show quiet or disabled states with tone (slate-ink-2/3, an inset face), never by fading the whole element.
- **Do** design every new surface for both day and night: light literals go through a variable whose fallback is the day value, and text must pass 4.5:1 on the night surfaces too.
- **Do** pair every irreversible-feeling close with an Undo that restores everything it changed, and make the critical path go through What to do.
- **Do** colour words with the deepened partners (royal-blue-text, emerald-text, amber-text, crimson-text) and fills, dots, borders and shadows with the base hues.
- **Do** put every camera picture, snapshot and clip in the dark rounded camera screen, with screen-text and screen-danger for any text on it.
- **Do** keep the logo a white mark on its solid royal-blue (#2d68fe) tile.
- **Do** set words in Rubik sentence case, and numbers and short uppercase tags in Chakra Petch with tabular figures.
- **Do** keep one royal-blue gradient primary action per view.
- **Do** guard any new motion behind prefers-reduced-motion, and keep the split-flap flip as the only authored moment.
- **Do** keep touch targets at 44px or more on coarse pointers. A transparent hit area around a small text button counts, as long as it doesn't overlap another target.

### Don't:
- **Don't** colour words with royal-blue, emerald, amber or crimson; they are fills and fall below 4.5:1 as text.
- **Don't** put a dark panel anywhere except behind camera video.
- **Don't** use dark hard offset shadows, or give a card, button or field a one-sided drop shadow; the only zero-blur shadow is the white lip of the camera screen.
- **Don't** recolour the logo for status, theme or emphasis.
- **Don't** set headings in Chakra Petch or uppercase, and don't set paragraphs in uppercase.
- **Don't** extend the thick coloured left edge or the haloed nav bar to new components. They come from the pinned export, and the user chose to keep them on incident rows and the active nav item.
- **Don't** let colour stand alone as the alert; a lamp or tint always comes with a words-level label.
