import { useState } from "react";

export function AddInterestingStock({ apiBase }: { apiBase: string }) {
  const [newSymbol, setNewSymbol] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function addSymbol() {
    const sym = newSymbol.trim().toUpperCase();
    if (!sym) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const r = await fetch(`${apiBase}/value/interesting/stocks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setNewSymbol("");
      setMessage(`${sym} added to interesting stocks. Daily jobs will backfill data.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      style={{
        marginBottom: 20,
        padding: 14,
        background: "#f7fafc",
        borderRadius: 8,
        border: "1px solid #e2e8f0",
      }}
    >
      <h3 style={{ margin: "0 0 8px", fontSize: 16 }}>Add interesting stock</h3>
      <p style={{ margin: "0 0 10px", fontSize: 13, color: "#4a5568" }}>
        Adds a ticker to the tracked universe in the database. Scheduled jobs will fetch prices,
        fundamentals, news, and analyst data.
      </p>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <input
          value={newSymbol}
          onChange={(e) => setNewSymbol(e.target.value)}
          placeholder="e.g. AAPL"
          style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid #cbd5e0", width: 140 }}
          onKeyDown={(e) => e.key === "Enter" && addSymbol()}
        />
        <button type="button" onClick={addSymbol} disabled={busy}>
          {busy ? "Adding…" : "Add to list"}
        </button>
      </div>
      {message && <p style={{ margin: "10px 0 0", fontSize: 13, color: "#276749" }}>{message}</p>}
      {error && <p style={{ margin: "10px 0 0", fontSize: 13, color: "#c53030" }}>{error}</p>}
    </section>
  );
}
