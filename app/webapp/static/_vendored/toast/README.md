# `toast` — the neutral frosted toast

The fleet's one transient message, for the result of a user-initiated command ("Saved", "Opening in Explorer", "Couldn't reach the server"). It is the floating nav bar's glass (translucent card fill, hairline border, `backdrop-filter: blur(20px) saturate(1.25)`), centred 8px above the nav so the nav never hides it, and above every overlay. **Only a real error tints** — a `danger` fill, border and `accent-fg` text. Success, info and warning stay neutral: the message says what happened, colour does not. Spec: `~/.claude/design.md` → `toast` component and its contract bullet; feedback altitude in "Async data & feedback". Normalized from app-launcher's "Neutral frosted toast" (ported from home-automation #782).

## Files

| File | Role |
| --- | --- |
| `toast.css` | Visual contract — the frosted surface, centring above the nav, `max-content` sizing, the error tint. References design tokens only. |
| `toast.js` | `showToast(message, kind?, opts?)` / `hideToast()`. Creates the element on first use. |
| `toast.html` | Optional skeleton, for a page that wants the live region in the markup from first paint. |

## How to vendor

1. Copy this `toast/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/toast/`). Do **not** edit `toast.css` or `toast.js` per-app.
2. Link the CSS and import the function:
   ```html
   <link rel="stylesheet" href="/static/_vendored/toast/toast.css">
   ```
   ```js
   import { showToast } from '/static/_vendored/toast/toast.js';
   ```
3. Define the tokens below. The nav's two glass tokens (`--tabbar-bg`, `--tabbar-border`) are the ones an app that vendors `nav/` already has.

## Usage

```js
showToast('Saved');                                  // neutral, polite, 2.2 s
showToast('Could not reach the server', 'error');    // tinted, assertive, 4.5 s
showToast('Uploading…', undefined, { ms: 0 });       // stays until the next toast or hideToast()
```

- **One toast at a time.** A new call replaces the one on screen and restarts the timer.
- **Plain text only.** The message is set with `textContent`, so server text needs no escaping. A toast carries no icon, no action and no HTML; a message that needs those is an inline status or a modal.
- **Only `'error'` is a kind.** Any other value (or none) renders the neutral toast. There is deliberately no success variant: do not add a green one, and do not tint a warning.
- **Live region.** `showToast()` sets `role="status"` and `aria-live` (`assertive` for the error, `polite` otherwise) without moving focus. If you ship the `toast.html` skeleton, that is the element it uses (`id="toast"`).
- **Failure copy is sanitized** — no hostnames, URLs, exception classes or timeout internals in the message; the logs keep the detail (design.md, feedback altitude).
- **Reserve it** for a user-initiated command's progress or result. Passive background status renders inline beside the surface it describes, never as a toast.

## Layout contract

- `position: fixed`, `left: 50%` + `translateX(-50%)`, `bottom` = nav height + 2× nav margin + `env(safe-area-inset-bottom)` + 8px, using nav-tabs.css's own `var(--bottom-tabs-*, fallback)` defaults, so an app that leaves those tokens undefined still lands on the right line.
- `width: max-content` with `max-width: min(100vw - 24px, 560px)`: one line whenever the message fits. Without `max-content` the box shrinks to the 50vw left of its anchor and wraps at half the screen. `text-wrap: balance` splits a longer message evenly.
- `padding: 12px 18px`, `font-size: var(--font-label)`, weight 700, line-height 1.5, centred.
- `z-index: 400`, above a full-screen overlay or modal backdrop.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--tabbar-bg` | `rgba(255,255,255,0.85)` (dark `rgba(22,27,34,0.86)`) | glass fill (the nav bar's) |
| `--tabbar-border` | `rgba(31,35,40,0.12)` (dark `rgba(255,255,255,0.13)`) | hairline border (the nav bar's) |
| `--ink` | `#1f2328` | text |
| `--deficit` | `#cf222e` | error fill (75% mix) and border |
| `--accent-fg` | `#ffffff` | error text |
| `--shadow` | `0 8px 24px rgba(31,35,40,0.12)` | the theme shadow (falls back to the nav bar's `0 10px 34px rgba(0,0,0,0.5)` if undefined) |
| `--radius-md` | `12px` | corners |
| `--font-label` | `0.875rem` | text size |
| `--bottom-tabs-height` / `--bottom-tabs-margin` | `61px` / `21px` | optional; fall back to the nav's own defaults |

## Don't diverge

`toast.css` and `toast.js` are vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. An app that draws its own toast, or tints a success one, is drifting from the spec; adopt this one instead. If your own CSS declares the same selector this file touches (`.toast`), use longhand properties or a disjoint media condition: a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
