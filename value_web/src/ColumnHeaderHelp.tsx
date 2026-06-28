import { useEffect, useId, useRef, useState } from "react";
import type { IndicatorDoc } from "./indicatorDocs";

type Props = {
  doc: IndicatorDoc;
};

export function ColumnHeaderHelp({ doc }: Props) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent | TouchEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("touchstart", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("touchstart", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div
      ref={rootRef}
      className="column-help"
      onMouseEnter={() => {
        if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) setOpen(true);
      }}
      onMouseLeave={() => {
        if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) setOpen(false);
      }}
    >
      <button
        type="button"
        className="column-help-trigger"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`About ${doc.label}`}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        ⓘ
      </button>
      {open && (
        <div id={panelId} className="column-help-panel" role="tooltip">
          <strong>{doc.label}</strong>
          {doc.formula && (
            <p className="column-help-formula">
              <span className="column-help-kicker">Logic</span>
              {doc.formula}
            </p>
          )}
          <p>
            <span className="column-help-kicker">Feature meaning</span>
            {doc.featureMeaning}
          </p>
          <p>
            <span className="column-help-kicker">LLM interpretation</span>
            {doc.llmInterpretation}
          </p>
        </div>
      )}
    </div>
  );
}
