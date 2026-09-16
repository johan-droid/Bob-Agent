"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ThemeToggle } from "@/components/ThemeToggle";

const NAV = [
  { href: "/", label: "🏠 Dashboard" },
  { href: "/chat", label: "💬 Chat" },
  { href: "/kanban", label: "📊 Kanban" },
  { href: "/workspace", label: "🖥️ Workspace" },
  { href: "/vault", label: "📚 Vault" },
  { href: "/outputs", label: "📁 Outputs" },
  { href: "/schedule", label: "📅 Schedule" },
  { href: "/approvals", label: "✅ Approvals", badge: true },
  { href: "/templates", label: "📋 Templates" },
  { href: "/cost", label: "💰 Cost" },
  { href: "/recipes", label: "📖 Recipes" },
  { href: "/reasoning", label: "🔍 Reasoning" },
  { href: "/insights", label: "📊 Insights" },
  { href: "/settings", label: "🔧 Settings" },
  { href: "/audit", label: "📜 Audit Log" },
];

export function Sidebar() {
  const pathname = usePathname();
  const [pending, setPending] = useState(0);
  const [open, setOpen] = useState(false);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [queueDepth, setQueueDepth] = useState(0);
  const [activeAgents, setActiveAgents] = useState(0);

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      try {
        const rows = await api.listApprovals(true);
        if (alive) setPending(rows.length);
      } catch {
        if (alive) setPending(0);
      }
      try {
        await api.health();
        const tasks = await api.listTasks();
        if (!alive) return;
        setConnected(true);
        setQueueDepth(tasks.filter((t) => t.state === "PENDING" || t.state === "QUEUED").length);
        setActiveAgents(tasks.filter((t) => t.state === "RUNNING").length);
      } catch {
        if (alive) setConnected(false);
      }
    };
    void refresh();
    const t = setInterval(refresh, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // Close the drawer on navigation (narrow viewports).
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  return (
    <>
      <button
        type="button"
        className="menu-button"
        aria-label="Open navigation"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        ☰
      </button>
      {open ? (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setOpen(false)}
        />
      ) : null}
      <aside className={`sidebar${open ? " open" : ""}`}>
        <div className="logo">
          <span>🤖 Agent System</span>
          <ThemeToggle />
        </div>
        <nav>
          {NAV.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={active ? "active" : ""}
                aria-current={active ? "page" : undefined}
              >
                {item.label}
                {item.badge && pending > 0 ? <span className="badge">{pending}</span> : null}
              </Link>
            );
          })}
        </nav>
        <div className="sidebar-footer" aria-label="System status">
          <div className="row">
            <span
              className={`conn-dot${connected === null ? "" : connected ? " conn-ok" : " conn-bad"}`}
              aria-hidden="true"
            />
            <span>{connected === null ? "checking…" : connected ? "connected" : "unreachable"}</span>
          </div>
          <div className="row">queue depth: {queueDepth}</div>
          <div className="row">active agents: {activeAgents}</div>
        </div>
      </aside>
    </>
  );
}
