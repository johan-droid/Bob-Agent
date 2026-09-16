"use client";

import { useEffect, useRef, useState, useCallback, useMemo, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import {
  api,
  EventPoller,
  type Session,
  type Task,
  type ModelRoutingProviders,
} from "@/lib/api";
import { ChatSidebar } from "@/components/ChatSidebar";
import {
  MessageBubble,
  type ToolCallItem,
  type InChatApproval,
} from "@/components/MessageBubble";
import { useToast } from "@/components/Toast";
import { Skeleton } from "@/components/Skeleton";

interface ChatMessage {
  id: string;
  role: "user" | "agent" | "system" | "approval";
  content: string;
  streaming?: boolean;
  thought?: string;
  toolCalls?: ToolCallItem[];
  approval?: InChatApproval;
  timestamp?: string;
  taskId?: string;
}

const STARTER_PROMPTS = [
  {
    title: "🔍 Analyze Workspace Structure",
    prompt: "List all files in the current workspace and provide a summary of the project architecture.",
  },
  {
    title: "📚 Query Obsidian Vault",
    prompt: "Recall relevant notes from the Obsidian memory vault regarding recent agent tasks and decisions.",
  },
  {
    title: "⚡ Run Code Refactor",
    prompt: "Inspect the codebase for optimization opportunities and propose clean refactoring steps.",
  },
  {
    title: "🛡️ Check System Status & Tools",
    prompt: "Perform a system health check and report all configured LLM providers, tool permissions, and budget status.",
  },
];

function ChatPageContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const toast = useToast();

  const [sessions, setSessions] = useState<Session[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [streamChars, setStreamChars] = useState(0);
  const [disconnected, setDisconnected] = useState(false);
  const [failures, setFailures] = useState(0);
  const [routing, setRouting] = useState<ModelRoutingProviders | null>(null);
  const [attachedFiles, setAttachedFiles] = useState<{ name: string; content: string }[]>([]);
  const [showSlashMenu, setShowSlashMenu] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const pollerRef = useRef<EventPoller | null>(null);
  const streamBufferRef = useRef("");
  const thoughtBufferRef = useRef("");
  const activeToolsRef = useRef<Record<string, ToolCallItem>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Load session list and routing config on mount
  useEffect(() => {
    void api.listSessions().then(setSessions).catch(() => setSessions([]));
    void api.listRoutingProviders().then(setRouting).catch(() => null);
  }, []);

  // Handle URL session query param
  useEffect(() => {
    const sessionParam = searchParams.get("session");
    if (sessionParam && sessionParam !== selectedId) {
      setSelectedId(sessionParam);
    }
  }, [searchParams, selectedId]);

  // Scroll to bottom when messages update
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isStreaming]);

  // Load history and initialize event listener when session changes
  useEffect(() => {
    if (!selectedId) {
      setMessages([]);
      return;
    }

    // Load initial tasks for this session
    void api.listTasks(selectedId).then(async (tasks) => {
      const msgs: ChatMessage[] = [];
      const approvals = await api.listApprovals(false).catch(() => []);

      for (const t of tasks) {
        msgs.push({
          id: `task-${t.id}-user`,
          role: "user",
          content: t.title,
          taskId: t.id,
        });

        const relatedApproval = approvals.find((a) => a.task_id === t.id);
        if (relatedApproval) {
          msgs.push({
            id: `approval-${relatedApproval.approval_id}`,
            role: "approval",
            content: "",
            approval: {
              approval_id: relatedApproval.approval_id,
              requested_action: relatedApproval.requested_action,
              risk: relatedApproval.risk,
              scope: relatedApproval.scope,
              requester: relatedApproval.requester,
              reason: relatedApproval.reason,
              decided: relatedApproval.decision !== "PENDING",
              decision: relatedApproval.decision as "ALLOW_ONCE" | "ALLOW_ALWAYS" | "DENIED",
            },
          });
        }

        if (t.state === "SUCCEEDED" || t.state === "FAILED" || t.state === "CANCELLED") {
          const output = (t as { output?: string | null }).output;
          msgs.push({
            id: `task-${t.id}-result`,
            role: "agent",
            content:
              t.state === "FAILED"
                ? `⚠️ **Task failed:** ${t.last_error || "unknown error"}`
                : output || `✓ **Task finished:** ${t.state}`,
            taskId: t.id,
          });
        }
      }
      // Merge, don't wipe: handleSend adds the optimistic user message (and
      // streaming bubble) before the session id is set — a blind replace
      // here would delete the just-sent first message of a new chat.
      setMessages((prev) => (prev.length > 0 ? prev : msgs));
    });

    // Start polling events for live interaction
    pollerRef.current?.stop();
    streamBufferRef.current = "";
    thoughtBufferRef.current = "";
    activeToolsRef.current = {};

    const poller = new EventPoller(
      (events) => {
        setDisconnected(false);
        setFailures(0);

        setMessages((prev) => {
          let next = [...prev];

          for (const e of events) {
            const payload = (e.payload ?? {}) as Record<string, unknown>;

            // Filter for current session if tagged
            if (e.session_id && e.session_id !== selectedId) {
              continue;
            }

            if (e.type === "model.token") {
              const delta = String(payload.delta ?? "");
              streamBufferRef.current += delta;
              setStreamChars(streamBufferRef.current.length);

              const lastIdx = next.length - 1;
              if (lastIdx >= 0 && next[lastIdx].streaming) {
                next[lastIdx] = {
                  ...next[lastIdx],
                  content: streamBufferRef.current,
                  thought: thoughtBufferRef.current || undefined,
                };
              } else {
                next.push({
                  id: `stream-${Date.now()}`,
                  role: "agent",
                  content: streamBufferRef.current,
                  thought: thoughtBufferRef.current || undefined,
                  streaming: true,
                });
              }
            } else if (e.type === "model.thought") {
              const delta = String(payload.delta ?? payload.content ?? "");
              thoughtBufferRef.current += delta;
              const lastIdx = next.length - 1;
              if (lastIdx >= 0 && next[lastIdx].streaming) {
                next[lastIdx] = {
                  ...next[lastIdx],
                  thought: thoughtBufferRef.current,
                };
              }
            } else if (e.type === "model.completed") {
              streamBufferRef.current = "";
              thoughtBufferRef.current = "";
              setStreamChars(0);
              setIsStreaming(false);
              setActiveTaskId(null);

              const lastIdx = next.length - 1;
              if (lastIdx >= 0 && next[lastIdx].streaming) {
                next[lastIdx] = { ...next[lastIdx], streaming: false };
              }
            } else if (e.type === "tool.started") {
              // Backend emits {tool, capability_risk, protocol} (agent_loop);
              // older/alternate shapes carry tool_name/name + call_id/input.
              const toolName = String(
                payload.tool ?? payload.tool_name ?? payload.name ?? "tool",
              );
              const toolId = String(
                payload.call_id ??
                  payload.approval_id ??
                  `tool-${toolName}-${Date.now()}-${Math.random().toString(36).slice(2)}`,
              );
              const toolArgs = (payload.input ?? payload.args ?? {}) as Record<string, unknown>;

              const item: ToolCallItem = {
                id: toolId,
                name: toolName,
                args: toolArgs,
                status: "running",
              };
              activeToolsRef.current[toolId] = item;

              const lastIdx = next.length - 1;
              if (lastIdx >= 0 && next[lastIdx].role === "agent") {
                const tools = [...(next[lastIdx].toolCalls || []), item];
                next[lastIdx] = { ...next[lastIdx], toolCalls: tools };
              }
            } else if (e.type === "tool.completed" || e.type === "tool.failed") {
              // Backend emits {tool, ok} — match by call_id when present,
              // else by tool name, else mark the newest running tool.
              const toolId = String(payload.call_id ?? "");
              const toolName = String(payload.tool ?? payload.tool_name ?? "");
              const result = String(
                payload.output ?? payload.result ?? payload.error ?? payload.reason ?? "",
              );
              const duration = Number(payload.duration_ms ?? 0);
              const status: "completed" | "failed" =
                e.type === "tool.completed" ? "completed" : "failed";

              const lastIdx = next.length - 1;
              if (lastIdx >= 0 && next[lastIdx].toolCalls) {
                const tools = next[lastIdx].toolCalls!;
                let matched = false;
                const updatedTools: ToolCallItem[] = tools.map((t) => {
                  const hit =
                    (toolId && t.id === toolId) ||
                    (toolName && t.name === toolName);
                  if (hit) matched = true;
                  return hit ? { ...t, result, durationMs: duration, status } : t;
                });
                if (!matched) {
                  // Fall back to the newest still-running tool call.
                  for (let i = updatedTools.length - 1; i >= 0; i--) {
                    if (updatedTools[i].status === "running") {
                      updatedTools[i] = {
                        ...updatedTools[i],
                        result,
                        durationMs: duration,
                        status,
                      };
                      break;
                    }
                  }
                }
                next[lastIdx] = { ...next[lastIdx], toolCalls: updatedTools };
              }
            } else if (e.type === "approval.requested") {
              next.push({
                id: `approval-${payload.approval_id ?? Date.now()}`,
                role: "approval",
                content: "",
                approval: {
                  approval_id: String(payload.approval_id ?? ""),
                  requested_action: String(payload.requested_action ?? ""),
                  risk: String(payload.risk ?? "MEDIUM"),
                  scope: String(payload.scope ?? "tool"),
                  reason: payload.reason ? String(payload.reason) : null,
                  decided: false,
                },
              });
            } else if (
              e.type === "approval.approved" ||
              e.type === "approval.denied" ||
              e.type === "approval.expired"
            ) {
              // Canonical backend names (see domain/events.py) — there is no
              // "approval.decided" event. approval_id may live top-level or
              // in the payload.
              const approvalId = String(
                payload.approval_id ?? (e as { approval_id?: unknown }).approval_id ?? "",
              );
              const decision =
                e.type === "approval.approved"
                  ? "ALLOW_ONCE"
                  : e.type === "approval.denied"
                    ? "DENIED"
                    : "DENIED";
              next = next.map((m) =>
                m.approval?.approval_id === approvalId
                  ? {
                      ...m,
                      approval: {
                        ...m.approval,
                        decided: true,
                        decision:
                          String(payload.decision ?? decision) as
                            | "ALLOW_ONCE"
                            | "ALLOW_ALWAYS"
                            | "DENIED",
                      },
                    }
                  : m,
              );
            } else if (
              e.type === "task.completed" ||
              e.type === "task.failed" ||
              e.type === "task.cancelled"
            ) {
              // Terminal task events close any open streaming bubble. Without
              // this, a task that fails before its first token leaves the
              // optimistic bubble hanging (spinner forever).
              const lastIdx = next.length - 1;
              if (e.type === "task.failed") {
                const errText = String(payload.error ?? "Task failed");
                if (lastIdx >= 0 && next[lastIdx].streaming) {
                  next[lastIdx] = {
                    ...next[lastIdx],
                    streaming: false,
                    content: next[lastIdx].content || `⚠️ **Task failed:** ${errText}`,
                  };
                } else {
                  next.push({
                    id: `taskfail-${Date.now()}`,
                    role: "agent",
                    content: `⚠️ **Task failed:** ${errText}`,
                  });
                }
              } else if (lastIdx >= 0 && next[lastIdx].streaming) {
                const output = String(payload.output ?? "");
                next[lastIdx] = {
                  ...next[lastIdx],
                  streaming: false,
                  content: next[lastIdx].content || output,
                };
              }
              setIsStreaming(false);
              setActiveTaskId(null);
            }
          }
          return next;
        });
      },
      () => {
        setFailures((n) => n + 1);
        setDisconnected(true);
        setIsStreaming(false);
      },
      1500,
      selectedId,
    );

    pollerRef.current = poller;
    poller.start();

    return () => {
      poller.stop();
      streamBufferRef.current = "";
      thoughtBufferRef.current = "";
      activeToolsRef.current = {};
    };
  }, [selectedId]);

  const handleNewChat = useCallback(() => {
    setSelectedId(null);
    setMessages([]);
    setIsStreaming(false);
    setStreamChars(0);
    streamBufferRef.current = "";
    thoughtBufferRef.current = "";
    router.push("/chat");
  }, [router]);

  const handleSelectSession = useCallback(
    (id: string) => {
      setSelectedId(id);
      setMessages([]);
      setIsStreaming(false);
      setStreamChars(0);
      streamBufferRef.current = "";
      thoughtBufferRef.current = "";
      router.push(`/chat?session=${id}`);
    },
    [router],
  );

  const handleRenameSession = useCallback(
    async (id: string, newGoal: string) => {
      try {
        await api.updateSession(id, { goal: newGoal });
        setSessions((prev) =>
          prev.map((s) => (s.id === id ? { ...s, goal: newGoal } : s)),
        );
        toast("Conversation renamed", "success");
      } catch (err) {
        toast(`Failed to rename: ${(err as Error).message}`, "error");
      }
    },
    [toast],
  );

  const handleDeleteSession = useCallback(
    async (id: string) => {
      try {
        await api.deleteSession(id);
        setSessions((prev) => prev.filter((s) => s.id !== id));
        if (selectedId === id) {
          handleNewChat();
        }
        toast("Conversation deleted", "success");
      } catch (err) {
        toast(`Failed to delete: ${(err as Error).message}`, "error");
      }
    },
    [selectedId, handleNewChat, toast],
  );

  const handleExportSession = useCallback(
    (id: string, format: "markdown" | "json") => {
      const session = sessions.find((s) => s.id === id);
      const title = session?.goal || `conversation-${id.slice(0, 8)}`;
      let data = "";
      let filename = `${title.replace(/[^a-z0-9]/gi, "_").toLowerCase()}.${format === "json" ? "json" : "md"}`;

      if (format === "json") {
        data = JSON.stringify(
          {
            session_id: id,
            goal: session?.goal,
            messages,
          },
          null,
          2,
        );
      } else {
        data = `# ${session?.goal || "Conversation"}\n\n`;
        for (const m of messages) {
          if (m.role === "user") {
            data += `### 👤 User\n${m.content}\n\n`;
          } else if (m.role === "agent") {
            data += `### 🤖 Agent\n${m.content}\n\n`;
          }
        }
      }

      const blob = new Blob([data], { type: format === "json" ? "application/json" : "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      toast(`Exported ${filename}`, "success");
    },
    [sessions, messages, toast],
  );

  const handleDecideApproval = useCallback(
    async (approvalId: string, approve: boolean, policy: string) => {
      try {
        await api.decideApproval(approvalId, approve, policy);
        setMessages((prev) =>
          prev.map((m) =>
            m.approval?.approval_id === approvalId
              ? {
                  ...m,
                  approval: {
                    ...m.approval,
                    decided: true,
                    decision: policy as "ALLOW_ONCE" | "ALLOW_ALWAYS" | "DENIED",
                  },
                }
              : m,
          ),
        );
        toast(`Approval decision recorded: ${policy}`, "success");
      } catch (err) {
        toast(`Failed to submit decision: ${(err as Error).message}`, "error");
      }
    },
    [toast],
  );

  const handleStopGeneration = useCallback(async () => {
    if (activeTaskId) {
      try {
        await api.cancelTask(activeTaskId, "User stopped generation");
        toast("Agent task stopped", "info");
      } catch (err) {
        toast(`Failed to stop task: ${(err as Error).message}`, "error");
      }
    }
    setIsStreaming(false);
  }, [activeTaskId, toast]);

  const handleSend = useCallback(
    async (textToSend?: string) => {
      let promptText = (textToSend ?? input).trim();
      if (!promptText && attachedFiles.length === 0) return;

      // Append attached files context if any
      if (attachedFiles.length > 0) {
        const fileContentStr = attachedFiles
          .map((f) => `\n\n--- Attachment: ${f.name} ---\n${f.content}\n--- End Attachment ---`)
          .join("");
        promptText = `${promptText}${fileContentStr}`.trim();
        setAttachedFiles([]);
      }

      const userMsg: ChatMessage = {
        id: `user-${Date.now()}`,
        role: "user",
        content: promptText,
      };

      setMessages((prev) => [...prev, userMsg]);
      setInput("");
      setShowSlashMenu(false);

      let sessionId = selectedId;
      if (!sessionId) {
        try {
          const created = await api.createSession(promptText.slice(0, 100));
          setSessions((prev) => [created, ...prev]);
          sessionId = created.id;
          setSelectedId(sessionId);
          router.push(`/chat?session=${sessionId}`);
        } catch (err) {
          toast((err as Error).message, "error");
          setMessages((prev) => [
            ...prev,
            {
              id: `err-${Date.now()}`,
              role: "agent",
              content: "❌ Failed to create session. Is the Bob Agent backend running?",
            },
          ]);
          return;
        }
      }

      // Create the task (stays PENDING by contract) then explicitly run
      // it — the backend executes in-process, no Redis/RQ worker needed.
      try {
        const createdTask = await api.createTask(sessionId, promptText);
        setActiveTaskId(createdTask.id);
        try {
          await api.runTask(createdTask.id);
        } catch (runErr) {
          toast(`Task created but run failed: ${(runErr as Error).message}`, "error");
        }
      } catch (err) {
        toast(`Failed to start task: ${(err as Error).message}`, "error");
      }

      streamBufferRef.current = "";
      thoughtBufferRef.current = "";
      setStreamChars(0);
      setMessages((prev) => [
        ...prev,
        { id: `agent-${Date.now()}`, role: "agent", content: "", streaming: true },
      ]);
      setIsStreaming(true);
    },
    [input, attachedFiles, selectedId, router, toast],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void handleSend();
      }
    },
    [handleSend],
  );

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const reader = new FileReader();
      reader.onload = (event) => {
        const content = event.target?.result as string;
        setAttachedFiles((prev) => [...prev, { name: file.name, content }]);
        toast(`Attached ${file.name}`, "info");
      };
      reader.readAsText(file);
    }
  };

  const removeAttachment = (index: number) => {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const currentSession = useMemo(
    () => sessions.find((s) => s.id === selectedId),
    [sessions, selectedId],
  );

  const streamingActive = messages.some((m) => m.streaming);

  return (
    <div className="chat-layout">
      <ChatSidebar
        sessions={sessions}
        selectedId={selectedId}
        onSelect={handleSelectSession}
        onNewChat={handleNewChat}
        onRefresh={() => {
          void api.listSessions().then(setSessions);
        }}
        onRenameSession={handleRenameSession}
        onDeleteSession={handleDeleteSession}
        onExportSession={handleExportSession}
      />

      <div className="chat-main">
        {/* Chat Header Bar */}
        <div className="chat-top-header">
          <div className="chat-top-title">
            <h2>{currentSession ? currentSession.goal : "New Conversation"}</h2>
            {selectedId && (
              <span className="session-id-badge">ID: {selectedId.slice(0, 8)}</span>
            )}
          </div>
          <div className="chat-top-controls">
            {routing && (
              <div className="provider-indicator-pill" title="Default Model Router">
                <span className="dot online" />
                <span>{routing.default_provider}/{routing.default_model || "auto"}</span>
              </div>
            )}
            {selectedId && (
              <button
                type="button"
                className="btn btn-sm btn-secondary"
                onClick={() => handleExportSession(selectedId, "markdown")}
                title="Export conversation as Markdown"
              >
                ⬇ Export
              </button>
            )}
          </div>
        </div>

        {/* Disconnection Warning Banner */}
        {disconnected && (
          <div className="reconnect-banner" role="alert">
            🔴 Realtime connection lost — retrying automatically (sequence resumable, no events lost
            {failures > 1 ? ` · attempt ${failures}` : ""}).
          </div>
        )}

        {/* Messages Container */}
        {messages.length === 0 ? (
          <div className="chat-empty-state">
            <div className="chat-empty-hero">
              <div className="hero-badge">🤖 BOB AGENT v3.1</div>
              <h2>How can Bob help you today?</h2>
              <p>
                Bob is your autonomous assistant equipped with shell execution, file management,
                Obsidian vault memory, and Model Context Protocol (MCP) integrations.
              </p>
            </div>

            <div className="starter-prompts-grid">
              {STARTER_PROMPTS.map((starter, idx) => (
                <button
                  key={idx}
                  type="button"
                  className="starter-card"
                  onClick={() => void handleSend(starter.prompt)}
                >
                  <div className="starter-title">{starter.title}</div>
                  <div className="starter-desc">{starter.prompt}</div>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="chat-messages" aria-live="polite" aria-label="Conversation messages">
            {messages.map((msg) => (
              <MessageBubble
                key={msg.id}
                role={msg.role}
                content={msg.content}
                streaming={msg.streaming}
                thought={msg.thought}
                toolCalls={msg.toolCalls}
                approval={msg.approval}
                onDecideApproval={handleDecideApproval}
                onRetry={
                  msg.role === "user" ? () => void handleSend(msg.content) : undefined
                }
                timestamp={msg.timestamp}
              />
            ))}

            {streamingActive && streamChars === 0 && (
              <div className="typing-indicator-row">
                <span className="typing-avatar">🤖</span>
                <div className="typing-bubble">
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>
        )}

        {/* Floating Token & Live Stream Meter */}
        {streamingActive && streamChars > 0 && (
          <div className="token-meter-floating">
            <span>⚡ Streaming tokens: ≈ {Math.max(1, Math.round(streamChars / 4)).toLocaleString()}</span>
          </div>
        )}

        {/* Attachments Preview Row */}
        {attachedFiles.length > 0 && (
          <div className="attachments-preview-bar">
            {attachedFiles.map((file, idx) => (
              <div key={idx} className="attachment-chip">
                <span>📎 {file.name}</span>
                <button type="button" onClick={() => removeAttachment(idx)}>✕</button>
              </div>
            ))}
          </div>
        )}

        {/* Chat Input Dock */}
        <div className="chat-input-area">
          <div className="chat-input-dock">
            <input
              type="file"
              ref={fileInputRef}
              style={{ display: "none" }}
              onChange={handleFileUpload}
              multiple
            />

            <button
              type="button"
              className="chat-attach-btn"
              onClick={() => fileInputRef.current?.click()}
              title="Attach text file or context"
              aria-label="Attach file"
            >
              📎
            </button>

            <textarea
              id="chat-input"
              className="chat-input"
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                if (e.target.value.startsWith("/")) {
                  setShowSlashMenu(true);
                } else {
                  setShowSlashMenu(false);
                }
              }}
              onKeyDown={handleKeyDown}
              placeholder="Ask Bob anything or type / for commands… (Enter to send, Shift+Enter for newline)"
              rows={1}
            />

            {isStreaming ? (
              <button
                type="button"
                className="chat-stop-btn"
                onClick={() => void handleStopGeneration()}
                title="Stop generation"
                aria-label="Stop generation"
              >
                ■ Stop
              </button>
            ) : (
              <button
                type="button"
                className="chat-send-btn"
                onClick={() => void handleSend()}
                disabled={!input.trim() && attachedFiles.length === 0}
                aria-label="Send prompt"
              >
                ↑
              </button>
            )}
          </div>

          <div className="chat-input-hints">
            <span><strong>Enter</strong> to send</span>
            <span><strong>Shift+Enter</strong> for newline</span>
            <span><strong>Cmd+K</strong> command palette</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ChatPage() {
  return (
    <Suspense
      fallback={
        <div className="chat-layout">
          <div className="chat-main" style={{ padding: 24 }}>
            <Skeleton lines={6} />
          </div>
        </div>
      }
    >
      <ChatPageContent />
    </Suspense>
  );
}
