"use client";

import { useEffect, useState } from "react";

const COOKIE = "bob-theme";

function readTheme(): "light" | "dark" {
  if (typeof document === "undefined") return "light";
  const attr = document.documentElement.getAttribute("data-theme");
  return attr === "dark" ? "dark" : "light";
}

// Persisted in a cookie (not localStorage): this is a server-adjacent app
// and the choice must be readable on subsequent server renders.
export function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark">("light");

  useEffect(() => {
    setTheme(readTheme());
  }, []);

  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    document.cookie = `${COOKIE}=${next}; path=/; max-age=31536000; samesite=lax`;
    setTheme(next);
  };

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggle}
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
    >
      {theme === "dark" ? "☀️" : "🌙"}
    </button>
  );
}
