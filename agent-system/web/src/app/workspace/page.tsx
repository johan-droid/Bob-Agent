"use client";

import { useEffect, useState } from "react";
import { api, type WorkspaceOut } from "@/lib/api";
import { Skeleton } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

function b64ToText(b64: string): string {
  const bin = atob(b64);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function textToB64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

export default function WorkspacePage() {
  const [workspaces, setWorkspaces] = useState<WorkspaceOut[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [files, setFiles] = useState<string[]>([]);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState<string | null>(null);
  const [previewBinary, setPreviewBinary] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const toast = useToast();

  const refresh = async () => {
    try {
      setWorkspaces(await api.listWorkspaces());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) return;
    setPreviewPath(null);
    setPreviewText(null);
    void api
      .workspaceTree(selected)
      .then((r) => setFiles(r.files))
      .catch(() => setFiles([]));
  }, [selected]);

  const create = async () => {
    if (!newName.trim()) return;
    try {
      const ws = await api.createWorkspace(newName.trim());
      setNewName("");
      await refresh();
      setSelected(ws.id);
      toast("Workspace created", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const remove = async (id: string, name: string) => {
    if (!window.confirm(`Delete workspace "${name}"? This cannot be undone.`)) return;
    try {
      await api.deleteWorkspace(id);
      if (selected === id) {
        setSelected(null);
        setFiles([]);
      }
      await refresh();
      toast("Workspace deleted", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const preview = async (path: string) => {
    if (!selected) return;
    setPreviewPath(path);
    setPreviewText(null);
    setPreviewBinary(false);
    setEditing(false);
    try {
      const data = await api.readWorkspaceFile(selected, path);
      if (data.size > 200_000) {
        setPreviewBinary(true);
        return;
      }
      const text = b64ToText(data.content_b64);
      // eslint-disable-next-line no-control-regex
      if (text.includes("\u0000")) {
        setPreviewBinary(true);
        return;
      }
      setPreviewText(text);
      setDraft(text);
    } catch (err) {
      toast((err as Error).message, "error");
      setPreviewPath(null);
    }
  };

  const saveFile = async () => {
    if (!selected || !previewPath) return;
    try {
      await api.writeWorkspaceFile(selected, previewPath, textToB64(draft));
      setPreviewText(draft);
      setEditing(false);
      toast("File saved", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div>
      <h1 className="page-title">Workspace</h1>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}

      <div className="card">
        <h3>New workspace</h3>
        <div className="form-row">
          <label htmlFor="ws-name" className="visually-hidden">
            Workspace name
          </label>
          <input
            id="ws-name"
            className="input"
            value={newName}
            placeholder="workspace name"
            onChange={(e) => setNewName(e.target.value)}
          />
          <button className="btn" onClick={() => void create()}>
            Create
          </button>
        </div>
      </div>

      <div className="card">
        <h3>Workspaces</h3>
        {loading ? (
          <Skeleton lines={3} />
        ) : workspaces.length === 0 ? (
          <EmptyState icon="🖥️" title="No workspaces yet" hint="Create one above to start browsing files." />
        ) : (
          workspaces.map((w) => (
            <div key={w.id} className="toolbar ws-row">
              <button
                type="button"
                className="btn secondary"
                aria-pressed={selected === w.id}
                onClick={() => setSelected(w.id)}
              >
                {w.name}
              </button>
              <span className="muted">
                ({w.file_count} files · {w.status})
              </span>
              <button
                type="button"
                className="btn danger btn-sm"
                onClick={() => void remove(w.id, w.name)}
                aria-label={`Delete workspace ${w.name}`}
              >
                Delete
              </button>
            </div>
          ))
        )}
      </div>

      {selected && (
        <div className="card">
          <h3>Files</h3>
          {files.length === 0 ? (
            <p className="muted">Empty workspace.</p>
          ) : (
            files.map((f) => (
              <button
                key={f}
                type="button"
                className="btn secondary file-btn"
                onClick={() => void preview(f)}
                aria-label={`Preview file ${f}`}
              >
                {f}
              </button>
            ))
          )}
        </div>
      )}

      {previewPath && (
        <div className="card">
          <h3>Preview: {previewPath}</h3>
          {previewText === null && !previewBinary ? (
            <Skeleton lines={6} />
          ) : previewBinary ? (
            <p className="muted">
              Binary or large file — text preview unavailable. The backend serves raw bytes via
              GET /api/v1/workspaces/{"{id}"}/file; in-browser rendering is limited to text.
            </p>
          ) : editing ? (
            <>
              <label htmlFor="ws-editor" className="visually-hidden">
                Edit file {previewPath}
              </label>
              <textarea
                id="ws-editor"
                className="json-editor"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
              <div className="form-row">
                <button className="btn" onClick={() => void saveFile()}>
                  Save (PUT file endpoint)
                </button>
                <button className="btn secondary" onClick={() => { setEditing(false); setDraft(previewText ?? ""); }}>
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <>
              <pre className="preview-pre">{previewText}</pre>
              <div className="form-row">
                <button className="btn secondary" onClick={() => setEditing(true)}>
                  Edit
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
