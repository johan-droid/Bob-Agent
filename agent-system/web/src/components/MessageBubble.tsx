"use client";

import { useState } from "react";

export interface ToolCallItem {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: string;
  status: "pending" | "running" | "completed" | "failed";
  durationMs?: number;
}

export interface InChatApproval {
  approval_id: string;
  requested_action: string;
  risk: string;
  scope: string;
  requester?: string;
  reason?: string | null;
  decided?: boolean;
  decision?: "ALLOW_ONCE" | "ALLOW_ALWAYS" | "DENIED";
}

export interface MessageBubbleProps {
  id?: string;
  role: "user" | "agent" | "system" | "tool" | "approval";
  content: string;
  streaming?: boolean;
  thought?: string;
  toolCalls?: ToolCallItem[];
  approval?: InChatApproval;
  onDecideApproval?: (id: string, approve: boolean, policy: string) => Promise<void>;
  onRetry?: () => void;
  timestamp?: string;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderFormattedText(text: string): string {
  let html = escapeHtml(text);

  // Fenced Code blocks (```language\ncode```)
  html = html.replace(
    /```([a-zA-Z0-9_\-#+.]*)\n?([\s\S]*?)```/g,
    (_match: string, lang: string, code: string) => {
      const language = lang.trim() || "text";
      const cleanCode = code.replace(/\n$/, "");
      return `<div class="code-block-wrapper">
        <div class="code-block-header">
          <span class="code-lang-tag">${escapeHtml(language)}</span>
          <button class="code-copy-btn" onclick="navigator.clipboard.writeText(this.closest('.code-block-wrapper').querySelector('code').innerText); this.innerText='Copied!'; setTimeout(() => this.innerText='Copy', 2000)">Copy</button>
        </div>
        <pre><code class="language-${escapeHtml(language)}">${cleanCode}</code></pre>
      </div>`;
    },
  );

  // Inline code `code`
  html = html.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');

  // Blockquotes
  html = html.replace(/^>\s?(.*)$/gm, '<blockquote class="chat-blockquote">$1</blockquote>');

  // Headers
  html = html.replace(/^###\s+(.+)$/gm, '<h3 class="chat-h3">$1</h3>');
  html = html.replace(/^##\s+(.+)$/gm, '<h2 class="chat-h2">$1</h2>');
  html = html.replace(/^#\s+(.+)$/gm, '<h1 class="chat-h1">$1</h1>');

  // Bold & Italic
  html = html.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");

  // Task list checkboxes
  html = html.replace(/^- \[ \]\s+(.+)$/gm, '<li class="task-item"><input type="checkbox" disabled /> <span>$1</span></li>');
  html = html.replace(/^- \[x\]\s+(.+)$/gm, '<li class="task-item"><input type="checkbox" checked disabled /> <span class="task-done">$1</span></li>');

  // Unordered lists
  html = html.replace(/^- (.+)$/gm, "<li>$1</li>");
  html = html.replace(/(<li>(?:(?!<\/li>).)*<\/li>\n?)+/g, (match) => `<ul class="chat-list">${match}</ul>`);

  // Ordered lists
  html = html.replace(/^\d+\.\s+(.+)$/gm, '<li class="chat-ol-item">$1</li>');

  // Tables
  html = html.replace(
    /((?:\|[^\n]+\|\n?)+)/g,
    (tableMatch) => {
      const rows = tableMatch.trim().split("\n");
      if (rows.length < 2) return tableMatch;
      let tableHtml = '<div class="table-container"><table class="chat-table">';
      rows.forEach((row, i) => {
        if (row.includes("---")) return; // divider
        const cells = row.split("|").filter((_, idx, arr) => idx > 0 && idx < arr.length - 1);
        const tag = i === 0 ? "th" : "td";
        tableHtml += "<tr>";
        cells.forEach((c) => {
          tableHtml += `<${tag}>${c.trim()}</${tag}>`;
        });
        tableHtml += "</tr>";
      });
      tableHtml += "</table></div>";
      return tableHtml;
    }
  );

  // Paragraphs
  html = html.replace(/\n\n+/g, "</p><p>");
  html = `<p>${html}</p>`;

  // Clean empty paragraphs and invalid nesting
  html = html.replace(/<p>\s*<\/p>/g, "");
  html = html.replace(/<p>(<(?:div|h[1-3]|ul|blockquote|table)[\s\S]*?>)/g, "$1");
  html = html.replace(/(<\/(?:div|h[1-3]|ul|blockquote|table)>)<\/p>/g, "$1");

  // Single newlines
  html = html.replace(/\n/g, "<br/>");

  return html;
}

export function MessageBubble({
  role,
  content,
  streaming,
  thought,
  toolCalls,
  approval,
  onDecideApproval,
  onRetry,
  timestamp,
}: MessageBubbleProps) {
  const [thoughtOpen, setThoughtOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [approvalActioning, setApprovalActioning] = useState<string | null>(null);

  const isUser = role === "user";
  const avatar = isUser ? "👤" : role === "approval" ? "🛡️" : "🤖";

  const handleCopyMessage = () => {
    void navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleApprovalAction = async (approve: boolean, policy: string) => {
    if (!approval || !onDecideApproval) return;
    setApprovalActioning(policy);
    try {
      await onDecideApproval(approval.approval_id, approve, policy);
    } finally {
      setApprovalActioning(null);
    }
  };

  return (
    <div className={`message-row ${role}${streaming ? " is-streaming" : ""}`}>
      <div className="message-avatar" aria-hidden="true">
        {avatar}
      </div>
      <div className="message-content-container">
        {/* Thought / Reasoning Accordion */}
        {thought && thought.trim().length > 0 && (
          <div className="thought-container">
            <button
              type="button"
              className="thought-toggle-btn"
              onClick={() => setThoughtOpen(!thoughtOpen)}
              aria-expanded={thoughtOpen}
            >
              <span className="thought-icon">💭</span>
              <span className="thought-title">
                {thoughtOpen ? "Hide Thinking Process" : "View Thinking Process"}
              </span>
              <span className="thought-arrow">{thoughtOpen ? "▲" : "▼"}</span>
            </button>
            {thoughtOpen && (
              <div className="thought-content">
                <p>{thought}</p>
              </div>
            )}
          </div>
        )}

        {/* Tool Execution Cards */}
        {toolCalls && toolCalls.length > 0 && (
          <div className="tool-calls-container">
            {toolCalls.map((call) => (
              <div key={call.id} className={`tool-card tool-status-${call.status}`}>
                <div className="tool-card-header">
                  <div className="tool-card-title">
                    <span className="tool-badge">🔧 TOOL</span>
                    <strong className="tool-name">{call.name}</strong>
                    {call.durationMs !== undefined && (
                      <span className="tool-duration">{call.durationMs}ms</span>
                    )}
                  </div>
                  <span className={`tool-status-pill status-${call.status}`}>
                    {call.status === "running" && "⏳ Running"}
                    {call.status === "completed" && "✓ Completed"}
                    {call.status === "failed" && "✕ Failed"}
                    {call.status === "pending" && "⏸ Queued"}
                  </span>
                </div>
                {call.args && Object.keys(call.args).length > 0 && (
                  <details className="tool-args-details">
                    <summary className="tool-args-summary">Parameters</summary>
                    <pre className="tool-json-preview">
                      {JSON.stringify(call.args, null, 2)}
                    </pre>
                  </details>
                )}
                {call.result && (
                  <div className="tool-result-box">
                    <span className="tool-result-label">Result:</span>
                    <pre className="tool-result-text">{call.result}</pre>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* In-Chat Approval Card */}
        {approval && (
          <div className={`approval-card risk-${approval.risk.toLowerCase()}`}>
            <div className="approval-card-header">
              <span className="approval-icon">⚠️</span>
              <div>
                <h4>Permission Requested</h4>
                <p className="approval-desc">
                  The agent requires authorization for: <code>{approval.requested_action}</code>
                </p>
              </div>
              <span className={`risk-badge risk-${approval.risk.toLowerCase()}`}>
                {approval.risk} RISK
              </span>
            </div>
            {approval.reason && (
              <p className="approval-reason"><strong>Reason:</strong> {approval.reason}</p>
            )}
            <div className="approval-details">
              <span><strong>Scope:</strong> {approval.scope}</span>
              {approval.requester && <span><strong>Requester:</strong> {approval.requester}</span>}
            </div>
            {!approval.decided ? (
              <div className="approval-actions">
                <button
                  type="button"
                  className="btn btn-sm btn-primary-action"
                  disabled={approvalActioning !== null}
                  onClick={() => handleApprovalAction(true, "ALLOW_ONCE")}
                >
                  {approvalActioning === "ALLOW_ONCE" ? "Processing…" : "✓ Approve Once"}
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-secondary"
                  disabled={approvalActioning !== null}
                  onClick={() => handleApprovalAction(true, "ALLOW_ALWAYS")}
                >
                  {approvalActioning === "ALLOW_ALWAYS" ? "Processing…" : "★ Always Allow"}
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-danger"
                  disabled={approvalActioning !== null}
                  onClick={() => handleApprovalAction(false, "DENIED")}
                >
                  {approvalActioning === "DENIED" ? "Processing…" : "✕ Reject"}
                </button>
              </div>
            ) : (
              <div className="approval-decided-banner">
                <span>Decision recorded: <strong>{approval.decision || "Decided"}</strong></span>
              </div>
            )}
          </div>
        )}

        {/* Main Text Content */}
        {content && content.trim().length > 0 && (
          <div
            className={`message-bubble${streaming ? " streaming-cursor" : ""}`}
            dangerouslySetInnerHTML={{ __html: renderFormattedText(content) }}
          />
        )}

        {/* Action Toolbar below bubble */}
        <div className="message-toolbar">
          {timestamp && <span className="message-timestamp">{timestamp}</span>}
          <button
            type="button"
            className="message-action-btn"
            onClick={handleCopyMessage}
            aria-label="Copy message"
            title="Copy message"
          >
            {copied ? "✓ Copied" : "📋 Copy"}
          </button>
          {onRetry && (
            <button
              type="button"
              className="message-action-btn"
              onClick={onRetry}
              aria-label="Retry message"
              title="Retry message"
            >
              🔄 Retry
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
