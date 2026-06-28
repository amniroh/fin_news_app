import { useCallback, useRef, useState, type ReactNode } from "react";
import { OverlayPanel } from "./OverlayPanel";

type Props = {
  detail?: string;
  title?: string;
  children: ReactNode;
};

export function CellDetailTrigger({ detail, title, children }: Props) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState({ x: 0, y: 0 });
  const triggerRef = useRef<HTMLButtonElement>(null);

  const close = useCallback(() => setOpen(false), []);

  if (!detail) {
    return <>{children}</>;
  }

  const openPanel = () => {
    const r = triggerRef.current?.getBoundingClientRect();
    setAnchor({ x: r?.left ?? 0, y: r?.bottom ?? 0 });
    setOpen(true);
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="cell-detail-trigger cell-detail-trigger--has-detail"
        aria-expanded={open}
        aria-label={title ? `${title}: tap for details` : "Tap for details"}
        onClick={(e) => {
          e.stopPropagation();
          if (open) close();
          else openPanel();
        }}
      >
        {children}
      </button>
      <OverlayPanel open={open} onClose={close} title={title} anchor={anchor} backdrop>
        <p className="cell-detail-text">{detail}</p>
      </OverlayPanel>
    </>
  );
}
