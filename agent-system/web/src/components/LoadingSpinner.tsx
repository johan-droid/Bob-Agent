"use client";

export function LoadingSpinner({ label = "Loading…" }: { label?: string }) {
  return (
    <span className="spinner-wrap" role="status" aria-label={label}>
      <span className="spinner" aria-hidden="true" />
      <span className="muted">{label}</span>
    </span>
  );
}
