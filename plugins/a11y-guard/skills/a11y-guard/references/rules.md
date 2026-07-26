# a11y-guard rule catalog

Stable rule IDs and severities. `critical` > `high` > `medium` > `low` > `info`.
Default CI gate (`--fail-on`) is `high`. Each maps to a WCAG success criterion.

| Rule | Severity | Trigger | Who it blocks | WCAG |
| --- | --- | --- | --- | --- |
| A11Y001 | high | `<img>` with no `alt` / `aria-label` | Screen-reader users lose the image's meaning | 1.1.1 |
| A11Y002 | high | `<button>`/`<a>` with no text and no `aria-label`/`title` | Icon-only control announced as just "button" | 4.1.2 |
| A11Y003 | high | Click handler on `div`/`span`/etc. without `role` + `tabindex` + key handler | Keyboard and screen-reader users can't reach or activate it | 2.1.1, 4.1.2 |
| A11Y005 | high | `<input>`/`<select>`/`<textarea>` with no label association | Users can't tell what to enter (placeholder ≠ label) | 1.3.1, 3.3.2 |
| A11Y006 | high | `aria-hidden` on a focusable element | "Ghost focus": focusable but announces nothing | 4.1.2 |
| A11Y008 | high | `<html>` without `lang` | Screen reader guesses the language → wrong pronunciation | 3.1.1 |
| A11Y004 | medium | `tabindex` greater than 0 | Fragile manual focus order surprises keyboard users | 2.4.3 |
| A11Y007 | medium | `<a>` with no `href` | Not focusable or activatable by keyboard | 2.1.1 |
| A11Y009 | medium | `user-scalable=no` / `maximum-scale=1` | Low-vision users can't pinch-zoom | 1.4.4 |
| A11Y010 | medium | `autoplay` media without `muted` | Audio drowns out the screen reader | 1.4.2 |
| A11Y012 | medium | HTML document with no `<title>` | No way to distinguish tabs/history | 2.4.2 |
| A11Y011 | low | Link text like "click here" / "read more" | Link lists (a common SR navigation mode) become meaningless | 2.4.4 |

## The two fixes worth internalizing

**Use the real element.** `<button>` gives you focusability, Enter/Space
activation, and the correct role for free. A `<div onClick>` gives you none of
them, and retrofitting all three by hand (`role="button"`, `tabIndex={0}`, an
Enter/Space handler) is what A11Y003 is asking for — using `<button>` is
strictly less work.

**Name the control, hide the icon.** For an icon-only control, put the name on
the *control* and hide the decorative glyph:

```jsx
<button aria-label="Close dialog" onClick={close}>
  <CloseIcon aria-hidden="true" />
</button>
```

Note `aria-hidden` on the icon (correct — it's decorative) versus `aria-hidden`
on the button itself (A11Y006 — creates ghost focus).

## Notes & limitations

- **Tag-level regex scan, not a DOM parser.** It handles HTML, JSX/TSX, Vue, and
  Svelte attribute syntax (including `{expr}`, `@click`, `on:click`), and masks
  HTML `<!-- -->` and JSX `{/* */}` comments so commented-out markup is ignored.
  Deeply dynamic markup (attributes spread from an object, elements built in a
  `.map()` with computed props) can be missed.
- **Custom components are skipped** for element-specific rules — a capitalized
  tag like `<Button>` could render anything, so the analyzer doesn't guess. Lint
  the component's own definition instead.
- **A11Y005 label pairing** is textual: it accepts a matching `for=`/`htmlFor=`
  anywhere in the same file. A label defined in a different file won't be seen.
- **Automated checks find a minority of real barriers.** Passing this linter is a
  floor, not a ceiling — colour contrast, focus order, meaningful reading order,
  and whether `alt` text is actually *useful* still need human (and ideally
  screen-reader) review.
