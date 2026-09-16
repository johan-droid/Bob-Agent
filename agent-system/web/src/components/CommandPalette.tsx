"use client";

import { useEffect, useState, useMemo, useCallback } from "react";
import { useRouter } from "next/navigation";
import { api, type Session } from "@/lib/api";

interface CommandItem {
  id: string;
  category: "Navigation" | "Action" | "Conversations";
  title: string;
  subtitle?: string;
  icon: string;
  perform: () => void;
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [sessions, setSessions] = useState<Session[]>([]);
  const router = useRouter();

  // Listen for Cmd+K / Ctrl+K
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
      } else if (e.key === "Escape" && open) {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  // Load sessions when palette opens
  useEffect(() => {
    if (open) {
      setQuery("");
      setSelectedIndex(0);
      void api.listSessions().then(setSessions).catch(() => setSessions([]));
    }
  }, [open]);

  const navigate = useCallback(
    (path: string) => {
      setOpen(false);
      router.push(path);
    },
    [router],
  );

  const commands = useMemo<CommandItem[]>(() => {
    const items: CommandItem[] = [
      {
        id: "nav-chat",
        category: "Navigation",
        title: "Go to Chat",
        subtitle: "Interactive agent conversational workspace",
        icon: "💬",
        perform: () => navigate("/chat"),
      },
      {
        id: "action-new-chat",
        category: "Action",
        title: "Start New Chat",
        subtitle: "Create a fresh conversation session",
        icon: "＋",
        perform: () => {
          navigate("/chat");
        },
      },
      {
        id: "nav-settings",
        category: "Navigation",
        title: "Settings & Providers",
        subtitle: "Configure LLM keys, memory, tools, and integrations",
        icon: "🔧",
        perform: () => navigate("/settings"),
      },
      {
        id: "nav-kanban",
        category: "Navigation",
        title: "Task Kanban",
        subtitle: "View active, pending, and completed tasks",
        icon: "📊",
        perform: () => navigate("/kanban"),
      },
      {
        id: "nav-workspace",
        category: "Navigation",
        title: "Workspace Files",
        subtitle: "Browse workspace filesystem and file trees",
        icon: "🖥️",
        perform: () => navigate("/workspace"),
      },
      {
        id: "nav-vault",
        category: "Navigation",
        title: "Obsidian Memory Vault",
        subtitle: "Browse indexed notes and memories",
        icon: "📚",
        perform: () => navigate("/vault"),
      },
      {
        id: "nav-approvals",
        category: "Navigation",
        title: "Pending Approvals",
        subtitle: "Review human-in-the-loop permission requests",
        icon: "✅",
        perform: () => navigate("/approvals"),
      },
      {
        id: "nav-cost",
        category: "Navigation",
        title: "Cost & Token Analytics",
        subtitle: "Inspect model calls, token usage, and spend",
        icon: "💰",
        perform: () => navigate("/cost"),
      },
      {
        id: "nav-recipes",
        category: "Navigation",
        title: "Agent Recipes",
        subtitle: "Execute predefined automated workflows",
        icon: "📖",
        perform: () => navigate("/recipes"),
      },
      {
        id: "nav-insights",
        category: "Navigation",
        title: "Daily Insights",
        subtitle: "Synthesized agent findings and suggestions",
        icon: "💡",
        perform: () => navigate("/insights"),
      },
      {
        id: "nav-templates",
        category: "Navigation",
        title: "Workspace Templates",
        subtitle: "Snapshot, archive, and clone isolated workspace environments",
        icon: "📦",
        perform: () => navigate("/templates"),
      },
      {
        id: "nav-audit",
        category: "Navigation",
        title: "Audit Log",
        subtitle: "Inspect chronological system events and execution trace",
        icon: "📜",
        perform: () => navigate("/audit"),
      },
    ];

    // Add recent sessions to commands
    for (const s of sessions.slice(0, 8)) {
      items.push({
        id: `session-${s.id}`,
        category: "Conversations",
        title: s.goal || "Untitled Conversation",
        subtitle: `Session ID: ${s.id.slice(0, 8)} · ${s.status}`,
        icon: "🗨️",
        perform: () => navigate(`/chat?session=${s.id}`),
      });
    }

    return items;
  }, [navigate, sessions]);

  const filteredCommands = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (c) =>
        c.title.toLowerCase().includes(q) ||
        (c.subtitle && c.subtitle.toLowerCase().includes(q)) ||
        c.category.toLowerCase().includes(q),
    );
  }, [commands, query]);

  useEffect(() => {
    setSelectedIndex(0);
  }, [filteredCommands]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((prev) => (prev + 1) % Math.max(1, filteredCommands.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((prev) => (prev - 1 + filteredCommands.length) % Math.max(1, filteredCommands.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const selected = filteredCommands[selectedIndex];
      if (selected) {
        selected.perform();
      }
    }
  };

  if (!open) return null;

  return (
    <div className="cmd-palette-backdrop" onClick={() => setOpen(false)}>
      <div
        className="cmd-palette-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Command Palette"
      >
        <div className="cmd-palette-input-wrap">
          <span className="cmd-search-icon">🔍</span>
          <input
            type="text"
            className="cmd-palette-input"
            placeholder="Type a command, session name, or destination… (Esc to close)"
            value={query}
            autoFocus
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          <span className="cmd-esc-tag">ESC</span>
        </div>

        <div className="cmd-palette-list" role="listbox">
          {filteredCommands.length === 0 ? (
            <div className="cmd-empty">No matching commands or sessions found</div>
          ) : (
            filteredCommands.map((cmd, idx) => {
              const active = idx === selectedIndex;
              return (
                <div
                  key={cmd.id}
                  role="option"
                  aria-selected={active}
                  className={`cmd-item${active ? " active" : ""}`}
                  onClick={() => cmd.perform()}
                  onMouseEnter={() => setSelectedIndex(idx)}
                >
                  <span className="cmd-item-icon">{cmd.icon}</span>
                  <div className="cmd-item-body">
                    <div className="cmd-item-title-row">
                      <span className="cmd-item-title">{cmd.title}</span>
                      <span className="cmd-item-category">{cmd.category}</span>
                    </div>
                    {cmd.subtitle && (
                      <span className="cmd-item-subtitle">{cmd.subtitle}</span>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>
        <div className="cmd-palette-footer">
          <span><strong>↑↓</strong> navigate</span>
          <span><strong>Enter</strong> select</span>
          <span><strong>Esc</strong> close</span>
          <span><strong>Cmd+K</strong> toggle</span>
        </div>
      </div>
    </div>
  );
}
