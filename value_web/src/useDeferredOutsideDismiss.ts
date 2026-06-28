import { useEffect, type RefObject } from "react";

/** Close when clicking/tapping outside `refs`, deferred so the opening tap does not immediately dismiss. */
export function useDeferredOutsideDismiss(
  open: boolean,
  refs: RefObject<HTMLElement | null> | RefObject<HTMLElement | null>[],
  onClose: () => void,
) {
  useEffect(() => {
    if (!open) return;
    const refList = Array.isArray(refs) ? refs : [refs];

    const onDoc = (e: MouseEvent | TouchEvent) => {
      const target = e.target as Node;
      if (refList.some((r) => r.current?.contains(target))) return;
      onClose();
    };

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };

    const timer = window.setTimeout(() => {
      document.addEventListener("mousedown", onDoc);
      document.addEventListener("touchstart", onDoc, { passive: true });
      document.addEventListener("keydown", onKey);
    }, 0);

    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("touchstart", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, refs, onClose]);
}
