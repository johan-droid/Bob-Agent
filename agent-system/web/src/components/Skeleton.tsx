"use client";

interface SkeletonProps {
  lines?: number;
  className?: string;
}

/** Shimmer placeholder rows for first-paint loading states. Pure CSS. */
export function Skeleton({ lines = 3, className = "" }: SkeletonProps) {
  return (
    <div className={`skeleton-block ${className}`} aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton" />
      ))}
    </div>
  );
}

export function SkeletonCard() {
  return (
    <div className="card" aria-hidden="true">
      <div className="skeleton skeleton-title" />
      <div className="skeleton" />
      <div className="skeleton skeleton-short" />
    </div>
  );
}
