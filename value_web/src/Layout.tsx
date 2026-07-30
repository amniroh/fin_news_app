import { NavLink, Outlet } from "react-router-dom";
import "./App.css";

export function Layout() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header-inner">
          <div className="app-brand">Value Metrics</div>
          <nav className="app-nav" aria-label="Main navigation">
            <NavLink
              to="/"
              end
              className={({ isActive }) => (isActive ? "nav-link nav-link-active" : "nav-link")}
            >
              Home
            </NavLink>
            <NavLink
              to="/stocks"
              className={({ isActive }) => (isActive ? "nav-link nav-link-active" : "nav-link")}
            >
              Stocks
            </NavLink>
            <NavLink
              to="/research"
              className={({ isActive }) => (isActive ? "nav-link nav-link-active" : "nav-link")}
            >
              Research
            </NavLink>
            <NavLink
              to="/internal-logs"
              className={({ isActive }) => (isActive ? "nav-link nav-link-active" : "nav-link")}
            >
              Internal logs
            </NavLink>
            <NavLink
              to="/prediction-markets"
              className={({ isActive }) => (isActive ? "nav-link nav-link-active" : "nav-link")}
            >
              Prediction markets
            </NavLink>
            <NavLink
              to="/strategies"
              className={({ isActive }) => (isActive ? "nav-link nav-link-active" : "nav-link")}
            >
              Strategy results
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
