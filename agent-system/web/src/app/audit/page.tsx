"use client";

import { useEffect, useMemo, useState } from "react";
import { api, EventPoller, type EventOut } from "@/lib/api";
import { Skeleton } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

const MAX_EVENTS = 500;
const PAGE_SIZE = 100;

function toCsv(rows: EventOut[]): string {
  const esc = (v: string) => `"${v.replace(/"/g, '""')}"`;
  const head = "sequence,timestamp,type,actor,session_id,task_id,payload";
  const lines = rows.map((e) =>
    [
      String(e.sequence),
      e.timestamp,
      e.type,
      e.actor,
      e.session_id ?? "",
      e.task_id ?? "",
      JSON.stringify(e.payload ?? {}),
    ]
      .map(esc)
      .join(","),
  );
  return [head, ...lines].join("\n");
}

export default function AuditPage() {
  const [events, setEvents] = useState<EventOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(true);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [page, setPage] = useState(0);
  const toast = useToast();

  useEffect(() => {
    // Load history first, then tail live with resume-from-sequence.
    const poller = new EventPoller(
      (incoming) => {
        setConnected(true);
        setEvents((prev) => [...prev, ...incoming].slice(-MAX_EVENTS));
      },
      () => setConnected(false),
      2000,
    );
    void api
      .eventsAfter(0, 200)
      .then((rows) => {
        setEvents(rows.slice(-MAX_EVENTS));
        setConnected(true);
        setLoading(false);
        poller.start();
      })
      .catch((err) => {
        setError((err as Error).message);
        setConnected(false);
        setLoading(false);
      });
    return () => poller.stop();
  }, []);

  const types = useMemo(() => [...new Set(events.map((e) => e.type))].sort(), [events]);

  const filtered = useMemo(() => {
    let rows = events;
    if (typeFilter !== "all") rows = rows.filter((e) => e.type === typeFilter);
    const q = query.trim().toLowerCase();
    if (q) {
      rows = rows.filter(
        (e) =>
          e.type.toLowerCase().includes(q) ||
          e.actor.toLowerCase().includes(q) ||
          JSON.stringify(e.payload ?? {}).toLowerCase().includes(q),
      );
    }
    return [...rows].reverse();
  }, [events, typeFilter, query]);

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const pageRows = filtered.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);

  const exportCsv = () => {
    try {
      const blob = new Blob([toCsv(filtered)], { type: "text/csv" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "audit-events.csv";
      a.click();
      URL.revokeObjectURL(url);
      toast(`Exported ${filtered.length} events`, "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div>
      <h1 className="page-title">Audit Log</h1>
      <p className="muted">
        {connected ? "🟢 tailing canonical events" : "🔴 connection lost — will retry"} ·{" "}
        {events.length} events (capped at {MAX_EVENTS})
      </p>
      {error && (
        <ErrorBanner
          message={error}
          onRetry={() => window.location.reload()}
          onDismiss={() => setError(null)}
        />
      )}

      <div className="toolbar" role="search">
        <label htmlFor="audit-search" className="visually-hidden">
          Search by outcome, actor, or payload
        </label>
        <input
          id="audit-search"
          className="input"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setPage(0); }}
          placeholder="Search outcome, actor, payload…"
        />
        <label htmlFor="audit-type" className="muted">
          Type
        </label>
        <select
          id="audit-type"
          value={typeFilter}
          onChange={(e) => { setTypeFilter(e.target.value); setPage(0); }}
        >
          <option value="all">All</option>
          {types.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <button className="btn secondary" onClick={exportCsv} disabled={filtered.length === 0}>
          Export CSV ({filtered.length})
        </button>
      </div>

      {loading ? (
        <Skeleton lines={8} />
      ) : filtered.length === 0 && !error ? (
        <EmptyState icon="📜" title="No events match" hint="Adjust the search or type filter." />
      ) : (
        <>
          <table className="events">
            <thead>
              <tr>
                <th>#</th>
                <th>time</th>
                <th>type</th>
                <th>actor</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((e) => (
                <tr key={e.event_id}>
                  <td>{e.sequence}</td>
                  <td>{new Date(e.timestamp).toLocaleTimeString()}</td>
                  <td>{e.type}</td>
                  <td>{e.actor}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {pages > 1 && (
            <div className="pagination">
              <button
                className="btn secondary btn-sm"
                disabled={safePage === 0}
                onClick={() => setPage(safePage - 1)}
              >
                ← Prev
              </button>
              <span>
                Page {safePage + 1} of {pages} ({PAGE_SIZE}/page)
              </span>
              <button
                className="btn secondary btn-sm"
                disabled={safePage >= pages - 1}
                onClick={() => setPage(safePage + 1)}
              >
                Next →
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
