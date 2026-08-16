import type { ButtonHTMLAttributes, ReactNode } from "react";
import "./ui.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows an inline spinner in place of the leading icon slot and forces
   *  `aria-busy`/disabled — for in-flight async actions (submit, refresh). */
  loading?: boolean;
  /** Optional leading icon/glyph, hidden while `loading`. */
  icon?: ReactNode;
  children?: ReactNode;
}

/** Shared button primitive — the only interactive-accent color is `--blue`
 *  (variant="primary"); secondary/ghost use neutral surfaces, danger uses
 *  `--red`. No component in this codebase had a shared Button before this
 *  (every page hand-rolled inline-styled buttons — see Phase 1 plan). */
export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  disabled,
  icon,
  children,
  className,
  type = "button",
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || loading;
  return (
    <button
      type={type}
      className={["ui-btn", `ui-btn-${variant}`, `ui-btn-${size}`, className]
        .filter(Boolean)
        .join(" ")}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? (
        <span className="ui-btn-spinner" role="status" aria-label="Loading" />
      ) : (
        icon
      )}
      {children}
    </button>
  );
}
