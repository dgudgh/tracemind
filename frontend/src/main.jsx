import React from "react";
import { createRoot } from "react-dom/client";
import "antd/dist/reset.css";
import "./styles.css";
import { AppRoot } from "./AppRoot";

const rootElement = document.getElementById("root");
const appRoot = window.__MESGUARD_APP_ROOT__ || createRoot(rootElement);
window.__MESGUARD_APP_ROOT__ = appRoot;
appRoot.render(<AppRoot />);
