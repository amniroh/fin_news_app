import { useEffect, useState } from "react";

export function isCoarsePointer(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(hover: none), (pointer: coarse)").matches;
}

export function useCoarsePointer(): boolean {
  const [coarse, setCoarse] = useState(isCoarsePointer);

  useEffect(() => {
    const mq = window.matchMedia("(hover: none), (pointer: coarse)");
    const onChange = () => setCoarse(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return coarse;
}
