import { useEffect, useRef, useState } from "react";

// One global tooltip for the whole app. Any element with a non-empty `data-tip` attribute gets
// it on hover/focus. Rendered fixed-position relative to the viewport, so it never clips inside
// scroll containers (overflow:auto grids, the rail, etc.), and flips below the anchor when near
// the top edge. Fades in/out (see .tip-pop in app.css) instead of snapping.
//
// The tricky case: when the hovered element is re-rendered or removed because the screen state
// changed (a click, a route change, async data landing), the matching `mouseout` never fires and
// a stale tip lingers — sometimes stuck for good. We guard against that by remembering the anchor
// and dropping the tip the moment it leaves the DOM or stops being hovered, plus clearing on any
// pointer-down (clicks almost always mutate the view).
export default function TooltipLayer() {
  const [tip, setTip] = useState(null); // { text, x, y, below }
  const [shown, setShown] = useState(false); // drives the fade; false → fade out, then unmount
  const anchorRef = useRef(null);
  const hideTimer = useRef(null);

  useEffect(() => {
    function locate(target) {
      const el = target?.closest?.("[data-tip]");
      if (!el) return null;
      const text = el.getAttribute("data-tip");
      if (!text) return null;
      const r = el.getBoundingClientRect();
      const below = r.top < 64; // not enough room above → drop under the anchor
      return { el, text, x: r.left + r.width / 2, y: below ? r.bottom : r.top, below };
    }
    const show = (t) => {
      clearTimeout(hideTimer.current);
      anchorRef.current = t.el;
      setTip(t);
      setShown(true);
    };
    const hide = () => {
      anchorRef.current = null;
      setShown(false);
      clearTimeout(hideTimer.current);
      hideTimer.current = setTimeout(() => setTip(null), 140); // unmount after the fade
    };

    const onOver = (e) => { const t = locate(e.target); if (t) show(t); };
    const onOut = (e) => { if (e.target?.closest?.("[data-tip]")) hide(); };
    const clear = () => hide();

    document.addEventListener("mouseover", onOver);
    document.addEventListener("mouseout", onOut);
    document.addEventListener("pointerdown", clear, true); // clicks usually mutate the view
    document.addEventListener("wheel", clear, { passive: true });
    window.addEventListener("scroll", clear, true);
    window.addEventListener("blur", clear);

    // If the anchor is re-rendered/removed while hovered, mouseout never fires. Watch the DOM
    // and drop the tip the moment its anchor is gone or no longer under the pointer.
    const mo = new MutationObserver(() => {
      const el = anchorRef.current;
      if (el && (!el.isConnected || !el.matches(":hover"))) hide();
    });
    mo.observe(document.body, { childList: true, subtree: true });

    return () => {
      document.removeEventListener("mouseover", onOver);
      document.removeEventListener("mouseout", onOut);
      document.removeEventListener("pointerdown", clear, true);
      document.removeEventListener("wheel", clear);
      window.removeEventListener("scroll", clear, true);
      window.removeEventListener("blur", clear);
      mo.disconnect();
      clearTimeout(hideTimer.current);
    };
  }, []);

  if (!tip) return null;
  return (
    <div
      className={"tip-pop" + (tip.below ? " tip-below" : "") + (shown ? " tip-show" : "")}
      style={{ left: tip.x, top: tip.y }}
      role="tooltip"
    >
      {tip.text}
    </div>
  );
}
