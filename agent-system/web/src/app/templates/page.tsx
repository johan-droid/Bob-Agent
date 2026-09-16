"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { api, type Template, type WorkspaceOut } from "@/lib/api";
import { Skeleton } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

export default function TemplatesPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showRestoreModal, setShowRestoreModal] = useState<Template | null>(null);
  const [createWorkspaceId, setCreateWorkspaceId] = useState("");
  const [createTemplateName, setCreateTemplateName] = useState("");
  const [restoreWorkspaceName, setRestoreWorkspaceName] = useState("");
  const [processing, setProcessing] = useState(false);
  const toast = useToast();

  const loadData = useCallback(async () => {
    try {
      const [tpls, ws] = await Promise.all([
        api.listTemplates(),
        api.listWorkspaces().catch(() => [] as WorkspaceOut[]),
      ]);
      setTemplates(tpls);
      setWorkspaces(ws);
      if (ws.length > 0 && !createWorkspaceId) {
        setCreateWorkspaceId(ws[0].id);
      }
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [createWorkspaceId]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const handleCreateTemplate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!createWorkspaceId || !createTemplateName.trim()) {
      toast("Please select a workspace and provide a template name", "error");
      return;
    }
    setProcessing(true);
    try {
      const res = await api.createTemplate(createWorkspaceId, createTemplateName.trim());
      toast(
        `Template "${res.name}" created (${(res.size_bytes / 1024).toFixed(1)} KB)`,
        "success",
      );
      if (res.skipped_secrets && res.skipped_secrets.length > 0) {
        toast(`Protected ${res.skipped_secrets.length} secret files from snapshot`, "info");
      }
      setShowCreateModal(false);
      setCreateTemplateName("");
      await loadData();
    } catch (err) {
      toast(`Failed to create template: ${(err as Error).message}`, "error");
    } finally {
      setProcessing(false);
    }
  };

  const handleRestoreTemplate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!showRestoreModal) return;
    setProcessing(true);
    try {
      const ws = await api.restoreTemplate(
        showRestoreModal.template_id,
        restoreWorkspaceName.trim() || undefined,
      );
      toast(`Workspace "${ws.name}" restored successfully!`, "success");
      setShowRestoreModal(null);
      setRestoreWorkspaceName("");
      await loadData();
    } catch (err) {
      toast(`Failed to restore template: ${(err as Error).message}`, "error");
    } finally {
      setProcessing(false);
    }
  };

  const handleDeleteTemplate = async (templateId: string, name: string) => {
    if (!confirm(`Delete template "${name}"?`)) return;
    try {
      await api.deleteTemplate(templateId);
      toast("Template deleted", "success");
      await loadData();
    } catch (err) {
      toast(`Failed to delete template: ${(err as Error).message}`, "error");
    }
  };

  return (
    <div className="templates-container">
      <div className="vault-header">
        <div className="vault-header-title">
          <h1 className="page-title" style={{ margin: 0 }}>
            Workspace Templates
          </h1>
          <p className="muted" style={{ margin: "4px 0 0" }}>
            Snapshot, archive, and clone isolated workspace environments with mandatory secret exclusion.
          </p>
        </div>
        <div className="vault-header-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setShowCreateModal(true)}
            disabled={workspaces.length === 0}
            title={workspaces.length === 0 ? "No active workspaces to snapshot" : ""}
          >
            + Create Template
          </button>
        </div>
      </div>

      {error && <ErrorBanner message={error} onRetry={() => void loadData()} />}

      {/* Secret Exclusion Safety Callout */}
      <div className="vault-safety-banner">
        <span className="safety-icon">🛡️</span>
        <div>
          <strong>Pre-Snapshot Secret Protection Active:</strong>
          <span>
            {" "}
            All environment files (<code>.env*</code>), credentials, private keys, and authorization tokens are automatically scrubbed during snapshot.
          </span>
        </div>
      </div>

      {/* Templates Grid */}
      {loading ? (
        <div className="stats-grid" style={{ marginTop: 20 }}>
          <Skeleton lines={4} />
          <Skeleton lines={4} />
        </div>
      ) : templates.length === 0 ? (
        <EmptyState
          icon="📦"
          title="No workspace templates yet"
          hint={
            workspaces.length > 0
              ? 'Click "+ Create Template" to snapshot any of your workspaces into a reusable template.'
              : "Create a workspace first, then snapshot it as a template."
          }
        />
      ) : (
        <div className="templates-grid">
          {templates.map((tpl) => (
            <div key={tpl.template_id} className="template-card card">
              <div className="template-card-header">
                <div className="template-icon">📦</div>
                <div className="template-info">
                  <h3 className="template-title">{tpl.name}</h3>
                  <span className="template-id">ID: {tpl.template_id}</span>
                </div>
              </div>

              <div className="template-meta-row">
                <div className="meta-item">
                  <span className="meta-label">Size</span>
                  <span className="meta-val">{(tpl.size_bytes / 1024).toFixed(1)} KB</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Created</span>
                  <span className="meta-val">{new Date(tpl.created_at).toLocaleDateString()}</span>
                </div>
                {tpl.skipped_secrets && tpl.skipped_secrets.length > 0 && (
                  <div className="meta-item">
                    <span className="meta-label">Protected</span>
                    <span className="meta-val">{tpl.skipped_secrets.length} secrets</span>
                  </div>
                )}
              </div>

              <div className="template-actions">
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={() => {
                    setRestoreWorkspaceName(`${tpl.name}-clone`);
                    setShowRestoreModal(tpl);
                  }}
                >
                  🚀 Restore as Workspace
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-danger"
                  onClick={() => void handleDeleteTemplate(tpl.template_id, tpl.name)}
                >
                  🗑️ Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Modal: Create Template */}
      {showCreateModal && (
        <div className="modal-backdrop">
          <div className="modal-dialog">
            <div className="modal-header">
              <h3>Snapshot Workspace as Template</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowCreateModal(false)}
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleCreateTemplate}>
              <div className="modal-body">
                <div className="form-group">
                  <label htmlFor="tpl-source-ws">Select Workspace to Snapshot *</label>
                  <select
                    id="tpl-source-ws"
                    className="form-select"
                    value={createWorkspaceId}
                    onChange={(e) => setCreateWorkspaceId(e.target.value)}
                    required
                  >
                    {workspaces.map((ws) => (
                      <option key={ws.id} value={ws.id}>
                        {ws.name} ({ws.file_count} files · {(ws.size_bytes / 1024).toFixed(1)} KB)
                      </option>
                    ))}
                  </select>
                </div>

                <div className="form-group">
                  <label htmlFor="tpl-name">Template Name *</label>
                  <input
                    id="tpl-name"
                    type="text"
                    className="form-input"
                    placeholder="e.g. Next.js SaaS Starter"
                    value={createTemplateName}
                    onChange={(e) => setCreateTemplateName(e.target.value)}
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
                  disabled={processing || !createTemplateName.trim()}
                >
                  {processing ? "Creating Snapshot…" : "Create Template"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Modal: Restore Template */}
      {showRestoreModal && (
        <div className="modal-backdrop">
          <div className="modal-dialog">
            <div className="modal-header">
              <h3>Restore Template to Workspace</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowRestoreModal(null)}
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleRestoreTemplate}>
              <div className="modal-body">
                <p className="muted" style={{ margin: "0 0 16px" }}>
                  This will clone template <strong>{showRestoreModal.name}</strong> into a new isolated workspace.
                </p>

                <div className="form-group">
                  <label htmlFor="restore-ws-name">New Workspace Name *</label>
                  <input
                    id="restore-ws-name"
                    type="text"
                    className="form-input"
                    placeholder="e.g. My Project"
                    value={restoreWorkspaceName}
                    onChange={(e) => setRestoreWorkspaceName(e.target.value)}
                    required
                  />
                </div>
              </div>

              <div className="modal-footer">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => setShowRestoreModal(null)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={processing || !restoreWorkspaceName.trim()}
                >
                  {processing ? "Restoring Workspace…" : "Restore Workspace"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
