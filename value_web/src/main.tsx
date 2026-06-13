import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import "./index.css";
import App from "./App.tsx";
import { StrategiesPage } from "./StrategiesPage.tsx";
import { InterestingStocksPage } from "./InterestingStocksPage.tsx";
import { TickerDetailPage } from "./TickerDetailPage.tsx";

const apiBase = (import.meta as any).env?.VITE_API_BASE || "http://localhost:8000";

function NavBar() {
  const linkStyle = ({ isActive }: { isActive: boolean }) => ({
    padding: "8px 14px",
    textDecoration: "none",
    color: isActive ? "#1a365d" : "#4a5568",
    fontWeight: isActive ? 600 : 500,
    borderBottom: isActive ? "2px solid #2b6cb0" : "2px solid transparent",
  });
  return (
    <nav
      style={{
        display: "flex",
        gap: 8,
        padding: "8px 16px",
        borderBottom: "1px solid #e2e8f0",
        background: "#f7fafc",
      }}
    >
      <Link to="/" style={{ marginRight: 12, fontWeight: 700, color: "#1a365d", textDecoration: "none", padding: "8px 0" }}>
        Market Analysis
      </Link>
      <NavLink to="/" end style={linkStyle}>
        Metrics
      </NavLink>
      <NavLink to="/strategies" style={linkStyle}>
        Strategies
      </NavLink>
      <NavLink to="/stocks" style={linkStyle}>
        Stocks
      </NavLink>
    </nav>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <NavBar />
      <Routes>
        <Route path="/" element={<App />} />
        <Route path="/strategies" element={<StrategiesPage apiBase={apiBase} />} />
        <Route path="/stocks" element={<InterestingStocksPage apiBase={apiBase} />} />
        <Route path="/stocks/:symbol" element={<TickerDetailPage apiBase={apiBase} />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
