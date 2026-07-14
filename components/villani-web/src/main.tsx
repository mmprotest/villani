import React from "react";
import ReactDOM from "react-dom/client";
import "@villani/ui/theme.css";
import ConsoleApp from "./ConsoleApp";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConsoleApp />
  </React.StrictMode>,
);
