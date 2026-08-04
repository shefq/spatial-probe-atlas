import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, PropsWithChildren, ReactNode } from "react";
import { useEffect, useId, useRef } from "react";

export function Button({
  variant = "default",
  size = "md",
  busy = false,
  children,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "primary" | "danger" | "ghost";
  size?: "sm" | "md";
  busy?: boolean;
}) {
  return (
    <button className={`button button--${variant} button--${size}`} disabled={disabled || busy} {...props}>
      {busy ? <span className="spinner" aria-hidden="true" /> : null}
      {children}
    </button>
  );
}

export function Card({ title, eyebrow, actions, children, className = "", ...props }: PropsWithChildren<HTMLAttributes<HTMLElement>> & {
  title?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className={`card ${className}`} {...props}>
      {title || eyebrow || actions ? (
        <header className="card__header">
          <div>
            {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
            {title ? <h2 className="card__title">{title}</h2> : null}
          </div>
          {actions ? <div className="card__actions">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}

export function StatusBadge({ state, label }: { state: string | boolean | null | undefined; label?: string }) {
  const normalized = state === true ? "ready" : state === false || !state ? "inactive" : String(state).toLowerCase();
  const tone = ["ready", "active", "running", "completed", "pass", "tracked", "good", "open", "cuda_ready"].includes(normalized)
    ? "good"
    : ["warning", "degraded", "recoverable", "reconnecting", "accepted_with_warning", "warn"].includes(normalized)
      ? "warning"
      : ["failed", "error", "critical", "lost", "fail", "quarantined"].includes(normalized)
        ? "danger"
        : "neutral";
  return <span className={`status status--${tone}`}><span className="status__dot" />{label ?? normalized.replaceAll("_", " ")}</span>;
}

export function Metric({ label, value, detail, tone }: { label: string; value: ReactNode; detail?: ReactNode; tone?: "good" | "warning" | "danger" }) {
  return (
    <div className={`metric ${tone ? `metric--${tone}` : ""}`}>
      <span className="metric__label">{label}</span>
      <strong className="metric__value">{value ?? "—"}</strong>
      {detail ? <span className="metric__detail">{detail}</span> : null}
    </div>
  );
}

export function Field({ label, hint, error, children, className = "" }: PropsWithChildren<{ label: ReactNode; hint?: ReactNode; error?: ReactNode; className?: string }>) {
  return (
    <label className={`field ${className}`}>
      <span className="field__label">{label}</span>
      {children}
      {error ? <span className="field__error">{error}</span> : hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input className="input" {...props} />;
}

export function EmptyState({ icon = "◎", title, children, actions }: PropsWithChildren<{ icon?: string; title: string; actions?: ReactNode }>) {
  return (
    <div className="empty-state">
      <div className="empty-state__icon" aria-hidden="true">{icon}</div>
      <h2>{title}</h2>
      <div className="muted">{children}</div>
      {actions ? <div className="button-row">{actions}</div> : null}
    </div>
  );
}

export function InlineAlert({ tone = "info", title, children, action }: PropsWithChildren<{ tone?: "info" | "warning" | "danger" | "success"; title: string; action?: ReactNode }>) {
  return (
    <div className={`alert alert--${tone}`} role={tone === "danger" ? "alert" : "status"}>
      <div className="alert__mark" aria-hidden="true">{tone === "danger" ? "!" : tone === "warning" ? "△" : tone === "success" ? "✓" : "i"}</div>
      <div><strong>{title}</strong><div>{children}</div></div>
      {action ? <div className="alert__action">{action}</div> : null}
    </div>
  );
}

export function ProgressBar({ value, label }: { value?: number; label?: string }) {
  const percent = Math.max(0, Math.min(100, Math.round((value ?? 0) * 100)));
  return (
    <div className="progress" aria-label={label ?? "Progress"} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} role="progressbar">
      <span style={{ width: `${percent}%` }} />
    </div>
  );
}

export function Modal({ open, title, description, children, footer, onRequestClose, size = "md", testId }: PropsWithChildren<{
  open: boolean;
  title: string;
  description?: string;
  footer?: ReactNode;
  onRequestClose: () => void;
  size?: "sm" | "md" | "lg" | "xl";
  testId?: string;
}>) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const onRequestCloseRef = useRef(onRequestClose);
  useEffect(() => {
    onRequestCloseRef.current = onRequestClose;
  }, [onRequestClose]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onRequestCloseRef.current();
      if (event.key !== "Tab" || !panelRef.current) return;
      const items = panelRef.current.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex='0']");
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    document.body.classList.add("modal-open");
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("modal-open");
      previous?.focus();
    };
  }, [open]);
  if (!open) return null;
  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) onRequestClose(); }} data-testid={testId}>
      <div className={`modal modal--${size}`} role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panelRef} tabIndex={-1}>
        <header className="modal__header">
          <div><h2 id={titleId}>{title}</h2>{description ? <p>{description}</p> : null}</div>
          <Button variant="ghost" aria-label="Close" onClick={onRequestClose}>×</Button>
        </header>
        <div className="modal__body">{children}</div>
        {footer ? <footer className="modal__footer">{footer}</footer> : null}
      </div>
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button key={option.value} className={option.value === value ? "is-active" : ""} type="button" onClick={() => onChange(option.value)} aria-pressed={option.value === value}>
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return <div className="skeleton" aria-label="Loading">{Array.from({ length: lines }).map((_, index) => <span key={index} />)}</div>;
}

export function Toggle({ label, ...props }: InputHTMLAttributes<HTMLInputElement> & { label: string }) {
  return (
    <label className="toggle"><input type="checkbox" {...props} /><span className="toggle__control" /><span>{label}</span></label>
  );
}
