"use client";

import { useEffect, useState, useMemo, useCallback } from "react";
import { api, type VaultNote, type VaultNoteDetail } from "@/lib/api";
import { Skeleton } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

const LAYERS = [
  { id: "ALL", label: "All Layers", icon: "📚" },
  { id: "SYSTEM", label: "System", icon: "🛡️" },
  { id: "USER", label: "User", icon: "👤" },
  { id: "TASK", label: "Task", icon: "⚡" },
  { id: "WORKSPACE", label: "Workspace", icon: "🖥️" },
];

export default function VaultPage() {
  const [notes, setNotes] = useState<VaultNote[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [selectedNote, setSelectedNote] = useState<VaultNoteDetail | null>(null);
  const [activeLayer, setActiveLayer] = useState<string>("ALL");
  const [search, setSearch] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [loadingNote, setLoadingNote] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createDraft, setCreateDraft] = useState({
    title: "",
    layer: "USER",
    source: "user",
    tags: "",
    links: "",
    body: "",
  });
  const [saving, setSaving] = useState(false);
  const toast = useToast();

  const loadNotes = useCallback(async () => {
    try {
      const layerParam = activeLayer === "ALL" ? undefined : activeLayer;
      const data = await api.listVaultNotes(layerParam, search || undefined);
      setNotes(data);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [activeLayer, search]);

  useEffect(() => {
    void loadNotes();
  }, [loadNotes]);

  // Load single note detail when selected
  useEffect(() => {
    if (!selectedPath) {
      setSelectedNote(null);
      return;
    }
    setLoadingNote(true);
    api
      .getVaultNote(selectedPath)
      .then(setSelectedNote)
      .catch((err) => {
        toast(`Failed to load note: ${(err as Error).message}`, "error");
        setSelectedNote(null);
      })
      .finally(() => setLoadingNote(false));
  }, [selectedPath, toast]);

  const handleDeleteNote = async (path: string) => {
    if (!confirm("Are you sure you want to delete this memory note?")) return;
    try {
      await api.deleteVaultNote(path);
      toast("Note deleted", "success");
      if (selectedPath === path) {
        setSelectedPath(null);
        setSelectedNote(null);
      }
      await loadNotes();
    } catch (err) {
      toast(`Failed to delete: ${(err as Error).message}`, "error");
    }
  };

  const handleCreateNote = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!createDraft.title.trim()) {
      toast("Note title is required", "error");
      return;
    }
    setSaving(true);
    try {
      const tags = createDraft.tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
      const links = createDraft.links
        .split(",")
        .map((l) => l.trim().replace(/^\[\[|\]\]$/g, ""))
        .filter(Boolean);

      const created = await api.createVaultNote({
        title: createDraft.title.trim(),
        layer: createDraft.layer,
        source: createDraft.source || "user",
        body: createDraft.body,
        tags,
        links,
      });

      toast("Memory note saved to Obsidian vault", "success");
      setShowCreateModal(false);
      setCreateDraft({
        title: "",
        layer: "USER",
        source: "user",
        tags: "",
        links: "",
        body: "",
      });
      await loadNotes();
      setSelectedPath(created.path);
    } catch (err) {
      toast(`Failed to save note: ${(err as Error).message}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const layerCounts = useMemo(() => {
    const counts: Record<string, number> = { ALL: notes.length };
    for (const n of notes) {
      const key = (n.layer || "USER").toUpperCase();
      counts[key] = (counts[key] || 0) + 1;
    }
    return counts;
  }, [notes]);

  return (
    <div className="vault-container">
      <div className="vault-header">
        <div className="vault-header-title">
          <h1 className="page-title" style={{ margin: 0 }}>
            Obsidian Memory Vault
          </h1>
          <p className="muted" style={{ margin: "4px 0 0" }}>
            Bi-directional memory notes with YAML frontmatter, [[wiki-links]], and semantic embeddings.
          </p>
        </div>
        <div className="vault-header-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setShowCreateModal(true)}
          >
            + Add Memory Note
          </button>
        </div>
      </div>

      {error && <ErrorBanner message={error} onRetry={() => void loadNotes()} />}

      {/* Layer Filter Tabs & Search Bar */}
      <div className="vault-toolbar">
        <div className="vault-layer-tabs">
          {LAYERS.map((layer) => (
            <button
              key={layer.id}
              type="button"
              className={`vault-layer-tab ${activeLayer === layer.id ? "active" : ""}`}
              onClick={() => setActiveLayer(layer.id)}
            >
              <span className="tab-icon">{layer.icon}</span>
              <span>{layer.label}</span>
              <span className="count-badge">{layerCounts[layer.id] || 0}</span>
            </button>
          ))}
        </div>

        <div className="vault-search-box">
          <span className="search-icon">🔍</span>
          <input
            type="text"
            className="search-input"
            placeholder="Search notes by title, tag, content..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {search && (
            <button
              type="button"
              className="clear-search-btn"
              onClick={() => setSearch("")}
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* Vault Master-Detail Layout */}
      <div className="vault-layout">
        {/* Notes List Column */}
        <div className="vault-list-pane">
          {loading ? (
            <div style={{ padding: 12 }}>
              <Skeleton lines={5} />
            </div>
          ) : notes.length === 0 ? (
            <EmptyState
              icon="📚"
              title="No memory notes found"
              hint={
                search
                  ? "Try adjusting your search query or switching layer filters."
                  : "Notes generated during agent tasks or manually created will appear here."
              }
            />
          ) : (
            <div className="vault-notes-list">
              {notes.map((note) => {
                const isSelected = selectedPath === note.path;
                return (
                  <button
                    type="button"
                    key={note.path}
                    className={`vault-note-item ${isSelected ? "selected" : ""}`}
                    onClick={() => setSelectedPath(note.path)}
                  >
                    <div className="note-item-top">
                      <span className={`layer-badge layer-${note.layer?.toLowerCase()}`}>
                        {note.layer}
                      </span>
                      <span className="note-date">
                        {note.created ? new Date(note.created).toLocaleDateString() : ""}
                      </span>
                    </div>
                    <div className="note-item-title">{note.title}</div>
                    {note.tags && note.tags.length > 0 && (
                      <div className="note-item-tags">
                        {note.tags.slice(0, 3).map((tag, idx) => (
                          <span key={idx} className="tag-pill">
                            #{tag}
                          </span>
                        ))}
                        {note.tags.length > 3 && (
                          <span className="tag-more">+{note.tags.length - 3}</span>
                        )}
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Note Reader Pane */}
        <div className="vault-reader-pane">
          {loadingNote ? (
            <div style={{ padding: 24 }}>
              <Skeleton lines={8} />
            </div>
          ) : selectedNote ? (
            <div className="note-reader-content">
              {/* Header */}
              <div className="note-reader-header">
                <div>
                  <div className="note-header-badges">
                    <span className={`layer-badge layer-${selectedNote.layer?.toLowerCase()}`}>
                      {selectedNote.layer}
                    </span>
                    <span className="source-badge">Source: {selectedNote.source}</span>
                    {selectedNote.session_id && (
                      <span className="meta-badge">Session: {selectedNote.session_id.slice(0, 8)}</span>
                    )}
                    {selectedNote.task_id && (
                      <span className="meta-badge">Task: {selectedNote.task_id.slice(0, 8)}</span>
                    )}
                  </div>
                  <h2 className="note-reader-title">{selectedNote.title}</h2>
                  <div className="note-reader-path">
                    📄 <code>{selectedNote.path}</code> · {(selectedNote.size_bytes / 1024).toFixed(1)} KB
                  </div>
                </div>

                <div className="note-reader-actions">
                  <button
                    type="button"
                    className="btn btn-sm btn-secondary"
                    onClick={() => {
                      void navigator.clipboard.writeText(selectedNote.body);
                      toast("Copied markdown to clipboard", "success");
                    }}
                    title="Copy Markdown"
                  >
                    📋 Copy
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm btn-danger"
                    onClick={() => void handleDeleteNote(selectedNote.path)}
                    title="Delete Note"
                  >
                    🗑️ Delete
                  </button>
                </div>
              </div>

              {/* Tags & Wiki-links metadata bar */}
              {(selectedNote.tags?.length > 0 || selectedNote.links?.length > 0) && (
                <div className="note-metadata-bar">
                  {selectedNote.tags?.length > 0 && (
                    <div className="meta-section">
                      <span className="meta-label">Tags:</span>
                      <div className="tags-flex">
                        {selectedNote.tags.map((t, idx) => (
                          <span key={idx} className="tag-pill">
                            #{t}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {selectedNote.links?.length > 0 && (
                    <div className="meta-section">
                      <span className="meta-label">Wiki-links:</span>
                      <div className="links-flex">
                        {selectedNote.links.map((link, idx) => (
                          <span key={idx} className="wiki-link-badge">
                            [[{link}]]
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Note Markdown Content Body */}
              <div className="note-body-viewer">
                <div className="markdown-prose">
                  {selectedNote.body.split("\n\n").map((paragraph, idx) => {
                    if (paragraph.startsWith("# ")) {
                      return <h1 key={idx}>{paragraph.slice(2)}</h1>;
                    }
                    if (paragraph.startsWith("## ")) {
                      return <h2 key={idx}>{paragraph.slice(3)}</h2>;
                    }
                    if (paragraph.startsWith("### ")) {
                      return <h3 key={idx}>{paragraph.slice(4)}</h3>;
                    }
                    if (paragraph.startsWith("```")) {
                      return (
                        <pre key={idx} className="code-block-view">
                          <code>{paragraph.replace(/^```[a-z]*\n|```$/g, "")}</code>
                        </pre>
                      );
                    }
                    if (paragraph.startsWith("- ")) {
                      return (
                        <ul key={idx}>
                          {paragraph.split("\n").map((li, lIdx) => (
                            <li key={lIdx}>{li.replace(/^- /, "")}</li>
                          ))}
                        </ul>
                      );
                    }
                    return <p key={idx}>{paragraph}</p>;
                  })}
                </div>
              </div>
            </div>
          ) : (
            <div className="reader-placeholder">
              <span className="placeholder-icon">📖</span>
              <h3>Select a note to read</h3>
              <p className="muted">
                Choose a note from the left sidebar to preview its contents, frontmatter metadata, and wiki-links.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Modal: Add Memory Note */}
      {showCreateModal && (
        <div className="modal-backdrop">
          <div className="modal-dialog">
            <div className="modal-header">
              <h3>Create Obsidian Memory Note</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowCreateModal(false)}
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleCreateNote}>
              <div className="modal-body">
                <div className="form-group">
                  <label htmlFor="note-title">Note Title *</label>
                  <input
                    id="note-title"
                    type="text"
                    className="form-input"
                    placeholder="e.g. Authentication Strategy & Keys"
                    value={createDraft.title}
                    onChange={(e) =>
                      setCreateDraft((prev) => ({ ...prev, title: e.target.value }))
                    }
                    required
                  />
                </div>

                <div className="form-row" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                  <div className="form-group">
                    <label htmlFor="note-layer">Memory Layer</label>
                    <select
                      id="note-layer"
                      className="form-select"
                      value={createDraft.layer}
                      onChange={(e) =>
                        setCreateDraft((prev) => ({ ...prev, layer: e.target.value }))
                      }
                    >
                      <option value="USER">User (Personal Preferences & Context)</option>
                      <option value="SYSTEM">System (Architecture & Security)</option>
                      <option value="TASK">Task (Execution Outcomes)</option>
                      <option value="WORKSPACE">Workspace (Repo Context)</option>
                    </select>
                  </div>

                  <div className="form-group">
                    <label htmlFor="note-source">Source Label</label>
                    <input
                      id="note-source"
                      type="text"
                      className="form-input"
                      placeholder="e.g. user, manual, agent"
                      value={createDraft.source}
                      onChange={(e) =>
                        setCreateDraft((prev) => ({ ...prev, source: e.target.value }))
                      }
                    />
                  </div>
                </div>

                <div className="form-row" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                  <div className="form-group">
                    <label htmlFor="note-tags">Tags (comma-separated)</label>
                    <input
                      id="note-tags"
                      type="text"
                      className="form-input"
                      placeholder="e.g. auth, security, jwt"
                      value={createDraft.tags}
                      onChange={(e) =>
                        setCreateDraft((prev) => ({ ...prev, tags: e.target.value }))
                      }
                    />
                  </div>

                  <div className="form-group">
                    <label htmlFor="note-links">Wiki-links (comma-separated)</label>
                    <input
                      id="note-links"
                      type="text"
                      className="form-input"
                      placeholder="e.g. UserAuth, ApiConfig"
                      value={createDraft.links}
                      onChange={(e) =>
                        setCreateDraft((prev) => ({ ...prev, links: e.target.value }))
                      }
                    />
                  </div>
                </div>

                <div className="form-group">
                  <label htmlFor="note-body">Markdown Content *</label>
                  <textarea
                    id="note-body"
                    className="form-textarea"
                    rows={8}
                    placeholder="Write detailed notes, documentation, or decisions here in Markdown..."
                    value={createDraft.body}
                    onChange={(e) =>
                      setCreateDraft((prev) => ({ ...prev, body: e.target.value }))
                    }
                    required
                  />
                </div>
              </div>

              <div className="modal-footer">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => setShowCreateModal(false)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={saving || !createDraft.title.trim() || !createDraft.body.trim()}
                >
                  {saving ? "Saving to Vault…" : "Save Note"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
