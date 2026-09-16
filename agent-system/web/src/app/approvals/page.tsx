"use client";

import { useEffect, useState } from "react";
import { api, type Approval } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

const HIGH_RISK_SCOPES = new Set(["browser:transact", "os:input"]);

function riskClass(a: Approval): "risk-high" | "risk-medium" | "risk-low" {
  if (HIGH_RISK_SCOPES.has(a.scope) || a.risk.toLowerCase() === "high") return "risk-high";
  if (a.scope === "file:write" || a.risk.toLowerCase() === "medium") return "risk-medium";
  return "risk-low";
}

export default function ApprovalsPage() {
  const [rows, setRows] = useState<Approval[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [pendingOnly, setPendingOnly] = useState(true);
  const toast = useToast();

  const refresh = async () => {
    try {
      setRows(await api.listApprovals(pendingOnly));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingOnly]);

  const decide = async (id: string, approve: boolean, policy = "ALLOW_ONCE") => {
    try {
      await api.decideApproval(id, approve, policy);
      await refresh();
      toast(approve ? `Approved (${policy})` : "Denied", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const sweep = async () => {
    try {
      const res = await api.sweepApprovals();
      await refresh();
      toast(`Swept ${res.expired.length} expired approvals`, "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div>
      <h1 className="page-title">Approvals</h1>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}

      <div className="toolbar">
        <label htmlFor="approvals-filter" className="muted">
          Show
        </label>
        <select
          id="approvals-filter"
          value={pendingOnly ? "pending" : "all"}
          onChange={(e) => setPendingOnly(e.target.value === "pending")}
        >
          <option value="pending">Pending only</option>
          <option value="all">All (incl. decided)</option>
        </select>
        <button className="btn secondary" onClick={() => void sweep()}>
          Sweep expired
        </button>
      </div>

      {loading ? (
        <SkeletonCard />
      ) : rows.length === 0 && !error ? (
        <EmptyState icon="✅" title="No approvals" hint={pendingOnly ? "No pending approvals." : "No approvals recorded."} />
      ) : (
        rows.map((a) => (
          <div className="card" key={a.approval_id}>
            <h3>{a.requested_action}</h3>
            <p className="muted">
              risk <span className={riskClass(a)}>{a.risk}</span> · scope{" "}
              <span className={riskClass(a)}>{a.scope}</span> · requested by {a.requester} ·
              decision {a.decision}
              {a.task_id ? ` · task ${a.task_id.slice(0, 8)}` : ""}
            </p>
            {a.reason && (
              <p>
                <span className="muted">Reason: </span>
                {a.reason}
              </p>
            )}
            {a.context && Object.keys(a.context).length > 0 && (
              <details>
                <summary className="muted">Context</summary>
                <pre className="preview-pre">{JSON.stringify(a.context, null, 2)}</pre>
              </details>
            )}
            {a.decision === "PENDING" ? (
              <div className="approval-actions">
                <button className="btn success" onClick={() => void decide(a.approval_id, true)}>
                  Approve
                </button>
                <button
                  className="btn secondary"
                  onClick={() => void decide(a.approval_id, true, "ALLOW_ALWAYS")}
                  title="Approve and always allow this type (policy ALLOW_ALWAYS)"
                >
                  Always Allow
                </button>
                <button className="btn danger" onClick={() => void decide(a.approval_id, false)}>
                  Deny
                </button>
              </div>
            ) : null}
          </div>
        ))
      )}
    </div>
  );
}
