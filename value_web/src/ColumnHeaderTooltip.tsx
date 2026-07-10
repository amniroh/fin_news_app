import { useCallback, useId, useRef, useState } from "react";
import { OverlayPanel } from "./OverlayPanel";
import { useCoarsePointer } from "./useCoarsePointer";
import { useDeferredOutsideDismiss } from "./useDeferredOutsideDismiss";

type Props = {
  label: string;
  description: string;
};

export function ColumnHeaderTooltip({ label, description }: Props) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState({ x: 0, y: 0 });
  const panelId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  const coarse = useCoarsePointer();

  useDeferredOutsideDismiss(open && !coarse, [panelRef, triggerRef], close);

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
        className="column-help-trigger"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`About ${label}`}
        onClick={(e) => {
          e.stopPropagation();
          if (open) close();
          else openPanel();
        }}
        onMouseEnter={() => {
          if (!coarse) openPanel();
        }}
        onMouseLeave={() => {
          if (!coarse) close();
        }}
      >
        ⓘ
      </button>
      <OverlayPanel
        ref={panelRef}
        open={open}
        onClose={close}
        title={label}
        anchor={anchor}
        backdrop={coarse}
        className="column-help-overlay"
      >
        <div
          id={panelId}
          className="column-help-panel-inner"
          role="tooltip"
          onMouseEnter={!coarse ? () => setOpen(true) : undefined}
          onMouseLeave={!coarse ? close : undefined}
        >
          <p style={{ margin: 0 }}>{description}</p>
        </div>
      </OverlayPanel>
    </>
  );
}
