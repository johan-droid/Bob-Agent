"use client";

interface ErrorBannerProps {
  message: string;
  onRetry?: () => void;
  onDismiss?: () => void;
}

export function ErrorBanner({ message, onRetry, onDismiss }: ErrorBannerProps) {
  return (
    <div className="error-banner" role="alert">
      <span className="error-banner-icon" aria-hidden="true">
        ⚠
      </span>
      <span className="error-banner-text">Backend unreachable: {message}</span>
      {onRetry ? (
        <button type="button" className="btn secondary error-banner-btn" onClick={onRetry}>
          Retry
        </button>
      ) : null}
      {onDismiss ? (
        <button
          type="button"
          className="error-banner-dismiss"
          aria-label="Dismiss error"
          onClick={onDismiss}
        >
          ×
        </button>
      ) : null}
    </div>
  );
}
