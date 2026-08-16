import { BrowserRouter, Routes, Route, Outlet } from "react-router-dom";
import { PageBoundary } from "./PageBoundary";
import { RefreshProvider } from "./hooks/useAutoRefresh";
import { GlobalErrorBanner, EndpointErrorBanner } from "./components/ErrorBanners";
import { AuthGate } from "./AuthGate";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { SavedViews } from "./pages/SavedViews";
import { Activity } from "./pages/Activity";
import { AgentConfig } from "./pages/AgentConfig";
import { Agents } from "./pages/Agents";
import { CollRuns } from "./pages/CollRuns";
import { Compl } from "./pages/Compl";
import { CustomViews } from "./pages/CustomViews";
import { Decisions } from "./pages/Decisions";
// ─── BEGIN T7 (rev14) additive import — reconcile with sibling routing work ───
import { LlmObservability } from "./pages/LlmObservability";
// ─── END T7 (rev14) additive import ───
import { Drift } from "./pages/Drift";
import { Eol } from "./pages/Eol";
import { HostPurposeMap } from "./pages/HostPurposeMap";
import { Fleet, Hosts, Resources } from "./pages/Hosts";
import { Iac } from "./pages/Iac";
import { Instincts } from "./pages/Instincts";
import { Intprops } from "./pages/Intprops";
import { Invrec } from "./pages/Invrec";
import { Notifications } from "./pages/Notifications";
import { Observations } from "./pages/Observations";
import { Remed } from "./pages/Remed";
import { ScanSchedule } from "./pages/ScanSchedule";
import { Scripts } from "./pages/Scripts";
import { Security } from "./pages/Security";
import { NetDiscovery } from "./pages/NetDiscovery";
import { Settings } from "./pages/Settings";
import { Software } from "./pages/Software";
/** Replaced pages/Vsphere.tsx, pages/Cloud.tsx and pages/R7detail.tsx — all
 *  three read only retired collectors' tables, all of which are empty. Their
 *  routes still resolve (bookmarks must not 404); see RETIRED_SOURCES in
 *  nav.ts for the per-path epitaph this page renders.
 *  NOT Software: /software was restored by owner decision (2026-08-13) — the
 *  data is wanted, only the collector is missing, which is a different thing
 *  from a source this deployment will never run. */
import { SourceUnavailable } from "./pages/SourceUnavailable";
import { Vulns } from "./pages/Vulns";
import { Home } from "./pages/Home";
import { Graph } from "./pages/Graph";
import { Documents } from "./pages/Documents";
import { Homelab } from "./pages/Homelab";
import { Sweeps } from "./pages/Sweeps";
import { Login } from "./pages/Login";
import { McpKeys } from "./pages/McpKeys";
import { ViewShare } from "./pages/ViewShare";
/* T2-chat-page (rev14): ADDITIVE — see the matching route block below. */
import { Chat } from "./pages/Chat";
import { Help } from "./pages/Help";
/** Router shell for the Wave 6.1 Vite+React dashboard rebuild (DR-6.1).
 *  Every routed page mounts here wrapped in its own PageBoundary. B1 shipped the
 *  shell only; B2+ add one <Route> per ported page (Saved Views, then Home,
 *  Graph). The ChatDrawer was previously mounted here outside <Routes>; the
 *  design-foundation MR (feat/dashboard2-design-foundation) replaced it with an
 *  always-visible embedded panel inside Sidebar (matching the legacy DC shell's
 *  sidebar chat — shell.dc.html lines 247-284). ChatDrawer.tsx is retained for
 *  reference but no longer mounted here.
 *
 *  MR-I: added the <Sidebar> nav shell (`.ib-shell`/`.ib-content` grid, see
 *  index.css) — before this MR the app had zero navigation chrome, every page
 *  was reachable only by typing its URL directly. Also added /vsphere and
 *  /octopus routes (pages/Vsphere.tsx, pages/Octopus.tsx) — see nav.ts for why.
 *
 *  TRK-236: the shell chrome (Sidebar/Topbar) + every routed page now mount
 *  under `AuthenticatedLayout`, a layout route wrapped in `<AuthGate>` (see
 *  AuthGate.tsx) instead of unconditionally as before — that gate is what
 *  stops an unauthenticated visit from firing ~18 doomed 401 fetches before
 *  the login redirect settles. `/login` is a sibling route OUTSIDE that
 *  layout, so it renders with no Sidebar/Topbar and no auth check at all.
 *
 *  F-2: `/views/:token` (ViewShare.tsx) is ALSO a sibling route outside
 *  AuthenticatedLayout, for the same reason as `/login`. The backend
 *  deliberately makes `GET /api/dashboard/views/{token}` public for
 *  `is_public` views (ui.py) and builds `share_url=/dashboard2/views/{token}`
 *  for the copy-link button — but mounting this route under AuthGate/Sidebar/
 *  Topbar meant an anonymous viewer's session-gated chrome fetches
 *  (StatusPill, SweepHealthStrip, ...) 401'd and bounced them to /login before
 *  the public view ever rendered, making the sharing feature unusable for its
 *  entire intended (anonymous) audience. Moving it out means it never mounts
 *  Sidebar/Topbar or fires any session-gated fetch, same as /login — the page
 *  fetches only its own (possibly-public) view data. */
function AuthenticatedLayout() {
  return (
    <AuthGate>
      {(me) => (
        <RefreshProvider>
          <GlobalErrorBanner />
          <EndpointErrorBanner />
          <div className="ib-shell">
            <Sidebar initialMe={me} />
            <div className="ib-content" style={{ padding: 0 }}>
              <Topbar />
              <div style={{ padding: "26px 32px" }}>
                <Outlet />
              </div>
            </div>
          </div>
        </RefreshProvider>
      )}
    </AuthGate>
  );
}

export default function App() {
  return (
    <BrowserRouter basename="/dashboard2">
      <Routes>
        <Route
          path="/login"
          element={
            <PageBoundary page="login">
              <Login />
            </PageBoundary>
          }
        />
        {/* F-2: sibling of /login, outside AuthenticatedLayout/AuthGate — see
         *  the file-header comment above for why. */}
        <Route
          path="/views/:token"
          element={
            <PageBoundary page="viewshare">
              <ViewShare />
            </PageBoundary>
          }
        />
        <Route element={<AuthenticatedLayout />}>
            <Route
              index
              element={
                <PageBoundary page="home">
                  <Home />
                </PageBoundary>
              }
            />
            <Route
              path="/savedviews"
              element={
                <PageBoundary page="savedviews">
                  <SavedViews />
                </PageBoundary>
              }
            />
            <Route
              path="/activity"
              element={
                <PageBoundary page="activity">
                  <Activity />
                </PageBoundary>
              }
            />
            <Route
              path="/agentconfig"
              element={
                <PageBoundary page="agentconfig">
                  <AgentConfig />
                </PageBoundary>
              }
            />
            <Route
              path="/agents"
              element={
                <PageBoundary page="agents">
                  <Agents />
                </PageBoundary>
              }
            />
            <Route
              path="/collruns"
              element={
                <PageBoundary page="collruns">
                  <CollRuns />
                </PageBoundary>
              }
            />
            <Route
              path="/compl"
              element={
                <PageBoundary page="compl">
                  <Compl />
                </PageBoundary>
              }
            />
            <Route
              path="/resources"
              element={
                <PageBoundary page="resources">
                  <Resources />
                </PageBoundary>
              }
            />
            <Route
              path="/scanschedule"
              element={
                <PageBoundary page="scanschedule">
                  <ScanSchedule />
                </PageBoundary>
              }
            />
            <Route
              path="/scripts"
              element={
                <PageBoundary page="scripts">
                  <Scripts />
                </PageBoundary>
              }
            />
            <Route
              path="/software"
              element={
                <PageBoundary page="software">
                  <Software />
                </PageBoundary>
              }
            />
            <Route
              path="/security"
              element={
                <PageBoundary page="security">
                  <Security />
                </PageBoundary>
              }
            />
            <Route
              path="/mcpkeys"
              element={
                <PageBoundary page="mcpkeys">
                  <McpKeys />
                </PageBoundary>
              }
            />
            <Route
              path="/netdiscovery"
              element={
                <PageBoundary page="netdiscovery">
                  <NetDiscovery />
                </PageBoundary>
              }
            />
            <Route
              path="/notifications"
              element={
                <PageBoundary page="notifications">
                  <Notifications />
                </PageBoundary>
              }
            />
            <Route
              path="/observations"
              element={
                <PageBoundary page="observations">
                  <Observations />
                </PageBoundary>
              }
            />
            <Route
              path="/r7detail"
              element={
                <PageBoundary page="r7detail">
                  <SourceUnavailable />
                </PageBoundary>
              }
            />
            <Route
              path="/customviews"
              element={
                <PageBoundary page="customviews">
                  <CustomViews />
                </PageBoundary>
              }
            />
            <Route
              path="/decisions"
              element={
                <PageBoundary page="decisions">
                  <Decisions />
                </PageBoundary>
              }
            />
            {/* ─── BEGIN T7 (rev14) additive route — reconcile with sibling routing work ─── */}
            <Route
              path="/llm"
              element={
                <PageBoundary page="llm">
                  <LlmObservability />
                </PageBoundary>
              }
            />
            {/* ─── END T7 (rev14) additive route ─── */}
            <Route
              path="/drift"
              element={
                <PageBoundary page="drift">
                  <Drift />
                </PageBoundary>
              }
            />
            <Route
              path="/eol"
              element={
                <PageBoundary page="eol">
                  <Eol />
                </PageBoundary>
              }
            />
            <Route
              path="/fleet"
              element={
                <PageBoundary page="fleet">
                  <Fleet />
                </PageBoundary>
              }
            />
            <Route
              path="/hosts"
              element={
                <PageBoundary page="hosts">
                  <Hosts />
                </PageBoundary>
              }
            />
            <Route
              path="/host-purpose-map"
              element={
                <PageBoundary page="host-purpose-map">
                  <HostPurposeMap />
                </PageBoundary>
              }
            />
            <Route
              path="/iac"
              element={
                <PageBoundary page="iac">
                  <Iac />
                </PageBoundary>
              }
            />
            <Route
              path="/instincts"
              element={
                <PageBoundary page="instincts">
                  <Instincts />
                </PageBoundary>
              }
            />
            <Route
              path="/intprops"
              element={
                <PageBoundary page="intprops">
                  <Intprops />
                </PageBoundary>
              }
            />
            <Route
              path="/invrec"
              element={
                <PageBoundary page="invrec">
                  <Invrec />
                </PageBoundary>
              }
            />
            <Route
              path="/remed"
              element={
                <PageBoundary page="remed">
                  <Remed />
                </PageBoundary>
              }
            />
            <Route
              path="/settings"
              element={
                <PageBoundary page="settings">
                  <Settings />
                </PageBoundary>
              }
            />
            <Route
              path="/vulns"
              element={
                <PageBoundary page="vulns">
                  <Vulns />
                </PageBoundary>
              }
            />
            <Route
              path="/graph"
              element={
                <PageBoundary page="graph">
                  <Graph />
                </PageBoundary>
              }
            />
            <Route
              path="/documents"
              element={
                <PageBoundary page="documents">
                  <Documents />
                </PageBoundary>
              }
            />
            <Route
              path="/vsphere"
              element={
                <PageBoundary page="vsphere">
                  <SourceUnavailable />
                </PageBoundary>
              }
            />
            <Route
              path="/cloud"
              element={
                <PageBoundary page="cloud">
                  <SourceUnavailable />
                </PageBoundary>
              }
            />
            <Route
              path="/homelab"
              element={
                <PageBoundary page="homelab">
                  <Homelab />
                </PageBoundary>
              }
            />
            <Route
              path="/sweeps"
              element={
                <PageBoundary page="sweeps">
                  <Sweeps />
                </PageBoundary>
              }
            />
            {/* ── T2-chat-page (rev14): ADDITIVE, expects integrator
              * reconciliation. Session-gated like every other page here — the
              * backend route it drives (POST /api/dashboard/chat) sits on
              * ui_router, which carries a router-level require_session. ── */}
            <Route
              path="/chat"
              element={
                <PageBoundary page="chat">
                  <Chat />
                </PageBoundary>
              }
            />
            <Route
              path="/help"
              element={
                <PageBoundary page="help">
                  <Help />
                </PageBoundary>
              }
            />
            {/* ── end T2-chat-page ── */}
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
