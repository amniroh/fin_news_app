import { forwardRef, useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useCoarsePointer } from "./useCoarsePointer";

type Props = {
  open: boolean;
  onClose: () => void;
  title?: string;
  subtitle?: string;
  /** Desktop popover anchor (viewport coords). Sheet layout when omitted or on coarse pointers. */
  anchor?: { x: number; y: number };
  children: ReactNode;
  className?: string;
  /** When true, show dimmed backdrop (default: coarse pointer only). */
  backdrop?: boolean;
};

const PANEL_W = 340;
const PANEL_H_EST = 280;

function popoverStyle(anchor: { x: number; y: number }): React.CSSProperties {
  const pad = 12;
  let x = anchor.x + pad;
  let y = anchor.y + pad;
  if (x + PANEL_W > window.innerWidth - pad) x = anchor.x - PANEL_W - pad;
  if (y + PANEL_H_EST > window.innerHeight - pad) y = Math.max(pad, anchor.y - PANEL_H_EST - pad);
  return { left: x, top: y, width: PANEL_W };
}

export const OverlayPanel = forwardRef<HTMLDivElement, Props>(function OverlayPanel(
  { open, onClose, title, subtitle, anchor, children, className, backdrop },
  ref,
) {
  const coarse = useCoarsePointer();
  const showBackdrop = backdrop ?? coarse;
  const sheet = coarse || !anchor;

  useEffect(() => {
    if (!open) return;
    document.body.classList.add("overlay-open");
    return () => document.body.classList.remove("overlay-open");
  }, [open]);

  if (!open) return null;

  return createPortal(
    <>
      {showBackdrop && (
        <button type="button" className="overlay-backdrop" aria-label="Close" onClick={onClose} />
      )}
      <div
        ref={ref}
        className={`overlay-panel ${sheet ? "overlay-panel-sheet" : "overlay-panel-popover"}${className ? ` ${className}` : ""}`}
        style={!sheet && anchor ? popoverStyle(anchor) : undefined}
        role="dialog"
        aria-modal={showBackdrop}
        aria-label={title}
        onMouseEnter={!showBackdrop ? (e) => e.stopPropagation() : undefined}
      >
        <div className="overlay-panel-header">
          <div className="overlay-panel-titles">
            {title && <strong>{title}</strong>}
            {subtitle && <span className="overlay-panel-sub">{subtitle}</span>}
          </div>
          <button type="button" className="overlay-panel-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="overlay-panel-body">{children}</div>
      </div>
    </>,
    document.body,
  );
});
