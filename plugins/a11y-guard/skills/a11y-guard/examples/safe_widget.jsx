// The same component, accessible. analyze_a11y.py reports no findings.
export function Toolbar({ onSave, onClose, goHome }) {
  return (
    <div className="toolbar">
      {/* A real button: focusable, Enter/Space work, announced as a button */}
      <button onClick={onSave}>Save</button>

      {/* Icon-only control carries an explicit accessible name */}
      <button aria-label="Close toolbar" onClick={onClose}><CloseIcon /></button>

      {/* Navigation uses a real href */}
      <a href="/">Home</a>

      {/* Decorative icon is hidden; the control itself stays announced */}
      <button onClick={onSave}>
        <CloseIcon aria-hidden="true" />
        Sync
      </button>
    </div>
  );
}
