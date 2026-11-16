"use client";

import { ArrowRight, ArrowUpRight, ChevronRight, Database, Layers3, LoaderCircle, LockKeyhole, Plus, Search, Table2, X } from "lucide-react";
import { useState } from "react";
import type { Connection, ConnectorCapability, Discovery, TableSchema } from "./types";
import { api, errorMessage, kindName } from "./types";
import { DatabaseGlyph, EmptyState, Notice } from "./ui";

export function ConnectionsView({ connections, capabilities }: { connections: Connection[]; capabilities: Record<string, ConnectorCapability> }) {
  const [exploring, setExploring] = useState<Connection | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  const [search, setSearch] = useState("");
  const visible = connections.filter((connection) => `${connection.label} ${connection.kind}`.toLowerCase().includes(search.toLowerCase()));
  return <><div className="page-heading"><div><span className="eyebrow">KNOW YOUR DATA ESTATE</span><h1>Connections</h1><p>Explore the registered databases and file stores available to your role.</p></div><button className="button secondary" onClick={() => setShowSetup(!showSetup)}><Plus size={16} />Connection setup</button></div>{showSetup && <Notice>Connections are registered by your workspace administrator. The console uses approved connection profiles; database credentials stay on the server. <a href="https://github.com/mahi80/DataMigration/blob/main/docs/frontend.md" target="_blank" rel="noreferrer">Open setup instructions <ArrowUpRight size={12} /></a></Notice>}<div className="connections-toolbar"><label className="search-field"><Search size={17} /><input aria-label="Search connections" placeholder="Search connections…" value={search} onChange={(event) => setSearch(event.target.value)} /></label><span>{visible.length} registered connections</span></div>
    {visible.length ? <div className="connection-grid">{visible.map((connection) => <section className="panel connection-card" key={connection.id}><div className="connection-card-top"><DatabaseGlyph kind={connection.kind} /><span className="pill neutral">{kindName(connection.kind)}</span></div><h2>{connection.label}</h2><p>{connection.description || "Registered workspace connection"}</p><dl><div><dt>Connection reference</dt><dd className="mono">{connection.url_env}</dd></div><div><dt>Table access</dt><dd>{connection.allowed_tables ? `${connection.allowed_tables.length} permitted tables` : "Configured namespace"}</dd></div></dl><div className="connection-card-bottom"><span><LockKeyhole size={13} />Server-managed access</span><button className="text-link" onClick={() => setExploring(connection)}>Explore<ArrowRight size={15} /></button></div></section>)}</div> : <section className="panel"><EmptyState icon={Database} title={search ? "No matching connections" : "Register your first connections"} description={search ? "Try a different name or database type." : "Your administrator can register source and destination profiles, then assign access to your identity."} /></section>}
    {exploring && <ConnectionExplorer key={exploring.id} connection={exploring} onClose={() => setExploring(null)} />}
    <section className="panel connector-coverage"><div className="panel-heading"><div><h2>Connector coverage</h2><p>Implemented adapters and the verification level recorded by the backend</p></div><Layers3 size={19} className="muted" /></div><div className="table-scroll"><table className="data-table"><thead><tr><th>Connector</th><th>Source</th><th>Target</th><th>Verification</th><th>Scope</th></tr></thead><tbody>{Object.entries(capabilities).map(([kind, capability]) => <tr key={kind}><td><span className="column-label"><DatabaseGlyph kind={kind} size="small" /><strong>{kindName(kind)}</strong></span></td><td>{capability.source ? "Yes" : "—"}</td><td>{capability.target ? "Yes" : "—"}</td><td><span className="verification-label">{capability.status.replaceAll("-", " ")}</span></td><td className="connector-note">{capability.notes}</td></tr>)}</tbody></table></div></section>
  </>;
}

function ConnectionExplorer({ connection, onClose }: { connection: Connection; onClose: () => void }) {
  const [scope, setScope] = useState("source");
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [schema, setSchema] = useState<TableSchema | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function discover(table?: string) {
    setBusy(true); setError("");
    try {
      const result = await api<Discovery>("discover", { method: "POST", body: JSON.stringify({ connection: { kind: connection.kind, url_env: connection.url_env, options: connection.options }, scope, include_schemas: Boolean(table), ...(table ? { tables: [table] } : {}), max_tables: 1000 }) });
      if (table) setSchema(result.tables[0]); else { setDiscovery(result); setSchema(null); }
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  return <section className="panel explorer"><div className="panel-heading"><div><h2>{connection.label}</h2><p>Inspect table metadata without returning source rows.</p></div><button className="icon-button" aria-label="Close connection explorer" onClick={onClose}><X size={18} /></button></div><div className="explorer-controls"><label>Inventory role<select value={scope} onChange={(event) => { setScope(event.target.value); setDiscovery(null); setSchema(null); }}><option value="source">Source inventory</option><option value="target">Destination inventory</option></select></label><button className="button primary" onClick={() => void discover()} disabled={busy}>{busy ? <LoaderCircle size={16} className="spin" /> : <Search size={16} />}Discover tables</button></div>{error && <div className="panel-body compact"><Notice tone="danger">{error}</Notice></div>}{discovery && <div className="explorer-body"><div className="explorer-tables"><span className="eyebrow">{discovery.table_names.length} TABLES</span>{discovery.table_names.map((table) => <button key={table} className={schema?.name === table ? "active" : ""} onClick={() => void discover(table)} disabled={busy}><Table2 size={15} /><span>{table}</span><ChevronRight size={14} /></button>)}{!discovery.table_names.length && <p>No permitted tables are present in this namespace.</p>}</div><div className="explorer-schema">{schema ? <><h3>{schema.name}</h3><div className="table-scroll"><table className="data-table"><thead><tr><th>Column</th><th>Type</th><th>Nullable</th><th>Key</th></tr></thead><tbody>{schema.columns.map((column) => <tr key={column.name}><td><code>{column.name}</code></td><td>{column.data_type}</td><td>{column.nullable ? "Yes" : "No"}</td><td>{schema.primary_key.includes(column.name) ? "Primary key" : "—"}</td></tr>)}</tbody></table></div></> : <EmptyState icon={Table2} title="Choose a table" description="Select a table to inspect its column types, nullability, and declared keys." />}</div></div>}</section>;
}
