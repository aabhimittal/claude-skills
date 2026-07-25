---
name: a11y-guard
description: This skill should be used when the user asks to "check accessibility", "a11y review", "is this accessible", "audit my markup for screen readers", "WCAG check", "add alt text / aria-labels", "keyboard navigation issues", or works with HTML or JSX/TSX/Vue/Svelte markup that renders UI. It statically lints markup for accessibility defects: missing alt text, icon-only controls with no accessible name, click handlers on non-interactive elements, unlabelled form fields, positive tabindex, aria-hidden on focusable elements, missing html lang, zoom-blocking viewport, autoplaying audio, and uninformative link text.
version: 1.0.0
---

# a11y-guard

Lint HTML and JSX/TSX markup for the accessibility defects that actually lock
people out: images with no alt text, icon buttons that announce nothing,
`<div onClick>` with no keyboard path, unlabelled inputs, and focus traps.

## When to use this skill

Use it when UI markup is written or reviewed, or whenever accessibility comes up:
"is this accessible?", "a11y review", "WCAG check", "add aria-labels", "keyboard
navigation issues". Also worth running proactively on a new component or page.

## Workflow

1. **Locate the markup.** Look at the diff, the named file, or pass a directory —
   the analyzer recurses `.html/.htm/.jsx/.tsx/.vue/.svelte/.astro` and skips
   `node_modules`, `dist`, `build`, etc.

2. **Run the analyzer.** Dependency-free standard-library Python:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/a11y-guard/scripts/analyze_a11y.py" src/components/Toolbar.jsx
   # or scan a tree:
   python3 ".../analyze_a11y.py" ./src
   ```

   Flags: `--json`, `--fail-on {info,low,medium,high,critical}` (CI gate, default
   `high`), `--selftest`. Exit code is `1` when findings meet the threshold, `0`
   when clean.

3. **Interpret the findings.** Each has a rule ID, severity, the exact line, *who
   it blocks* and why, plus the fix. Full catalog with WCAG mappings:
   `references/rules.md`.

4. **Explain and fix.** Frame findings by **human impact**, not rule numbers —
   "this icon button is announced as just 'button', so a screen-reader user can't
   tell it closes the dialog." Then apply the fix. Two patterns cover most of it:
   - **Use the real element.** `<button>` gives focus, Enter/Space, and the right
     role for free; a `<div onClick>` needs `role` + `tabIndex` + a key handler
     to catch up.
   - **Name the control, hide the icon:**
     `<button aria-label="Close"><Icon aria-hidden="true" /></button>`.

   The `examples/` folder pairs an inaccessible page and component with their
   accessible rewrites.

5. **State the limits honestly.** Automated checks catch a *minority* of real
   barriers. Passing this linter is a floor, not a ceiling: colour contrast,
   focus order, reading order, and whether the `alt` text is actually useful
   still need human review — ideally with a screen reader. Say so rather than
   implying the UI is now "accessible."

## What it detects (summary)

| | |
| --- | --- |
| **high** | A11Y001 missing `alt` · A11Y002 unnamed icon control · A11Y003 click handler on non-interactive element · A11Y005 unlabelled form field · A11Y006 `aria-hidden` on focusable element · A11Y008 missing `<html lang>` |
| **medium** | A11Y004 positive `tabindex` · A11Y007 `<a>` with no `href` · A11Y009 zoom-blocking viewport · A11Y010 unmuted autoplay · A11Y012 missing `<title>` |
| **low** | A11Y011 uninformative link text |

Custom components (capitalized tags like `<Button>`) are skipped for
element-specific rules — lint the component's own definition instead.

## Output style for the user

Lead with the count and the highest-impact barrier in human terms, go worst-first
with file:line, and give the corrected markup. Close with the caveat that
automated checks are a floor and suggest a keyboard/screen-reader pass.
