"use client";

/**
 * Cost tab — real /api/v1/model-calls (Phase 11 service + Phase 9 wiring).
 * Every row is a real ModelCall record; estimated usage/cost is badged, never
 * presented as exact (v3.1 §33).
 */

import { useEffect, useMemo, useState } from "react";
import { api, type ModelCall } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { ErrorBanner } from "@/components/ErrorBanner";

const PAGE_SIZE = 50;

export default function CostPage() {
  const [calls, setCalls] = useState<ModelCall[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);

  const refresh = async () => {
    try {
      setCalls(await api.listModelCalls(undefined, 500));
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

  const totalCost = calls.reduce((sum, c) => sum + (c.cost_usd ?? 0), 0);
  const totalTokens = calls.reduce(
    (sum, c) => sum + (c.tokens_in ?? 0) + (c.tokens_out ?? 0),
    0,
  );
  const anyEstimated = calls.some((c) => c.usage_is_estimated || c.cost_is_estimated);

  const byModel = useMemo(() => {
    const map = new Map<string, { cost: number; count: number }>();
    for (const c of calls) {
      const key = `${c.provider} / ${c.model_id}`;
      const prev = map.get(key) ?? { cost: 0, count: 0 };
      prev.cost += c.cost_usd ?? 0;
      prev.count += 1;
      map.set(key, prev);
    }
    const rows = [...map.entries()].sort((a, b) => b[1].cost - a[1].cost);
    const max = rows.length > 0 ? Math.max(...rows.map(([, v]) => v.cost), 0) : 0;
    return { rows, max };
  }, [calls]);

  const pages = Math.max(1, Math.ceil(calls.length / PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const pageRows = calls.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);

  return (
    <div>
      <h1 className="page-title">Cost</h1>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}

      {loading ? (
        <SkeletonCard />
      ) : (
        <>
          <div className="card">
            <h3>
              {calls.length} model calls · ${totalCost.toFixed(4)} recorded ·{" "}
              {totalTokens.toLocaleString()} tokens
              {anyEstimated && <span className="est-badge">includes estimates</span>}
            </h3>
            {calls.length === 0 && !error ? (
              <p className="muted">No model calls yet.</p>
            ) : (
              <>
                <h3>Breakdown by model</h3>
                {byModel.rows.map(([model, v]) => (
                  <div className="cost-bar-row" key={model}>
                    <span>{model}</span>
                    <div className="cost-bar-track">
                      <div
                        className="cost-bar-fill"
                        style={{ width: `${byModel.max > 0 ? (v.cost / byModel.max) * 100 : 0}%` }}
                      />
                    </div>
                    <span className="muted">
                      ${v.cost.toFixed(5)} · {v.count} calls
                    </span>
                  </div>
                ))}
              </>
            )}
          </div>

          {pageRows.map((c) => (
            <div className="card" key={c.id}>
              <h3>
                {c.provider} / {c.model_id}{" "}
                <span className={c.status === "ok" ? "state-SUCCEEDED" : "state-FAILED"}>
                  [{c.status}]
                </span>
                {c.usage_is_estimated && <span className="est-badge">estimated usage</span>}
                {c.cost_is_estimated && <span className="est-badge">estimated cost</span>}
              </h3>
              <p className="muted">
                in {c.tokens_in ?? "?"} / out {c.tokens_out ?? "?"}
                {c.tokens_cached ? ` · cached ${c.tokens_cached}` : ""}
                {c.cost_usd !== null && ` · $${c.cost_usd.toFixed(5)}`}
                {c.latency_ms !== null && ` · ${c.latency_ms} ms`}
              </p>
            </div>
          ))}

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
                Page {safePage + 1} of {pages} ({calls.length} calls, {PAGE_SIZE}/page)
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
