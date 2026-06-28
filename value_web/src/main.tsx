import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "./index.css";
import App from "./App.tsx";
import { getApiBase } from "./apiBase.ts";
import { Layout } from "./Layout.tsx";
import { StrategiesPage } from "./StrategiesPage.tsx";

const apiBase = getApiBase();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<App apiBase={apiBase} />} />
          <Route path="/strategies" element={<StrategiesPage apiBase={apiBase} />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
