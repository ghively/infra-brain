import { useRef } from "react";
import "./ui.css";

export interface TabItem {
  key: string;
  label: string;
  disabled?: boolean;
}

export interface TabsProps {
  tabs: TabItem[];
  active: string;
  onChange: (key: string) => void;
  className?: string;
  /** Accessible label for the tablist, e.g. "vSphere sections". */
  label?: string;
}

/** Keyboard-navigable tab strip: Left/Right (and Home/End) move focus+selection
 *  between tabs, per the WAI-ARIA "automatic activation" tabs pattern. Used
 *  heavily starting Phase 4 to consolidate multi-page groups (vSphere/Iac/
 *  Cloud, the Octopus x3 pages) into single tabbed pages. */
export function Tabs({ tabs, active, onChange, className, label }: TabsProps) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const enabledTabs = tabs.filter((t) => !t.disabled);

  const focusAndSelect = (key: string) => {
    onChange(key);
    refs.current[key]?.focus();
  };

  const handleKeyDown = (e: React.KeyboardEvent, currentKey: string) => {
    const idx = enabledTabs.findIndex((t) => t.key === currentKey);
    if (idx === -1) return;
    let nextIdx: number | null = null;
    switch (e.key) {
      case "ArrowRight":
      case "ArrowDown":
        nextIdx = (idx + 1) % enabledTabs.length;
        break;
      case "ArrowLeft":
      case "ArrowUp":
        nextIdx = (idx - 1 + enabledTabs.length) % enabledTabs.length;
        break;
      case "Home":
        nextIdx = 0;
        break;
      case "End":
        nextIdx = enabledTabs.length - 1;
        break;
      default:
        return;
    }
    e.preventDefault();
    focusAndSelect(enabledTabs[nextIdx].key);
  };

  return (
    <div className={["ui-tabs", className].filter(Boolean).join(" ")} role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button
          key={t.key}
          ref={(el) => {
            refs.current[t.key] = el;
          }}
          type="button"
          role="tab"
          id={`tab-${t.key}`}
          aria-selected={active === t.key}
          aria-controls={`tabpanel-${t.key}`}
          tabIndex={active === t.key ? 0 : -1}
          disabled={t.disabled}
          className="ui-tab"
          onClick={() => !t.disabled && onChange(t.key)}
          onKeyDown={(e) => handleKeyDown(e, t.key)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
