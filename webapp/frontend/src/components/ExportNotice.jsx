import { useEffect, useState } from "react";

export default function ExportNotice() {
  const [saved, setSaved] = useState(null);
  useEffect(() => {
    const onComplete = (event) => setSaved(event.detail);
    const onStart = () => setSaved(null);
    window.addEventListener("rundiff:export-start", onStart);
    window.addEventListener("rundiff:export-complete", onComplete);
    return () => {
      window.removeEventListener("rundiff:export-start", onStart);
      window.removeEventListener("rundiff:export-complete", onComplete);
    };
  }, []);
  if (!saved) return null;
  return (
    <div className="export-notice" role="status">
      <span className="export-check" aria-hidden="true">✓</span>
      <div><strong>Export complete</strong><div>Saved as “{saved.filename}” in “{saved.location}”.</div></div>
      <button className="btn ghost" aria-label="Dismiss export notification" onClick={() => setSaved(null)}>×</button>
    </div>
  );
}
