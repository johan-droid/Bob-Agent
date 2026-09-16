"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type Artifact } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

type SortKey = "newest" | "size" | "name";

export default function OutputsPage() {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [kindFilter, setKindFilter] = useState("all");
  const [sort, setSort] = useState<SortKey>("newest");
  const [preview, setPreview] = useState<Artifact | null>(null);
  const toast = useToast();

  const refresh = async () => {
    try {
      setArtifacts(await api.listArtifacts());
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

  const kinds = useMemo(() => [...new Set(artifacts.map((a) => a.kind))].sort(), [artifacts]);

  const visible = useMemo(() => {
    let rows = artifacts;
    if (kindFilter !== "all") rows = rows.filter((a) => a.kind === kindFilter);
    const sorted = [...rows];
    if (sort === "size") sorted.sort((a, b) => b.size_bytes - a.size_bytes);
    else if (sort === "name") sorted.sort((a, b) => a.path.localeCompare(b.path));
    return sorted;
  }, [artifacts, kindFilter, sort]);

  const openPreview = async (a: Artifact) => {
    try {
      // Re-fetch canonical metadata; artifact bytes have no download
      // endpoint yet, so preview shows metadata only (honest, v3.1 §33).
      const full = await api.getArtifact(a.id);
      setPreview(full);
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div>
      <h1 className="page-title">Outputs</h1>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}

      <div className="toolbar">
        <label htmlFor="outputs-kind" className="muted">
          Filter
        </label>
        <select id="outputs-kind" value={kindFilter} onChange={(e) => setKindFilter(e.target.value)}>
          <option value="all">All</option>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <label htmlFor="outputs-sort" className="muted">
          Sort
        </label>
        <select id="outputs-sort" value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
          <option value="newest">Newest</option>
          <option value="size">Size</option>
          <option value="name">Name</option>
        </select>
      </div>

      {loading ? (
        <>
          <SkeletonCard />
          <SkeletonCard />
        </>
      ) : visible.length === 0 && !error ? (
        <EmptyState
          icon="📁"
          title="No artifacts yet"
          hint="Generated documents will appear here."
        />
      ) : (
        visible.map((a) => (
          <div className="card" key={a.id}>
            <h3>
              {a.kind.toUpperCase()} · {a.path}
            </h3>
            <p className="muted">
              {(a.size_bytes / 1024).toFixed(1)} KB · task {a.task_id ?? "—"}
            </p>
            <div className="approval-actions">
              <button className="btn secondary" onClick={() => void openPreview(a)}>
                Preview
              </button>
              <span title="Backend API pending: no artifact download endpoint yet">
                <button className="btn secondary" disabled aria-disabled="true">
                  Download
                </button>
              </span>
              <span title="Backend API pending: no artifact delete endpoint yet">
                <button className="btn danger" disabled aria-disabled="true">
                  Delete
                </button>
              </span>
            </div>
            <p className="disabled-note">
              Download/Delete are disabled — the backend exposes artifact metadata only
              (GET /api/v1/artifacts). No mocked files are shown.
            </p>
          </div>
        ))
      )}

      {preview && (
        <div className="modal-backdrop" onClick={() => setPreview(null)}>
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label={`Artifact ${preview.path}`}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header">
              <h2>{preview.path}</h2>
              <button className="modal-close" onClick={() => setPreview(null)} aria-label="Close">
                ×
              </button>
            </div>
            <p className="muted">
              id {preview.id} · kind {preview.kind} · {(preview.size_bytes / 1024).toFixed(1)} KB ·
              task {preview.task_id ?? "—"}
            </p>
            <p className="muted">
              File bytes live on the backend host at this server-side path; there is no download
              endpoint yet, so in-browser preview of document contents is pending backend support.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
