"use client";

import { ArrowRight, ArrowUpRight, Search, Workflow } from "lucide-react";
import { useState } from "react";
import type { RunSummary } from "./types";
import { dateLabel, formatCount } from "./types";
import { EmptyState, RouteLabel, StatusBadge } from "./ui";

export function RunTable({ runs, onOpen, compact = false, loadingMore, hasMore, onLoadMore }: { runs: RunSummary[]; onOpen: (id: string) => void; compact?: boolean; loadingMore?: boolean; hasMore?: boolean; onLoadMore?: () => void }) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const filtered = runs.filter((run) => (status === "all" || run.status === status) && `${run.name} ${run.run_id} ${run.source_kind} ${run.target_kind}`.toLowerCase().includes(search.toLowerCase()));
  const displayed = compact ? filtered.slice(0, 5) : filtered;
  return <div className="run-table-wrap">
    {!compact && <div className="table-toolbar"><label className="search-field"><Search size={17} /><input aria-label="Search migrations" placeholder="Search loaded migrations…" value={search} onChange={(event) => setSearch(event.target.value)} /></label><label className="sr-only" htmlFor="run-status-filter">Filter status</label><select id="run-status-filter" value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option><option value="AWAITING_APPROVAL">Awaiting review</option><option value="RUNNING">Running</option><option value="COMPLETED">Completed</option><option value="FAILED">Failed</option><option value="REJECTED">Rejected</option></select><span className="table-count">{formatCount(filtered.length)} loaded</span></div>}
    {displayed.length ? <div className="table-scroll"><table className="data-table runs-table"><thead><tr><th>Migration</th><th>Source → target</th><th>Status</th><th>Tables</th><th>Updated</th><th><span className="sr-only">Open</span></th></tr></thead><tbody>{displayed.map((run) => <tr key={run.run_id}><td><button className="table-title" onClick={() => onOpen(run.run_id)}>{run.name || run.run_id}</button><span className="table-subtitle mono">{run.run_id.length > 24 ? `${run.run_id.slice(0, 24)}…` : run.run_id}</span></td><td><RouteLabel source={run.source_kind} target={run.target_kind} compact /></td><td><StatusBadge status={run.status} /></td><td><span className="tabular">{run.table_count}</span></td><td className="muted nowrap">{dateLabel(run.updated_at || run.created_at)}</td><td><button className="icon-button" aria-label={`Open ${run.name || run.run_id}`} onClick={() => onOpen(run.run_id)}><ArrowUpRight size={18} /></button></td></tr>)}</tbody></table></div> : <EmptyState icon={Workflow} title={search || status !== "all" ? "No matching migrations" : "Your migrations will appear here"} description={search || status !== "all" ? "Try another search or status filter. Search covers the loaded run window." : "Create a migration plan to inspect its mappings and send it for review."} />}
    {!compact && hasMore && <div className="table-footer"><span>Showing {formatCount(runs.length)} recent, permitted runs.</span><button className="button ghost small" disabled={loadingMore} onClick={onLoadMore}>{loadingMore ? "Loading…" : "Load more"}<ArrowRight size={14} /></button></div>}
  </div>;
}
