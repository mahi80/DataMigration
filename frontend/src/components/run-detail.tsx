"use client";

import { ArrowLeft, ArrowRight, Check, CheckCheck, Copy, Download, FileJson, GitBranch, Hash, KeyRound, LoaderCircle, RefreshCw, RotateCcw, ShieldCheck, Table2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { Plan, Principal, RunDetail as RunDetailValue, RunSummary, ValidationTable } from "./types";
import { api, dateLabel, errorMessage, formatCount, kindName } from "./types";
import { DatabaseGlyph, EmptyState, Lifecycle, Loading, Metric, Notice, StatusBadge } from "./ui";

export function RunDetailView({ id, principal, summary, onBack, onUpdated }: { id: string; principal: Principal; summary?: RunSummary; onBack: () => void; onUpdated: () => void }) {
  const [run, setRun] = useState<RunDetailValue | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState("");
  const [planError, setPlanError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [tab, setTab] = useState("summary");
  const [confirmation, setConfirmation] = useState<"approve" | "reject" | "retry" | null>(null);
  const [copied, setCopied] = useState(false);
  const canReadPlan = principal.actions.includes("read_plan");
  const canApprove = principal.actions.includes("approve");
  const canRetry = principal.actions.includes("retry");

  const refresh = useCallback(async () => {
    try {
      const next = await api<RunDetailValue>(`runs/${encodeURIComponent(id)}`);
      setRun(next); setError("");
      if (canReadPlan && next.plan_hash) {
        try { const nextPlan = await api<Plan>(`runs/${encodeURIComponent(id)}/plan`); setPlan(nextPlan); setPlanError(""); }
        catch (failure) { setPlanError(errorMessage(failure)); }
      }
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  }, [id, canReadPlan]);

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);
  const status = run?.status;
  useEffect(() => {
    if (!status || !["RUNNING", "CREATED"].includes(status)) return;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [status, refresh]);

  async function decide(action: "approve" | "reject" | "retry") {
    if (!run) return;
    setBusy(true); setError("");
    try {
      if (action === "retry") await api<RunDetailValue>(`runs/${encodeURIComponent(id)}/retry`, { method: "POST", body: "{}" });
      else await api<RunDetailValue>(`runs/${encodeURIComponent(id)}/approval`, { method: "POST", body: JSON.stringify({ approved: action === "approve", plan_hash: action === "approve" ? plan?.plan_hash : run.plan_hash }) });
      setConfirmation(null); setReviewed(false); await refresh(); onUpdated();
    } catch (failure) {
      setConfirmation(null);
      await refresh();
      setError(`${errorMessage(failure)} The latest run status is shown below; check it before retrying.`);
      onUpdated();
    } finally { setBusy(false); }
  }

  function download() {
    if (!plan) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(plan, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `migration-plan-${id}.json`; link.click(); URL.revokeObjectURL(url);
  }

  async function copyHash() {
    try { await navigator.clipboard.writeText(plan?.plan_hash || run?.plan_hash || ""); setCopied(true); }
    catch { setError("Clipboard access is unavailable. Select and copy the displayed plan hash."); }
  }

  if (loading) return <Loading label="Loading migration" />;
  if (!run) return <><button className="back-link" onClick={onBack}><ArrowLeft size={16} />Back to migrations</button><Notice tone="danger">{error || "This migration is not available to your account."}</Notice><button className="button secondary" onClick={refresh}><RefreshCw size={16} />Check status again</button></>;

  const name = plan?.spec.name || summary?.name || "Migration run";
  const validation = run.validation;
  const tables = validation?.tables || [];
  const source = plan?.spec.source.kind || summary?.source_kind;
  const target = plan?.spec.target.kind || summary?.target_kind;
  const correctionCount = tables.reduce((total, table) => total + Object.values(table.quality_rules || {}).reduce((count, rule) => count + rule.corrected, 0), 0);

  return <div className="run-detail">
    <button className="back-link" onClick={onBack}><ArrowLeft size={16} />Back to migrations</button>
    <div className="page-heading"><div><div className="heading-kicker"><span className="eyebrow">MIGRATION DETAILS</span><StatusBadge status={run.status} /></div><h1>{name}</h1><p><span className="mono">{id}</span><span className="dot-separator">·</span>Updated {dateLabel(run.updated_at || summary?.updated_at)}</p></div><div className="heading-actions"><button className="button secondary" onClick={refresh} disabled={busy}><RefreshCw size={16} />Refresh</button>{plan && <button className="button secondary" onClick={download}><Download size={16} />Export plan</button>}</div></div>
    {error && <Notice tone="danger">{error}</Notice>}
    {run.error && <Notice tone="danger"><strong>The run needs attention.</strong> {run.error}</Notice>}
    {run.status === "COMPLETED" && <Notice tone="success"><strong>Migration validated.</strong> All mapped rows passed the configured reconciliation scope.</Notice>}
    <section className="panel lifecycle-panel"><Lifecycle status={run.status} hasPlan={Boolean(run.plan_hash)} passed={validation?.passed} /></section>

    {run.status === "AWAITING_APPROVAL" && <section className="approval-panel"><div className="approval-intro"><span className="approval-icon"><ShieldCheck size={25} /></span><div><h2>Ready for a careful review</h2><p>Check the source, destination, key mappings, and quality rules. Approval applies to the exact plan below.</p></div></div>{canApprove ? <><label className="checkbox-line"><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} disabled={!plan || busy} />I have reviewed the source, destination, mappings, and quality rules.</label><div className="approval-actions"><span>Your approval is recorded as <strong>{principal.subject}</strong>.</span><button className="button secondary" disabled={busy || !run.plan_hash} onClick={() => setConfirmation("reject")}><X size={16} />Reject plan</button><button className="button primary" disabled={!reviewed || !plan || busy} onClick={() => setConfirmation("approve")}><Check size={17} />Approve and run</button></div></> : <Notice>A permitted approver can review and approve this plan. Your <strong>{principal.role}</strong> role can follow its progress.</Notice>}</section>}

    <div className="tabs" role="tablist" aria-label="Migration details"><button role="tab" aria-selected={tab === "summary"} onClick={() => setTab("summary")}>Overview</button><button role="tab" aria-selected={tab === "mappings"} onClick={() => setTab("mappings")}><GitBranch size={15} />Mappings & lineage</button><button role="tab" aria-selected={tab === "validation"} onClick={() => setTab("validation")}><CheckCheck size={15} />Validation{validation && <span className={`tab-dot ${validation.passed ? "passed" : "failed"}`} />}</button>{canReadPlan && <button role="tab" aria-selected={tab === "json"} onClick={() => setTab("json")}><FileJson size={15} />Plan JSON</button>}</div>

    {tab === "summary" && <div role="tabpanel" className="tab-content"><div className="metrics-grid three"><Metric label="Mapped tables" value={plan?.spec.tables.length ?? summary?.table_count ?? "—"} detail="Reviewed source-to-target mappings" icon={Table2} /><Metric label="Rows reconciled" value={validation ? formatCount(tables.reduce((total, table) => total + table.expected_rows, 0)) : "—"} detail={validation ? "Staged rows included in validation" : "Available after execution"} icon={CheckCheck} tone="blue" /><Metric label="Correction events" value={validation ? formatCount(correctionCount) : "—"} detail={validation ? "Applied by approved quality rules" : "Recorded during the migration"} icon={ShieldCheck} tone="amber" /></div>
      <div className="detail-grid"><section className="panel"><div className="panel-heading"><h2>Migration route</h2><span className="pill neutral">Snapshot</span></div><div className="detail-route"><div><DatabaseGlyph kind={source || ""} /><span><small>READ FROM</small><strong>{kindName(source || "")}</strong><code>{plan?.spec.source.url_env || summary?.source_connection || "Restricted"}</code></span></div><div className="route-connector"><ArrowRight size={21} /></div><div><DatabaseGlyph kind={target || ""} /><span><small>WRITE TO</small><strong>{kindName(target || "")}</strong><code>{plan?.spec.target.url_env || summary?.target_connection || "Restricted"}</code></span></div></div>{plan && <div className="policy-summary"><span><ShieldCheck size={15} />Source snapshot: owner confirmed</span><span>{plan.spec.policy.target_scope === "entire_table" ? "Entire destination table validated" : "Migrated keys validated"}</span></div>}</section>
      <section className="panel"><div className="panel-heading"><h2>Review evidence</h2><Hash size={17} className="muted" /></div><div className="panel-body compact"><span className="field-label">Plan fingerprint</span>{run.plan_hash ? <div className="hash-field"><code>{run.plan_hash}</code><button className="icon-button" onClick={copyHash} aria-label="Copy plan hash">{copied ? <Check size={16} /> : <Copy size={16} />}</button></div> : <p className="muted">The plan fingerprint will appear after discovery.</p>}<p className="quiet-note">The approved hash binds the mappings, quality rules, and connection references. Changes require a new plan.</p>{plan && <dl className="key-value-list"><div><dt>Row limit per table</dt><dd>{formatCount(plan.spec.policy.max_rows_per_table)}</dd></div><div><dt>Batch size</dt><dd>{formatCount(plan.spec.policy.batch_size)}</dd></div><div><dt>Quality rules</dt><dd>{plan.spec.tables.reduce((total, table) => total + table.quality_rules.length, 0)}</dd></div></dl>}</div></section></div>
      {canRetry && ["FAILED", "CREATED"].includes(run.status) && <div className="recovery-card"><div><h3>Recover this run</h3><p>Review the failure and retained receipts before retrying. Existing approvals and the plan binding are checked again.</p></div><button className="button secondary" disabled={busy} onClick={() => setConfirmation("retry")}><RotateCcw size={16} />Retry run</button></div>}
    </div>}

    {tab === "mappings" && <div role="tabpanel" className="tab-content">{!canReadPlan ? <Notice>Your role can read run outcomes. Mapping details require permission to read the plan.</Notice> : planError ? <Notice tone="danger">{planError}</Notice> : !plan ? <EmptyState icon={GitBranch} title="Mappings are being prepared" description="The reviewed table and column mappings will appear after planning completes." /> : <><div className="lineage-heading"><h2>Follow every mapped column</h2><p>Explicit source-to-target lineage from this plan, including transformations and quality checks.</p></div>{plan.spec.tables.map((table) => <section key={table.target} className="panel lineage-card"><div className="lineage-route"><div><span className="eyebrow">SOURCE TABLE</span><strong><Table2 size={17} />{table.source}</strong></div><div className="lineage-bridge"><span /><ArrowRight size={21} /><span /></div><div><span className="eyebrow">TARGET TABLE</span><strong><Table2 size={17} />{table.target}</strong></div><span className="pill neutral">{table.columns.length} columns</span></div><div className="table-scroll"><table className="data-table"><thead><tr><th>Source column</th><th>Transformation</th><th>Target column</th><th>Type</th><th>Quality checks</th></tr></thead><tbody>{table.columns.map((column) => <tr key={column.target}><td><code>{column.source}</code></td><td><span className="transform-tag">{column.transform === "identity" ? "Direct mapping" : column.transform.replaceAll("_", " ")}</span></td><td><span className="column-label">{table.keys.includes(column.target) && <KeyRound size={13} className="teal-text" />}<code>{column.target}</code></span></td><td className="muted">{column.data_type}{column.precision != null ? `(${column.precision},${column.scale})` : ""}{column.timezone ? ` · ${column.timezone}` : ""}</td><td>{table.quality_rules.filter((rule) => rule.column === column.target).map((rule) => <span key={rule.name} className="rule-tag">{rule.check.replaceAll("_", " ")}{rule.correction !== "none" ? ` · ${rule.correction}` : ""}</span>)}{!table.quality_rules.some((rule) => rule.column === column.target) && <span className="muted">—</span>}</td></tr>)}</tbody></table></div><div className="lineage-footer"><span><KeyRound size={13} />Keys: {table.keys.join(", ")}</span><span>{table.depends_on.length ? `Loads after: ${table.depends_on.join(", ")}` : "No table dependencies"}</span></div></section>)}</>}</div>}

    {tab === "validation" && <div role="tabpanel" className="tab-content">{!validation ? <section className="panel"><EmptyState icon={ShieldCheck} title="Validation follows execution" description="The worker stages and checks the source, writes approved batches, then reconciles every mapped column. Results will appear here." /></section> : <><div className="validation-banner"><span className={validation.passed ? "success-mark" : "failure-mark"}>{validation.passed ? <ShieldCheck size={28} /> : <X size={28} />}</span><div><h2>{validation.passed ? "Mapped data reconciled" : "Reconciliation needs attention"}</h2><p>{tables.length} table reports · Full mapped-column comparison · {correctionCount} correction events</p></div><StatusBadge status={validation.passed ? "VALIDATED" : "FAILED"} /></div><section className="panel"><div className="table-scroll"><table className="data-table"><thead><tr><th>Target table</th><th>Expected rows</th><th>Target rows</th><th>Missing</th><th>Extra</th><th>Duplicate</th><th>Mismatched</th><th>Result</th></tr></thead><tbody>{tables.map((table) => <tr key={table.table}><td><strong>{table.table}</strong><span className="table-subtitle">{table.scope === "mapped_keys" ? "Migrated keys only" : "Entire table"}</span></td><td>{formatCount(table.expected_rows)}</td><td>{formatCount(table.target_rows)}</td><td className={table.missing_keys ? "danger-text" : "muted"}>{table.missing_keys}</td><td className={table.extra_keys && table.scope !== "mapped_keys" ? "danger-text" : "muted"}>{table.extra_keys}</td><td className={table.duplicate_keys ? "danger-text" : "muted"}>{table.duplicate_keys}</td><td className={table.mismatched_rows ? "danger-text" : "muted"}>{table.mismatched_rows}</td><td><span className={`result-label ${table.passed ? "passed" : "failed"}`}>{table.passed ? <Check size={14} /> : <X size={14} />}{table.passed ? "Passed" : "Failed"}</span></td></tr>)}</tbody></table></div></section><QualityResults tables={tables} /></>}</div>}

    {tab === "json" && canReadPlan && <div role="tabpanel" className="tab-content">{planError ? <Notice tone="danger">{planError}</Notice> : plan ? <section className="panel"><div className="panel-heading"><div><h2>Exact migration plan</h2><p>Review the complete versioned contract before approval.</p></div><button className="button secondary small" onClick={download}><Download size={15} />Download JSON</button></div><pre className="json-panel">{JSON.stringify(plan, null, 2)}</pre></section> : <Loading label="Waiting for plan" />}</div>}

    {confirmation && <ConfirmDialog action={confirmation} target={kindName(target || "destination")} hash={plan?.plan_hash || run.plan_hash || ""} busy={busy} onCancel={() => !busy && setConfirmation(null)} onConfirm={() => void decide(confirmation)} />}
  </div>;
}

function QualityResults({ tables }: { tables: ValidationTable[] }) {
  const rules = tables.flatMap((table) => Object.entries(table.quality_rules || {}).map(([name, rule]) => ({ table: table.table, name, ...rule })));
  return <section className="panel"><div className="panel-heading"><div><h2>Quality and correction evidence</h2><p>Counts from the final staged data, after approved corrections.</p></div><ShieldCheck size={19} className="teal-text" /></div>{rules.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>Table / column</th><th>Check</th><th>Values checked</th><th>Corrections applied</th></tr></thead><tbody>{rules.map((rule) => <tr key={`${rule.table}-${rule.name}`}><td><strong>{rule.table}</strong><span className="table-subtitle">{rule.column}</span></td><td>{rule.check.replaceAll("_", " ")}</td><td>{formatCount(rule.checked)}</td><td><span className={rule.corrected ? "correction-count" : "muted"}>{formatCount(rule.corrected)}</span></td></tr>)}</tbody></table></div> : <div className="panel-body"><p className="quiet-note">No additional quality rules were configured. Key checks, type handling, and mapped-column reconciliation still apply.</p></div>}</section>;
}

function ConfirmDialog({ action, target, hash, busy, onCancel, onConfirm }: { action: "approve" | "reject" | "retry"; target: string; hash: string; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  const title = action === "approve" ? "Approve and execute this plan?" : action === "reject" ? "Reject this migration plan?" : "Retry this migration?";
  return <dialog ref={dialog} className="confirm-dialog" aria-labelledby="decision-title" onCancel={(event) => { event.preventDefault(); onCancel(); }}><div className="dialog-icon"><ShieldCheck size={26} /></div><h2 id="decision-title">{title}</h2><p>{action === "approve" ? `This authorizes the reviewed mappings and corrections to write to ${target}. The worker will validate the result and retain its execution receipts.` : action === "reject" ? "This ends the pending plan. Create a new reviewed plan when its mappings or prerequisites are ready." : "Confirm that you have investigated the failure. The worker reuses retained staging and receipts and checks the existing approval."}</p>{hash && <div className="dialog-fingerprint"><span>Exact plan fingerprint</span><code>{hash}</code></div>}<div className="dialog-actions"><button className="button secondary" onClick={onCancel} disabled={busy}>Cancel</button><button className={`button ${action === "reject" ? "danger" : "primary"}`} onClick={onConfirm} disabled={busy}>{busy && <LoaderCircle size={16} className="spin" />}{busy ? "Submitting…" : action === "approve" ? "Confirm approval" : action === "reject" ? "Confirm rejection" : "Confirm retry"}</button></div></dialog>;
}
