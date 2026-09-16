"use client";

import type { ReactNode } from "react";

interface EmptyStateProps {
  icon?: string;
  title: string;
  hint?: string;
  action?: ReactNode;
}

export function EmptyState({ icon = "○", title, hint, action }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <div className="empty-state-icon" aria-hidden="true">
        {icon}
      </div>
      <p className="empty-state-title">{title}</p>
      {hint ? <p className="muted">{hint}</p> : null}
      {action ? <div className="empty-state-action">{action}</div> : null}
    </div>
  );
}
