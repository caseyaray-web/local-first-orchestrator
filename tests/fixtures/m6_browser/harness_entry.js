import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";

const apiCalls = [];
const classes = (...values) => values.filter(Boolean).join(" ");
const element = (tag, base) => ({ className, children, ...props }) =>
  React.createElement(tag, { ...props, className: classes(base, className) }, children);
const Card = element("section", "m6-card");
const CardHeader = element("header", "m6-card-header");
const CardTitle = element("h2", "m6-card-title");
const CardContent = element("div", "m6-card-content");
const Button = ({ className, children, type = "button", ...props }) =>
  React.createElement("button", { ...props, type, className: classes("m6-button", className) }, children);
const Badge = element("span", "m6-badge");

async function fetchJSON(url, init) {
  const response = await fetch(url, { ...init, credentials: init?.credentials ?? "same-origin" });
  const body = await response.text();
  apiCalls.push({ method: init?.method || "GET", url, status: response.status, body });
  if (!response.ok) throw new Error(`${response.status}: ${body || response.statusText}`);
  return JSON.parse(body);
}

const registered = new Map();
window.__HERMES_PLUGINS__ = {
  register(name, component) { registered.set(name, component); },
  registerSlot() {},
};
window.__HERMES_PLUGIN_SDK__ = {
  sdkVersion: "1.1.0",
  React,
  hooks: { useState, useEffect, useCallback, useMemo, useRef, useContext, createContext },
  fetchJSON,
  components: { Card, CardHeader, CardTitle, CardContent, Button, Badge },
  utils: { cn: classes, timeAgo: String, isoTimeAgo: String },
};
window.__M6_HARNESS__ = { apiCalls, registered, reactVersion: React.version };

function Harness() {
  const [Component, setComponent] = useState(null);
  const [loadError, setLoadError] = useState("");
  useEffect(() => {
    const script = document.createElement("script");
    script.src = "/actual-plugin/index.js";
    script.onload = () => {
      const component = registered.get("local-first-orchestrator");
      if (component) setComponent(() => component);
      else setLoadError("Actual dashboard bundle loaded but did not register local-first-orchestrator.");
    };
    script.onerror = () => setLoadError("Actual dashboard bundle failed to load.");
    document.head.appendChild(script);
    return () => script.remove();
  }, []);
  if (loadError) return React.createElement("pre", { id: "harness-error" }, loadError);
  if (!Component) return React.createElement("p", { id: "harness-loading" }, "Loading actual local-first dashboard bundle…");
  return React.createElement(Component);
}

createRoot(document.getElementById("root")).render(React.createElement(Harness));
