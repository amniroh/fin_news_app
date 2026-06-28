/** Production: empty string (same origin via nginx). Dev: localhost backend. */
export function getApiBase(): string {
  return (
    (import.meta as any).env?.VITE_API_BASE ??
    (import.meta.env.DEV ? "http://localhost:8000" : "")
  );
}
