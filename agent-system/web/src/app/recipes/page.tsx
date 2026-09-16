"use client";

/**
 * Recipes tab — real /api/v1/recipes (Phase 16). No mocked data (v3.1 §33).
 * Execute posts real params; execution history has no backend endpoint, so
 * the executions count from the recipe record is shown with an honest note.
 */

import { useEffect, useState } from "react";
import { api, type Recipe } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

export default function RecipesPage() {
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [paramDrafts, setParamDrafts] = useState<Record<string, string>>({});
  const [paramError, setParamError] = useState<string | null>(null);
  const toast = useToast();

  const refresh = async () => {
    try {
      setRecipes(await api.listRecipes());
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

  const toggle = async (id: string) => {
    if (expanded === id) {
      setExpanded(null);
      return;
    }
    setExpanded(id);
    setParamError(null);
    try {
      const full = await api.getRecipe(id);
      setRecipes((prev) => prev.map((r) => (r.recipe_id === id ? { ...r, ...full } : r)));
      setParamDrafts((prev) => {
        if (prev[id] !== undefined) return prev;
        const defaults: Record<string, unknown> = full.parameters ?? {};
        const init: Record<string, string> = {};
        for (const [k, v] of Object.entries(defaults)) init[k] = String(v ?? "");
        return { ...prev, [id]: JSON.stringify(init, null, 2) };
      });
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const run = async (id: string) => {
    let params: Record<string, unknown> = {};
    const raw = (paramDrafts[id] ?? "").trim();
    if (raw) {
      try {
        params = JSON.parse(raw) as Record<string, unknown>;
        setParamError(null);
      } catch {
        setParamError("Parameters must be valid JSON.");
        return;
      }
    }
    setBusy(id);
    try {
      const res = await api.executeRecipe(id, params);
      await refresh();
      toast(`Recipe started: session ${res.session_id.slice(0, 8)}…`, "success");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const cancel = async (id: string) => {
    try {
      await api.cancelRecipe(id);
      await refresh();
      toast("Recipe run cancelled", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div>
      <h1 className="page-title">Recipes</h1>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}
      {loading ? (
        <SkeletonCard />
      ) : recipes.length === 0 && !error ? (
        <EmptyState icon="📖" title="No recipes saved yet" hint="Recipes created via the API appear here." />
      ) : (
        recipes.map((r) => (
          <div className="card" key={r.recipe_id}>
            <h3>
              {r.name} <span className="muted">v{r.version}</span>
            </h3>
            <p className="muted">
              {r.steps} steps · {r.executions} runs
              {r.tags.length > 0 && ` · ${r.tags.join(", ")}`}
            </p>
            {r.description && <p>{r.description}</p>}
            <div className="approval-actions">
              <button className="btn secondary" onClick={() => void toggle(r.recipe_id)}>
                {expanded === r.recipe_id ? "Hide parameters" : "Parameters"}
              </button>
              <button
                className="btn"
                disabled={busy === r.recipe_id}
                onClick={() => void run(r.recipe_id)}
              >
                {busy === r.recipe_id ? "Running…" : "Run recipe"}
              </button>
              <button className="btn danger btn-sm" onClick={() => void cancel(r.recipe_id)}>
                Cancel run
              </button>
            </div>
            {expanded === r.recipe_id && (
              <div className="stack-8">
                <label htmlFor={`params-${r.recipe_id}`} className="muted">
                  Parameters (JSON object; schema defaults from the recipe)
                </label>
                <textarea
                  id={`params-${r.recipe_id}`}
                  className="json-editor"
                  value={paramDrafts[r.recipe_id] ?? "{}"}
                  onChange={(e) =>
                    setParamDrafts((prev) => ({ ...prev, [r.recipe_id]: e.target.value }))
                  }
                />
                {paramError && (
                  <p className="field-error" role="alert">
                    {paramError}
                  </p>
                )}
                <p className="disabled-note">
                  Per-run history has no backend endpoint yet — the executions count above comes
                  from the recipe record itself.
                </p>
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}
