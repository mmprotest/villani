import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import FleetApp from "./FleetApp";
import InterrogateApp from "./InterrogateApp";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {location.pathname.startsWith("/ask") ? (
      <InterrogateApp />
    ) : location.pathname.startsWith("/fleet") ? (
      <FleetApp />
    ) : (
      <App />
    )}
  </React.StrictMode>,
);
