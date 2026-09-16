"use client";

/**
 * Pages for the remaining tabs (vault, schedule, templates, cost, recipes,
 * reasoning, insights, settings) follow the same pattern: fetch real backend
 * state via /api/v1 and show an honest "backend unreachable" state — no
 * mocked data (v3.1 §33). Backend endpoints for these tabs land with their
 * feature phases; the UI never fabricates content meanwhile.
 */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export function useApiGuard() {
  const [reachable, setReachable] = useState<boolean | null>(null);

  useEffect(() => {
    const check = async () => {
      try {
        await api.health();
        setReachable(true);
      } catch {
        setReachable(false);
      }
    };
    void check();
    const t = setInterval(check, 5000);
    return () => clearInterval(t);
  }, []);

  return reachable;
}
