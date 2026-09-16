"use client";

import { useState, useMemo } from "react";
import type { Session } from "@/lib/api";

interface ChatSidebarProps {
  sessions: Session[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onRefresh: () => void;
  onRenameSession?: (id: string, newGoal: string) => Promise<void>;
  onDeleteSession?: (id: string) => Promise<void>;
  onExportSession?: (id: string, format: "markdown" | "json") => void;
}

export function ChatSidebar({
  sessions,
  selectedId,
  onSelect,
  onNewChat,
  onRefresh,
  onRenameSession,
  onDeleteSession,
  onExportSession,
}: ChatSidebarProps) {
  const [search, setSearch] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editGoal, setEditGoal] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const filteredSessions = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(
      (s) => s.goal.toLowerCase().includes(q) || s.id.toLowerCase().includes(q),
    );
  }, [sessions, search]);

  const handleStartRename = (s: Session, e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingId(s.id);
    setEditGoal(s.goal);
  };

  const handleSaveRename = async (id: string, e: React.FormEvent | React.MouseEvent) => {
    e.stopPropagation();
    if (!editGoal.trim() || !onRenameSession) {
      setEditingId(null);
      return;
    }
    await onRenameSession(id, editGoal.trim());
    setEditingId(null);
  };

  const handleCancelRename = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingId(null);
  };

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (deletingId === id) {
      if (onDeleteSession) {
        await onDeleteSession(id);
      }
      setDeletingId(null);
    } else {
      setDeletingId(id);
      setTimeout(() => {
        setDeletingId((curr) => (curr === id ? null : curr));
      }, 4000);
    }
  };

  return (
    <div className="chat-sidebar">
      <div className="chat-sidebar-header">
        <div className="chat-sidebar-title-row">
          <h3>Conversations</h3>
          <span className="session-count-badge">{sessions.length}</span>
        </div>
        <button
          type="button"
          className="new-chat-btn"
          onClick={onNewChat}
          aria-label="Start new conversation"
        >
          <span>＋</span> New Chat
        </button>
      </div>

      <div className="chat-search-bar">
        <input
          type="text"
          className="chat-search-input"
          placeholder="Search chats…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search conversation history"
        />
        {search && (
          <button
            type="button"
            className="search-clear-btn"
            onClick={() => setSearch("")}
            aria-label="Clear search"
          >
            ✕
          </button>
        )}
      </div>

      <div className="session-list" role="listbox" aria-label="Conversations">
        {filteredSessions.length === 0 ? (
          <div className="session-empty-container">
            <p className="muted session-empty">
              {search ? "No matches found" : "No conversations yet"}
            </p>
          </div>
        ) : (
          filteredSessions.map((s) => {
            const active = selectedId === s.id;
            const isEditing = editingId === s.id;
            const isConfirmingDelete = deletingId === s.id;

            return (
              <div
                key={s.id}
                role="option"
                aria-selected={active}
                className={`session-item-wrapper${active ? " active" : ""}`}
                onClick={() => !isEditing && onSelect(s.id)}
              >
                {isEditing ? (
                  <div className="session-rename-box" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="text"
                      className="session-rename-input"
                      value={editGoal}
                      autoFocus
                      onChange={(e) => setEditGoal(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void handleSaveRename(s.id, e);
                        if (e.key === "Escape") setEditingId(null);
                      }}
                    />
                    <div className="rename-actions">
                      <button
                        type="button"
                        className="btn-icon"
                        title="Save"
                        onClick={(e) => void handleSaveRename(s.id, e)}
                      >
                        ✓
                      </button>
                      <button
                        type="button"
                        className="btn-icon"
                        title="Cancel"
                        onClick={handleCancelRename}
                      >
                        ✕
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="session-info">
                      <span className="session-goal" title={s.goal}>
                        {s.goal || "Untitled Conversation"}
                      </span>
                      <div className="session-submeta">
                        <span className="session-id-tag">{s.id.slice(0, 7)}</span>
                        <span className={`session-status-tag status-${s.status.toLowerCase()}`}>
                          {s.status}
                        </span>
                      </div>
                    </div>
                    <div className="session-item-actions">
                      {onRenameSession && (
                        <button
                          type="button"
                          className="session-action-btn"
                          title="Rename"
                          onClick={(e) => handleStartRename(s, e)}
                          aria-label={`Rename session ${s.goal}`}
                        >
                          ✎
                        </button>
                      )}
                      {onExportSession && (
                        <button
                          type="button"
                          className="session-action-btn"
                          title="Export Markdown"
                          onClick={(e) => {
                            e.stopPropagation();
                            onExportSession(s.id, "markdown");
                          }}
                          aria-label="Export conversation"
                        >
                          ⬇
                        </button>
                      )}
                      {onDeleteSession && (
                        <button
                          type="button"
                          className={`session-action-btn delete-btn${isConfirmingDelete ? " confirm" : ""}`}
                          title={isConfirmingDelete ? "Click again to delete" : "Delete"}
                          onClick={(e) => void handleDelete(s.id, e)}
                          aria-label={`Delete session ${s.goal}`}
                        >
                          {isConfirmingDelete ? "🗑️ Del?" : "🗑️"}
                        </button>
                      )}
                    </div>
                  </>
                )}
              </div>
            );
          })
        )}
      </div>

      <div className="chat-sidebar-footer">
        <button
          type="button"
          className="refresh-sessions-btn"
          onClick={onRefresh}
          aria-label="Refresh conversations"
        >
          ↻ Refresh List
        </button>
      </div>
    </div>
  );
}
