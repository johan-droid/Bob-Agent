"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ThemeToggle } from "@/components/ThemeToggle";

const LABELS: Record<string, string> = {
  "": "Dashboard",
  chat: "Chat",
  kanban: "Kanban",
  workspace: "Workspace",
  vault: "Vault",
  outputs: "Outputs",
  schedule: "Schedule",
  approvals: "Approvals",
  templates: "Templates",
  cost: "Cost",
  recipes: "Recipes",
  reasoning: "Reasoning",
  insights: "Insights",
  settings: "Settings",
  audit: "Audit Log",
};

/** Sticky top header: breadcrumb, connection dot, theme toggle slot. */
export function Header() {
  const pathname = usePathname();
  const [connected, setConnected] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        await api.health();
        if (alive) setConnected(true);
      } catch {
        if (alive) setConnected(false);
      }
    };
    void check();
    const t = setInterval(check, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const segment = pathname.split("/").filter(Boolean)[0] ?? "";
  const here = LABELS[segment] ?? "Dashboard";

  return (
    <header className="topbar">
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <Link href="/">Agent System</Link>
        <span aria-hidden="true"> › </span>
        {segment ? <span aria-current="page">{here}</span> : <span aria-current="page">Dashboard</span>}
      </nav>
      <div className="topbar-right">
        <span
          className={`conn-dot${connected === null ? "" : connected ? " conn-ok" : " conn-bad"}`}
          role="status"
          aria-label={connected === null ? "Checking connection" : connected ? "Backend connected" : "Backend unreachable"}
          title={connected === null ? "Checking…" : connected ? "Backend connected" : "Backend unreachable"}
        />
        <ThemeToggle />
      </div>
    </header>
  );
}
