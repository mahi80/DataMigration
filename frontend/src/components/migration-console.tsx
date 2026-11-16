"use client";

import { Activity, ArrowRight, ArrowUpRight, BookOpen, CheckCheck, ChevronRight, CircleHelp, Database, GitBranch, Layers3, LayoutDashboard, LoaderCircle, LockKeyhole, LogOut, Menu, Plus, RefreshCw, ShieldCheck, Sparkles, Table2, Workflow, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ConnectionsView } from "./connections-view";
import { MigrationWizard } from "./migration-wizard";
import { RunDetailView } from "./run-detail";
import { RunTable } from "./run-table";
import type { Connection, ConnectorCapability, Health, Principal, RunList, RunSummary } from "./types";
import { api, dateLabel, errorMessage, formatCount, kindName } from "./types";
import { Brand, EmptyState, Loading, Metric, Notice } from "./ui";
import { AskQ } from "./ask-q";
import type { RunFocus } from "./ask-q";
import { recordInteraction } from "./assistant-events";

type View = "overview" | "migrations" | "connections" | "new" | "run";
const docsUrl = "https://github.com/mahi80/DataMigration#readme";

function readLocation(): { view: View; run: string | null } {
  const params = new URLSearchParams(window.location.search);
  const run = params.get("run");
  const view = params.get("view");
  return run ? { view: "run", run } : { view: ["migrations", "connections", "new"].includes(view || "") ? view as View : "overview", run: null };
}

export function MigrationConsole() {
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [sessionError, setSessionError] = useState("");
  const [view, setView] = useState<View>("overview");
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [runFocus, setRunFocus] = useState<{ section: RunFocus; nonce: number }>();
  const focusSequence = useRef(0);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [capabilities, setCapabilities] = useState<Record<string, ConnectorCapability>>({});
  const [health, setHealth] = useState<Health | null>(null);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [lastSynced, setLastSynced] = useState<string>();
  const [mobileMenu, setMobileMenu] = useState(false);
  const identityEpoch = useRef(0);
  const requestSequence = useRef(0);
  const inFlight = useRef(false);
  const canPlan = principal?.actions.includes("plan") || false;
  const canDiscover = principal?.actions.includes("discover") || false;

  const clearIdentity = useCallback(() => {
    identityEpoch.current += 1;
    requestSequence.current += 1;
    inFlight.current = false;
    setPrincipal(null); setRuns([]); setConnections([]); setCapabilities({}); setHealth(null);
    setTotal(0); setHasMore(false); setLoading(true); setError(""); setLastSynced(undefined); setRefreshing(false); setLoadingMore(false);
  }, []);

  useEffect(() => {
    let live = true;
    void fetch("/api/session", { cache: "no-store" }).then(async (response) => {
      if (!response.ok) return null;
      return response.json() as Promise<{ principal: Principal }>;
    }).then((session) => { if (live && session?.principal) setPrincipal(session.principal); })
      .catch(() => { if (live) setSessionError("We could not connect to the console. Please try again."); })
      .finally(() => { if (live) { const location = readLocation(); setView(location.view); setSelectedRun(location.run); setCheckingSession(false); } });
    function restoreLocation() { const current = readLocation(); setView(current.view); setSelectedRun(current.run); setRunFocus(undefined); setMobileMenu(false); recordInteraction("navigation", "success"); }
    function expired() { clearIdentity(); setSessionError("Your session has expired or access changed. Sign in again to continue."); }
    window.addEventListener("popstate", restoreLocation);
    window.addEventListener("migration-session-expired", expired);
    return () => { live = false; window.removeEventListener("popstate", restoreLocation); window.removeEventListener("migration-session-expired", expired); };
  }, [clearIdentity]);

  const refreshWorkspace = useCallback(async () => {
    if (!principal || inFlight.current) return;
    inFlight.current = true;
    const epoch = identityEpoch.current;
    const sequence = ++requestSequence.current;
    setRefreshing(true);
    const results = await Promise.allSettled([
      api<RunList>("runs?limit=100&offset=0"), api<Health>("health"),
      api<{ connectors: Record<string, ConnectorCapability> }>("connectors"),
      principal.actions.includes("discover") ? api<{ connections: Connection[] }>("connections") : Promise.resolve({ connections: [] }),
    ]);
    if (epoch !== identityEpoch.current || sequence !== requestSequence.current) return;
    const failures: string[] = [];
    if (results[0].status === "fulfilled") { setRuns(results[0].value.runs); setTotal(results[0].value.total); setHasMore(results[0].value.has_more); }
    else failures.push(errorMessage(results[0].reason));
    if (results[1].status === "fulfilled") setHealth(results[1].value); else { setHealth(null); failures.push("Service health is unavailable."); }
    if (results[2].status === "fulfilled") setCapabilities(results[2].value.connectors); else failures.push(errorMessage(results[2].reason));
    if (results[3].status === "fulfilled") setConnections(results[3].value.connections); else failures.push(errorMessage(results[3].reason));
    setError([...new Set(failures)].join(" ")); setLoading(false); setRefreshing(false); inFlight.current = false;
    if (!failures.length) setLastSynced(new Date().toISOString());
  }, [principal]);

  useEffect(() => {
    if (!principal) return;
    const timer = window.setTimeout(() => void refreshWorkspace(), 0);
    return () => window.clearTimeout(timer);
  }, [principal, refreshWorkspace]);
  const hasActive = runs.some((run) => run.status === "RUNNING");
  useEffect(() => {
    if (!principal || !hasActive) return;
    const timer = window.setInterval(() => void refreshWorkspace(), 7000);
    return () => window.clearInterval(timer);
  }, [principal, hasActive, refreshWorkspace]);

  function navigate(next: View, run?: string, focus?: RunFocus) {
    setView(next); setSelectedRun(run || null); setRunFocus(focus ? { section: focus, nonce: ++focusSequence.current } : undefined); setMobileMenu(false);
    recordInteraction("navigation", "success");
    const query = run ? `?run=${encodeURIComponent(run)}` : next === "overview" ? "" : `?view=${next}`;
    window.history.pushState({}, "", `${window.location.pathname}${query}`);
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  async function signOut() {
    try {
      const response = await fetch("/api/session", { method: "DELETE" });
      if (!response.ok) throw new Error("Sign out could not be confirmed. Please retry.");
      clearIdentity(); setSessionError(""); navigate("overview");
    } catch (failure) { setError(errorMessage(failure)); }
  }

  async function loadMore() {
    const epoch = identityEpoch.current; setLoadingMore(true);
    try {
      const result = await api<RunList>(`runs?limit=100&offset=${runs.length}`);
      if (epoch !== identityEpoch.current) return;
      setRuns((current) => [...current, ...result.runs.filter((run) => !current.some((item) => item.run_id === run.run_id))]);
      setHasMore(result.has_more); setTotal(result.total);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setLoadingMore(false); }
  }

  if (checkingSession) return <div className="boot-screen"><Brand /><Loading label="Opening your workspace" /></div>;
  if (!principal) return <SignIn error={sessionError} onSignedIn={(identity) => { clearIdentity(); setSessionError(""); setPrincipal(identity); }} />;

  const title = ({ overview: "Overview", migrations: "Migrations", connections: "Connections", new: "New migration", run: "Migration details" })[view];
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    {mobileMenu && <button className="mobile-backdrop" aria-label="Close navigation" onClick={() => setMobileMenu(false)} />}
    <aside className={`sidebar ${mobileMenu ? "open" : ""}`}>
      <div className="sidebar-brand"><Brand /><button className="icon-button mobile-only" aria-label="Close navigation" onClick={() => setMobileMenu(false)}><X size={20} /></button></div>
      <div className="workspace-label"><span className="workspace-avatar">W</span><span><strong>Migration workspace</strong><small>Governed snapshots</small></span><LockKeyhole size={14} /></div>
      <div className="nav-label">WORKSPACE</div>
      <nav aria-label="Main navigation"><NavItem label="Overview" icon={LayoutDashboard} active={view === "overview"} onClick={() => navigate("overview")} /><NavItem label="Migrations" icon={Workflow} active={["migrations", "run", "new"].includes(view)} onClick={() => navigate("migrations")} badge={runs.filter((run) => run.status === "AWAITING_APPROVAL").length} />{canDiscover && <NavItem label="Connections" icon={Database} active={view === "connections"} onClick={() => navigate("connections")} />}</nav>
      <div className="sidebar-guide"><span><ShieldCheck size={20} /></span><h3>Evidence at every step</h3><p>Reviewed plans. Explicit corrections. Verified outcomes.</p><a href="https://github.com/mahi80/DataMigration/blob/main/docs/data-quality.md" target="_blank" rel="noreferrer">Read the quality guide<ArrowUpRight size={14} /></a></div>
      <div className="sidebar-bottom"><a className="support-link" href={docsUrl} target="_blank" rel="noreferrer"><BookOpen size={17} />Documentation<ArrowUpRight size={14} /></a><div className="profile"><span className="profile-avatar">{principal.subject.slice(0, 2).toUpperCase()}</span><span className="profile-text"><strong title={principal.subject}>{principal.subject}</strong><small>{principal.role}</small></span><button className="icon-button" aria-label="Sign out" onClick={signOut}><LogOut size={17} /></button></div></div>
    </aside>
    <div className="workspace-main"><header className="topbar"><div className="topbar-left"><button className="icon-button mobile-only" aria-label="Open navigation" onClick={() => setMobileMenu(true)}><Menu size={20} /></button><span className="breadcrumb-root">Workspace</span><ChevronRight size={13} /><strong>{title}</strong></div><div className="topbar-right"><span className={`service-state ${health?.draining ? "maintenance" : health?.status === "ok" ? "online" : "offline"}`}><span />{health?.draining ? "Maintenance mode" : health?.status === "ok" ? "API connected" : "Checking service"}</span><span className="topbar-divider" /><a href={docsUrl} target="_blank" rel="noreferrer" className="icon-button" aria-label="Open documentation"><CircleHelp size={19} /></a></div></header>
      <main id="main-content" className="main-content">
        {error && <Notice tone="danger">{error}</Notice>}
        {health?.draining && <Notice tone="warning">The workspace is in maintenance. You can review existing runs; new work resumes when maintenance ends.</Notice>}
        {loading ? <Loading /> : view === "overview" ? <Overview runs={runs} total={total} connections={connections} principal={principal} lastSynced={lastSynced} refreshing={refreshing} onRefresh={refreshWorkspace} onOpen={(id) => navigate("run", id)} onNew={() => navigate("new")} onAll={() => navigate("migrations")} />
          : view === "migrations" ? <><div className="page-heading"><div><span className="eyebrow">YOUR DATA JOURNEYS</span><h1>Migrations</h1><p>Review plans, follow execution, and inspect validated outcomes.</p></div><div className="heading-actions"><button className="button secondary" onClick={refreshWorkspace} disabled={refreshing}><RefreshCw size={16} className={refreshing ? "spin" : ""} />Refresh</button>{canPlan && <button className="button primary" onClick={() => navigate("new")} disabled={health?.draining}><Plus size={17} />New migration</button>}</div></div><section className="panel"><div className="panel-heading"><div className="inline-heading"><h2>Migration history</h2><span className="count-label">{formatCount(total)}</span></div><span className="muted small-text">Only runs permitted for your role</span></div><RunTable runs={runs} onOpen={(id) => navigate("run", id)} hasMore={hasMore} loadingMore={loadingMore} onLoadMore={loadMore} /></section></>
          : view === "connections" ? canDiscover ? <ConnectionsView connections={connections} capabilities={capabilities} /> : <Notice>Your role does not include connection discovery.</Notice>
          : view === "new" ? canPlan ? <MigrationWizard connections={connections} onCancel={() => navigate("migrations")} onCreated={(id) => { navigate("run", id); void refreshWorkspace(); }} /> : <Notice>Your role can review run outcomes. Creating plans requires a planner or operator identity.</Notice>
          : selectedRun ? <RunDetailView key={`${principal.subject}:${selectedRun}`} id={selectedRun} principal={principal} summary={runs.find((run) => run.run_id === selectedRun)} focusRequest={runFocus} onBack={() => { navigate("migrations"); void refreshWorkspace(); }} onUpdated={() => void refreshWorkspace()} /> : <EmptyState title="Choose a migration" description="Open a migration from its history to review the plan and result." />}
      </main>
      <footer className="workspace-footer"><span><ShieldCheck size={13} />Plans are reviewed before destination writes.</span><a href="https://github.com/mahi80/DataMigration/blob/main/docs/verification.md" target="_blank" rel="noreferrer">Verification & scope<ArrowUpRight size={12} /></a></footer>
    </div>
    <AskQ key={`${principal.subject}:${principal.role}:${principal.actions.join(",")}`} principal={principal} view={view} runId={selectedRun} onNavigate={navigate} />
  </div>;
}

function NavItem({ label, icon: Icon, active, onClick, badge }: { label: string; icon: typeof Database; active: boolean; onClick: () => void; badge?: number }) {
  return <button className={`nav-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined} onClick={onClick}><Icon size={18} /><span>{label}</span>{Boolean(badge) && <small>{badge}</small>}</button>;
}

function Overview({ runs, total, connections, principal, lastSynced, refreshing, onRefresh, onOpen, onNew, onAll }: { runs: RunSummary[]; total: number; connections: Connection[]; principal: Principal; lastSynced?: string; refreshing: boolean; onRefresh: () => void; onOpen: (id: string) => void; onNew: () => void; onAll: () => void }) {
  const pending = runs.filter((run) => run.status === "AWAITING_APPROVAL");
  const active = runs.filter((run) => run.status === "RUNNING").length;
  const passed = runs.filter((run) => run.validation?.passed);
  const failed = runs.filter((run) => run.validation && !run.validation.passed);
  const validatedRows = passed.reduce((count, run) => count + (run.validation?.expected_rows || 0), 0);
  const validatedTables = passed.reduce((count, run) => count + (run.validation?.tables || 0), 0);
  return <>
    <div className="page-heading"><div><span className="eyebrow">YOUR MIGRATION CONTROL ROOM</span><h1>Migration overview</h1><p>A clear view of every plan, review, and validated outcome.</p></div><div className="heading-actions"><button className="button secondary" onClick={onRefresh} disabled={refreshing} aria-label="Refresh workspace"><RefreshCw size={16} className={refreshing ? "spin" : ""} /><span>Refresh</span></button>{principal.actions.includes("plan") && <button className="button primary" onClick={onNew}><Plus size={17} />New migration</button>}</div></div>
    <div className="overview-banner"><div className="banner-copy"><span className="banner-kicker"><ShieldCheck size={15} />A GOVERNED PATH FOR YOUR DATA</span><h2>Move forward. Keep the evidence.</h2><p>Prepare an explicit plan, approve the exact mapping, and reconcile the result.</p><a href="https://github.com/mahi80/DataMigration/blob/main/docs/data-quality.md" target="_blank" rel="noreferrer">How validation works<ArrowUpRight size={15} /></a></div><div className="banner-flow" aria-label="Workflow: discover, review, validate"><span><Database size={23} /><strong>Discover</strong><small>Know the source</small></span><ArrowRight size={19} /><span><GitBranch size={23} /><strong>Review</strong><small>Approve the plan</small></span><ArrowRight size={19} /><span><ShieldCheck size={23} /><strong>Validate</strong><small>Verify the result</small></span></div></div>
    <div className="section-caption"><span>RECENT WORKSPACE ACTIVITY</span><small>Metrics cover {formatCount(runs.length)} loaded runs of {formatCount(total)} permitted runs.</small></div>
    <div className="metrics-grid"><Metric label="Running now" value={active} detail="Runs currently executing" icon={Activity} /><Metric label="Awaiting review" value={pending.length} detail="Plans ready for a decision" icon={GitBranch} tone="amber" /><Metric label="Validated runs" value={passed.length} detail="Reconciliation passed" icon={ShieldCheck} tone="blue" /><Metric label="Validated rows" value={formatCount(validatedRows)} detail="Across passed run reports" icon={Layers3} tone="violet" /></div>
    <div className="overview-grid"><section className="panel recent-runs"><div className="panel-heading"><div><h2>Recent migrations</h2><p>Latest activity in your permitted workspace</p></div><button className="text-link" onClick={onAll}>View all<ArrowRight size={15} /></button></div><RunTable runs={runs} compact onOpen={onOpen} /></section><section className="panel review-queue"><div className="panel-heading"><div className="inline-heading"><h2>Review queue</h2><span className="count-label amber">{pending.length}</span></div><GitBranch size={17} className="muted" /></div>{pending.length ? <div className="review-list">{pending.slice(0, 3).map((run) => <button key={run.run_id} onClick={() => onOpen(run.run_id)}><span className="review-item-icon"><Table2 size={17} /></span><span><strong>{run.name}</strong><small>{run.table_count} tables · {kindName(run.target_kind)}</small></span><ChevronRight size={16} /></button>)}<p>{principal.actions.includes("approve") ? "Your approver role can review these plans." : "Plans remain paused until an approver decides."}</p></div> : <div className="queue-clear"><span><CheckCheck size={27} /></span><h3>No pending reviews</h3><p>Plans waiting for approval will appear here.</p></div>}</section></div>
    <div className="overview-bottom"><section className="panel validation-overview"><div className="panel-heading"><div><h2>Validation at a glance</h2><p>Evidence from loaded reconciliation reports</p></div><ShieldCheck size={20} className="teal-text" /></div><div className="validation-overview-body"><div className="validation-total"><strong>{formatCount(validatedTables)}</strong><span>tables in passed reports</span></div><div className="validation-distribution">{passed.length + failed.length ? <div className="validation-bar" aria-label={`${passed.length} reports passed, ${failed.length} require attention`}><span style={{ width: `${passed.length / (passed.length + failed.length) * 100}%` }} /><i style={{ width: `${failed.length / (passed.length + failed.length) * 100}%` }} /></div> : <div className="validation-bar empty" />}<div className="validation-legend"><span><i className="green" />{passed.length} passed reports</span><span><i className="red" />{failed.length} need attention</span></div><p>Full mapped-column checks. No raw source values displayed.</p></div></div></section><section className="panel workspace-access"><div className="panel-heading"><h2>Your workspace access</h2><LockKeyhole size={17} className="muted" /></div><div className="panel-body compact"><div className="access-role"><span className="profile-avatar">{principal.subject.slice(0, 2).toUpperCase()}</span><span><strong>{principal.role}</strong><small>{principal.subject}</small></span></div><div className="permission-tags"><span>Read run outcomes</span>{principal.actions.includes("plan") && <span>Create plans</span>}{principal.actions.includes("approve") && <span>Approve execution</span>}{principal.actions.includes("discover") && <span>{connections.length} connections</span>}</div></div></section></div>
    <div className="sync-note"><span className="status-dot" />{lastSynced ? `Last synchronized ${dateLabel(lastSynced)}` : "Refresh to retrieve the latest workspace state"}</div>
  </>;
}

function SignIn({ error: initialError, onSignedIn }: { error: string; onSignedIn: (principal: Principal) => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
      const result = await response.json() as { principal?: Principal; error?: string; detail?: string };
      if (!response.ok || !result.principal) throw new Error(result.error || result.detail || "Access could not be verified. Check your token and try again.");
      setToken(""); onSignedIn(result.principal);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  return <div className="signin-layout"><section className="signin-story"><Brand /><div className="signin-story-main"><span className="signin-eyebrow"><Sparkles size={14} />YOUR GOVERNED DATA WORKSPACE</span><h1>Every migration.<br />A clear path.<br /><em>Verified.</em></h1><p>Bring discovery, reviewed mappings, and validation together in one focused workspace.</p><div className="signin-diagram"><div><span><Database size={21} /></span><strong>Discover</strong><small>Source & destination</small></div><ArrowRight size={20} /><div><span><GitBranch size={21} /></span><strong>Review</strong><small>Mappings & quality</small></div><ArrowRight size={20} /><div><span><ShieldCheck size={21} /></span><strong>Validate</strong><small>Evidence & lineage</small></div></div></div><div className="signin-story-footer"><ShieldCheck size={17} />Explicit approvals. Auditable outcomes.</div></section><section className="signin-form-section"><div className="signin-form-wrap"><span className="signin-lock"><LockKeyhole size={25} /></span><span className="eyebrow">WELCOME TO YOUR WORKSPACE</span><h2>Sign in to DataMigration</h2><p>Use the access token assigned to your workspace identity.</p><form onSubmit={submit}><label htmlFor="workspace-token">Workspace access token</label><input id="workspace-token" name="token" type="password" autoComplete="off" spellCheck={false} required minLength={24} value={token} onChange={(event) => setToken(event.target.value)} placeholder="Enter your access token" disabled={busy} /><small>Your role and permitted data are verified by the server.</small>{(error || initialError) && <Notice tone="danger">{error || initialError}</Notice>}<button className="button primary" type="submit" disabled={busy || token.length < 24}>{busy ? <LoaderCircle size={18} className="spin" /> : null}{busy ? "Verifying access…" : "Open workspace"}<ArrowRight size={17} /></button></form><div className="signin-help"><CircleHelp size={17} /><span>Need access? Ask your workspace administrator for an identity and token.</span></div><a className="signin-docs" href={docsUrl} target="_blank" rel="noreferrer">Setup & documentation<ArrowUpRight size={14} /></a></div><div className="signin-bottom"><span>DataMigration</span><span>Plan with confidence. Move with evidence.</span></div></section></div>;
}
