/** The landing page for a data source this deployment does not have.
 *
 *  ## Why this exists rather than a 404 or a deleted route
 *
 *  Removing the vSphere / Cloud / Software / Rapid7-CVE pages removed their
 *  *content*, not their URLs. Those URLs are in bookmarks, in old chat
 *  messages, and in this repo's own docs. Letting them fall through to a blank
 *  screen or a router miss would turn "we tidied the nav" into "the dashboard
 *  is broken", which is exactly the impression the tidy-up was meant to remove.
 *
 *  So every retired route still resolves — to this page, which says plainly
 *  what the source was, that it is not configured here, and why it is not
 *  coming back. The copy comes from `RETIRED_SOURCES` in `nav.ts`, the same
 *  table that serves as the deleted pages' epitaph, so the operator-facing
 *  explanation and the developer-facing one cannot drift apart.
 *
 *  This page fetches nothing. A retired source has no endpoint worth calling
 *  and a spinner followed by an empty table would be a worse answer than a
 *  sentence.
 */
import { useLocation } from "react-router-dom";
import { Panel } from "../components/ui/Panel";
import { PageShell } from "../components/ui/PageShell";
import { Icon } from "../lib/icons";
import { RETIRED_SOURCES } from "../nav";

export function SourceUnavailable() {
  const { pathname } = useLocation();
  const entry = RETIRED_SOURCES[pathname];

  const title = entry?.title ?? "Source not configured";
  const reason =
    entry?.reason ??
    "This data source is not configured in this environment, so there is nothing to show here.";
  const domains = entry?.domains ?? [];

  return (
    <div>
      <PageShell
        icon={<Icon name="settings" />}
        title={title}
        description="This source isn't configured here."
      />

      <Panel rail="info" mnemonic="RETIRED" description="source not present in this environment">
        <div style={{ padding: "18px 16px", maxWidth: 760, lineHeight: 1.65 }}>
          <p style={{ margin: 0, color: "var(--text)" }}>{reason}</p>

          {domains.length > 0 && (
            <p style={{ marginTop: 14, marginBottom: 0, color: "var(--faint)", fontSize: 13 }}>
              Collector {domains.length === 1 ? "domain" : "domains"}:{" "}
              <code style={{ color: "var(--text)" }}>{domains.join(", ")}</code> — retired, holding
              no data.
            </p>
          )}

          <p style={{ marginTop: 14, marginBottom: 0, color: "var(--faint)", fontSize: 13 }}>
            Nothing was deleted from the database and the collector still exists. If this source
            ever becomes real here, revive it with{" "}
            <code style={{ color: "var(--text)" }}>COLLECTION_REVIVED_DOMAINS</code> and it will
            reappear in the sidebar on its own — the navigation is derived from{" "}
            <code style={{ color: "var(--text)" }}>GET /api/dashboard/sources</code>, not from a
            hardcoded list.
          </p>

          <p style={{ marginTop: 18, marginBottom: 0 }}>
            <a href="/dashboard2/agents" style={{ color: "var(--blue)" }}>
              See every collector and its status →
            </a>
          </p>
        </div>
      </Panel>
    </div>
  );
}
