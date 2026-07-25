// Example React component with accessibility defects.
export function Toolbar({ onSave, onClose, goHome }) {
  return (
    <div className="toolbar">
      {/* A11Y003: click handler on a div — no keyboard path, no role */}
      <div onClick={onSave}>Save</div>

      {/* A11Y002: icon-only button with no accessible name */}
      <button onClick={onClose}><CloseIcon /></button>

      {/* A11Y007: anchor with no href used as a control */}
      <a onClick={goHome}>Home</a>

      {/* A11Y006: focusable but hidden from assistive tech */}
      <button aria-hidden="true" onClick={onSave}>Sync</button>
    </div>
  );
}
