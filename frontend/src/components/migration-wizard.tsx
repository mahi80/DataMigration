"use client";

import { ArrowLeft, ArrowRight, Check, ChevronDown, ChevronUp, Database, KeyRound, Layers3, LoaderCircle, Plus, Search, ShieldCheck, SlidersHorizontal, Table2, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import type { Connection, Discovery, MigrationSpec, QualityRule, RunDetail, TableMapping, TableSchema } from "./types";
import { api, errorMessage, kindName } from "./types";
import { DatabaseGlyph, EmptyState, Notice, StepIndicator } from "./ui";
import { recordInteraction } from "./assistant-events";

const dataTypes = ["integer", "float", "decimal", "string", "boolean", "datetime", "date", "json", "binary"];
const transforms = ["identity", "trim", "lower", "upper", "to_string", "to_integer", "to_decimal", "to_datetime", "json_encode"];
const checks = ["not_null", "non_blank", "unique", "regex", "range"];
const connectionValue = (connection: Connection) => ({ kind: connection.kind, url_env: connection.url_env, options: connection.options });

function initialMapping(schema: TableSchema, target: Connection): TableMapping {
  const simple = schema.name.split(".").at(-1) || schema.name;
  const namespace = typeof target.options.schema === "string" ? target.options.schema : "";
  const targetName = target.allowed_tables?.find((name) => name === simple || name.endsWith(`.${simple}`)) || (namespace ? `${namespace}.${simple}` : simple);
  return {
    source: schema.name,
    target: targetName,
    keys: [...schema.primary_key],
    depends_on: [],
    quality_rules: [],
    columns: schema.columns.map((column) => ({
      source: column.name, target: column.name, data_type: column.data_type,
      nullable: schema.primary_key.includes(column.name) ? false : column.nullable,
      transform: "identity",
      ...(schema.metadata?.decimal_types?.[column.name] || {}),
      ...(schema.metadata?.timestamp_timezones?.[column.name] ? { timezone: schema.metadata.timestamp_timezones[column.name] } : {}),
    })),
  };
}

export function MigrationWizard({ connections, onCancel, onCreated }: { connections: Connection[]; onCancel: () => void; onCreated: (id: string) => void }) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [sourceNames, setSourceNames] = useState<string[]>([]);
  const [targetNames, setTargetNames] = useState<string[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [discovered, setDiscovered] = useState(false);
  const [search, setSearch] = useState("");
  const [mappings, setMappings] = useState<TableMapping[]>([]);
  const [expanded, setExpanded] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [frozen, setFrozen] = useState(false);
  const [batchSize, setBatchSize] = useState(500);
  const [rowBudget, setRowBudget] = useState(100000);
  const [targetScope, setTargetScope] = useState("entire_table");
  const [allowCreate, setAllowCreate] = useState(true);
  const [workload, setWorkload] = useState("oltp");
  const source = connections.find((connection) => connection.id === sourceId);
  const target = connections.find((connection) => connection.id === targetId);
  const visibleNames = useMemo(() => sourceNames.filter((table) => table.toLowerCase().includes(search.toLowerCase())), [sourceNames, search]);

  function changeConnection(side: "source" | "target", id: string) {
    if (side === "source") setSourceId(id); else setTargetId(id);
    setSourceNames([]); setTargetNames([]); setSelected([]); setDiscovered(false); setError("");
  }

  async function discover() {
    if (!source || !target) return;
    setBusy(true); setError("");
    try {
      const [sourceResult, targetResult] = await Promise.all([
        api<Discovery>("discover", { method: "POST", body: JSON.stringify({ connection: connectionValue(source), scope: "source", include_schemas: false, max_tables: 1000 }) }),
        api<Discovery>("discover", { method: "POST", body: JSON.stringify({ connection: connectionValue(target), scope: "target", include_schemas: false, max_tables: 1000 }) }),
      ]);
      setSourceNames(sourceResult.table_names); setTargetNames(targetResult.table_names); setDiscovered(true);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }

  async function loadMappings() {
    if (!source || !target || !selected.length) return;
    if (!name.trim()) { setError("Give this migration a name before continuing."); return; }
    setBusy(true); setError("");
    try {
      const result = await api<Discovery>("discover", { method: "POST", body: JSON.stringify({ connection: connectionValue(source), scope: "source", tables: selected, include_schemas: true, max_tables: 1000 }) });
      setMappings(result.tables.map((schema) => initialMapping(schema, target)));
      setStep(1); setExpanded(0);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }

  function updateMapping(index: number, update: Partial<TableMapping>) {
    recordInteraction("mapping_edited");
    setMappings((current) => current.map((mapping, position) => position === index ? { ...mapping, ...update } : mapping));
  }

  function updateColumn(tableIndex: number, columnIndex: number, update: Partial<TableMapping["columns"][number]>) {
    recordInteraction("mapping_edited");
    setMappings((current) => current.map((mapping, index) => {
      if (index !== tableIndex) return mapping;
      const previousName = mapping.columns[columnIndex].target;
      const nextName = update.target === undefined ? previousName : update.target;
      return {
        ...mapping,
        columns: mapping.columns.map((column, position) => position === columnIndex ? { ...column, ...update } : column),
        keys: mapping.keys.map((key) => key === previousName ? nextName : key),
        quality_rules: mapping.quality_rules.map((rule) => rule.column === previousName ? { ...rule, column: nextName, ...(update.data_type && update.data_type !== "string" ? { correction: "none" } : {}) } : rule),
      };
    }));
  }

  function mappingIssue(): string {
    if (new Set(mappings.map((mapping) => mapping.target)).size !== mappings.length) return "Each table needs a distinct destination name.";
    for (const mapping of mappings) {
      if (!mapping.target.trim()) return `Choose a destination name for ${mapping.source}.`;
      if (target?.allowed_tables && !target.allowed_tables.includes(mapping.target)) return `${mapping.target} is outside the destination tables allowed for your role.`;
      if (!mapping.keys.length) return `Select at least one stable key for ${mapping.target}.`;
      if (mapping.columns.some((column) => !column.target.trim())) return `Every mapped column in ${mapping.target} needs a destination name.`;
      if (new Set(mapping.columns.map((column) => column.target)).size !== mapping.columns.length) return `Destination columns in ${mapping.target} must be unique.`;
      for (const column of mapping.columns) {
        if ((column.precision != null || column.scale != null) && (column.precision == null || column.scale == null || !Number.isInteger(column.precision) || !Number.isInteger(column.scale) || column.precision < 1 || column.precision > 76 || column.scale < 0 || column.scale > column.precision)) return `Review decimal precision and scale for ${mapping.target}.${column.target}.`;
      }
      for (const rule of mapping.quality_rules) {
        if (rule.check === "regex" && !rule.pattern) return `Add a regular expression for ${mapping.target}.${rule.column}.`;
        if (rule.check === "range" && !rule.minimum && !rule.maximum) return `Add a minimum or maximum for ${mapping.target}.${rule.column}.`;
      }
    }
    return "";
  }

  function review() {
    const issue = mappingIssue();
    if (issue) { setError(issue); return; }
    setError(""); setStep(2);
  }

  async function submit() {
    if (!source || !target || !frozen) return;
    const issue = mappingIssue();
    if (issue) { setError(issue); return; }
    const runId = `mig-${Date.now().toString(36)}-${crypto.randomUUID().slice(0, 8)}`;
    const spec: MigrationSpec = {
      spec_version: 1, name: name.trim(), mode: "snapshot", workload,
      source: connectionValue(source), target: connectionValue(target), tables: mappings,
      policy: { source_quiesced: true, batch_size: batchSize, max_rows_per_table: rowBudget, target_scope: targetScope, allow_create_tables: allowCreate },
      advisor: { provider: "disabled" },
    };
    setBusy(true); setError("");
    try {
      const result = await api<RunDetail>("runs", { method: "POST", body: JSON.stringify({ run_id: runId, spec }) });
      onCreated(result.run_id);
    } catch (failure) {
      // A plan request can have completed even if its response was interrupted.
      // Inspect the known run ID instead of blindly submitting another run.
      try { await api<RunDetail>(`runs/${runId}`); onCreated(runId); }
      catch { setError(`${errorMessage(failure)} Run reference: ${runId}.`); }
    } finally { setBusy(false); }
  }

  if (connections.length < 2) return <div className="panel"><EmptyState title="Connect your source and destination" description="Ask your workspace administrator to register two permitted connections. Credentials are managed on the server." action={<button className="button secondary" onClick={onCancel}>Back to workspace</button>} /></div>;

  return <div className="wizard">
    <div className="wizard-heading"><div><span className="eyebrow">NEW MIGRATION</span><h1>Create a migration plan</h1><p>Choose your data, review the mapping, and prepare it for approval.</p></div><button className="button ghost" onClick={onCancel} disabled={busy}>Cancel</button></div>
    <StepIndicator steps={["Connect & select", "Map & check", "Review plan"]} current={step} />
    {error && <Notice tone="danger">{error}</Notice>}
    {step === 0 && <>
      <section className="panel"><div className="panel-heading"><div><h2>Where should your data go?</h2><p>Only connections permitted for your identity are listed.</p></div><span className="pill">Snapshot migration</span></div>
        <div className="panel-body"><label className="field-label" htmlFor="migration-name">Migration name</label><input id="migration-name" className="name-input" maxLength={128} placeholder="e.g. Customer snapshot to analytics" value={name} onChange={(event) => setName(event.target.value)} />
          <div className="connection-pair"><ConnectionPicker label="Source connection" value={sourceId} connections={connections} onChange={(value) => changeConnection("source", value)} selected={source} /><div className="flow-arrow"><ArrowRight size={22} /></div><ConnectionPicker label="Target connection" value={targetId} connections={connections} onChange={(value) => changeConnection("target", value)} selected={target} /></div>
          {source && target && source.url_env === target.url_env && <Notice tone="warning">Choose a separate destination connection for this snapshot.</Notice>}
          <div className="panel-actions"><span className="microcopy"><ShieldCheck size={15} />Credentials stay in your configured connections.</span><button className="button primary" onClick={discover} disabled={busy || !source || !target || source.url_env === target.url_env}>{busy ? <LoaderCircle size={16} className="spin" /> : <Search size={16} />}{discovered ? "Refresh discovery" : "Discover tables"}</button></div>
        </div>
      </section>
      {discovered && <section className="panel"><div className="panel-heading"><div><h2>Select source tables</h2><p>{sourceNames.length} permitted tables discovered · {selected.length} selected</p></div><span className="pill neutral"><Table2 size={13} />{targetNames.length} existing target tables</span></div>
        <div className="table-toolbar"><label className="search-field"><Search size={17} /><input aria-label="Search source tables" placeholder="Find a table…" value={search} onChange={(event) => setSearch(event.target.value)} /></label><button className="button ghost small" onClick={() => setSelected(selected.length === sourceNames.length ? [] : sourceNames)}>{selected.length === sourceNames.length && sourceNames.length ? "Clear selection" : "Select all"}</button></div>
        {sourceNames.length ? <div className="table-selection">{visibleNames.map((table) => <label key={table} className={`select-table ${selected.includes(table) ? "selected" : ""}`}><input type="checkbox" checked={selected.includes(table)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, table] : current.filter((name) => name !== table))} /><Table2 size={17} /><span>{table}</span>{targetNames.some((name) => name === table || name.split(".").at(-1) === table.split(".").at(-1)) && <small>Target exists</small>}</label>)}</div> : <EmptyState icon={Table2} title="No permitted source tables found" description="Review the registered namespace and your table permissions with the workspace administrator." />}
      </section>}
      <div className="wizard-footer"><span>Planning pauses before destination writes.</span><button className="button primary" disabled={!selected.length || !name.trim() || busy} onClick={loadMappings}>{busy && <LoaderCircle size={16} className="spin" />}Continue to mapping<ArrowRight size={16} /></button></div>
    </>}
    {step === 1 && <>
      <div className="mapping-intro"><div><h2>Make the mapping explicit</h2><p>Review destination names, exact types, stable keys, and approved corrections.</p></div><span className="pill"><Layers3 size={14} />{mappings.length} tables</span></div>
      {mappings.map((mapping, tableIndex) => <section className="panel mapping-panel" key={mapping.source}>
        <button className="mapping-header" aria-expanded={expanded === tableIndex} onClick={() => setExpanded(expanded === tableIndex ? -1 : tableIndex)}><span className="mapping-number">{tableIndex + 1}</span><span><strong>{mapping.source}</strong><small>{mapping.columns.length} columns · {mapping.keys.length} key columns · {mapping.quality_rules.length} quality checks</small></span><ArrowRight size={17} /><span className="mapping-destination">{mapping.target}</span>{expanded === tableIndex ? <ChevronUp size={18} /> : <ChevronDown size={18} />}</button>
        {expanded === tableIndex && <div className="mapping-content"><div className="field-grid two"><label>Target table<input aria-label={`Target table for ${mapping.source}`} value={mapping.target} onChange={(event) => { const previous = mapping.target; const next = event.target.value; setMappings((current) => current.map((item, index) => ({ ...item, ...(index === tableIndex ? { target: next } : {}), depends_on: item.depends_on.map((dependency) => dependency === previous ? next : dependency) }))); }} /></label><div className="key-hint"><KeyRound size={18} /><span><strong>Choose stable business keys</strong><small>Key columns must uniquely identify every row.</small></span></div></div>
          <div className="table-scroll"><table className="data-table mapping-table"><thead><tr><th>Key</th><th>Source column</th><th>Target column</th><th>Target type</th><th>Transform</th><th>Nullable</th></tr></thead><tbody>{mapping.columns.map((column, columnIndex) => <tr key={column.source}><td><input aria-label={`Use ${column.source} as key in ${mapping.source}`} type="checkbox" checked={mapping.keys.includes(column.target)} onChange={(event) => { updateMapping(tableIndex, { keys: event.target.checked ? [...mapping.keys, column.target] : mapping.keys.filter((key) => key !== column.target), columns: mapping.columns.map((item, index) => index === columnIndex && event.target.checked ? { ...item, nullable: false } : item), quality_rules: mapping.quality_rules.map((rule) => rule.column === column.target && event.target.checked ? { ...rule, correction: "none" } : rule) }); }} /></td><td><code>{column.source}</code></td><td><input aria-label={`Target column for ${mapping.source}.${column.source}`} value={column.target} onChange={(event) => updateColumn(tableIndex, columnIndex, { target: event.target.value })} /></td><td><select aria-label={`Type for ${mapping.source}.${column.source}`} value={column.data_type} onChange={(event) => updateColumn(tableIndex, columnIndex, { data_type: event.target.value, ...(event.target.value !== "decimal" ? { precision: null, scale: null } : {}), ...(event.target.value !== "datetime" ? { timezone: null } : {}) })}>{dataTypes.map((type) => <option key={type}>{type}</option>)}</select></td><td><select aria-label={`Transform for ${mapping.source}.${column.source}`} value={column.transform} onChange={(event) => updateColumn(tableIndex, columnIndex, { transform: event.target.value })}>{transforms.map((transform) => <option key={transform}>{transform}</option>)}</select></td><td><input type="checkbox" aria-label={`Allow null for ${mapping.source}.${column.source}`} checked={column.nullable} disabled={mapping.keys.includes(column.target)} onChange={(event) => updateColumn(tableIndex, columnIndex, { nullable: event.target.checked })} /></td></tr>)}</tbody></table></div>
          {mapping.columns.some((column) => ["decimal", "datetime"].includes(column.data_type)) && <div className="precision-fields">{mapping.columns.map((column, columnIndex) => column.data_type === "decimal" ? <div className="precision-row" key={column.source}><code>{column.target}</code><label>Precision<input type="number" min={1} max={76} aria-label={`Precision for ${mapping.source}.${column.source}`} value={column.precision ?? ""} onChange={(event) => updateColumn(tableIndex, columnIndex, { precision: event.target.value ? Number(event.target.value) : null })} /></label><label>Scale<input type="number" min={0} max={76} aria-label={`Scale for ${mapping.source}.${column.source}`} value={column.scale ?? ""} onChange={(event) => updateColumn(tableIndex, columnIndex, { scale: event.target.value !== "" ? Number(event.target.value) : null })} /></label></div> : column.data_type === "datetime" ? <div className="precision-row" key={column.source}><code>{column.target}</code><label>Timezone<input aria-label={`Timezone for ${mapping.source}.${column.source}`} placeholder="Keep source semantics" value={column.timezone || ""} onChange={(event) => updateColumn(tableIndex, columnIndex, { timezone: event.target.value || null })} /></label></div> : null)}</div>}
          {mappings.length > 1 && <div className="dependency-field"><span className="field-label">Load after these tables</span><div className="checkbox-chips">{mappings.filter((other) => other.source !== mapping.source).map((other) => <label key={other.source}><input type="checkbox" checked={mapping.depends_on.includes(other.target)} onChange={(event) => updateMapping(tableIndex, { depends_on: event.target.checked ? [...mapping.depends_on, other.target] : mapping.depends_on.filter((name) => name !== other.target) })} />{other.target || other.source}</label>)}</div></div>}
          <QualityEditor mapping={mapping} onChange={(rules) => updateMapping(tableIndex, { quality_rules: rules })} />
        </div>}
      </section>)}
      <div className="wizard-footer"><button className="button secondary" onClick={() => setStep(0)}><ArrowLeft size={16} />Back</button><button className="button primary" onClick={review}>Review plan<ArrowRight size={16} /></button></div>
    </>}
    {step === 2 && <>
      <section className="panel"><div className="panel-heading"><div><h2>{name}</h2><p>A separate approver reviews the generated plan before execution.</p></div><span className="pill"><ShieldCheck size={14} />Review required</span></div><div className="panel-body">
        <div className="review-route"><div><DatabaseGlyph kind={source?.kind || ""} /><span><small>SOURCE</small><strong>{source?.label}</strong><p>{kindName(source?.kind || "")}</p></span></div><ArrowRight size={24} /><div><DatabaseGlyph kind={target?.kind || ""} /><span><small>DESTINATION</small><strong>{target?.label}</strong><p>{kindName(target?.kind || "")}</p></span></div></div>
        <div className="review-counts"><div><strong>{mappings.length}</strong><span>tables mapped</span></div><div><strong>{mappings.reduce((total, mapping) => total + mapping.columns.length, 0)}</strong><span>columns mapped</span></div><div><strong>{mappings.reduce((total, mapping) => total + mapping.quality_rules.length, 0)}</strong><span>quality checks</span></div></div>
        <div className="field-grid two"><label>Workload<select value={workload} onChange={(event) => setWorkload(event.target.value)}><option value="oltp">Transactional / OLTP</option><option value="olap">Analytics / OLAP</option><option value="star_schema">Existing star schema</option><option value="lakehouse">Lakehouse</option><option value="document">Documents</option><option value="key_value">Key/value</option><option value="wide_column">Wide column</option></select></label><label>Reconciliation scope<select value={targetScope} onChange={(event) => setTargetScope(event.target.value)}><option value="entire_table">Entire destination table</option><option value="mapped_keys">Migrated keys only</option></select></label><label>Maximum rows per table<input type="number" min={1} max={10000000} value={rowBudget} onChange={(event) => setRowBudget(Number(event.target.value))} /></label><label>Rows per batch<input type="number" min={1} max={10000} value={batchSize} onChange={(event) => setBatchSize(Number(event.target.value))} /></label></div>
        <label className="checkbox-line"><input type="checkbox" checked={allowCreate} onChange={(event) => setAllowCreate(event.target.checked)} />Allow new destination tables where the connector supports creation.</label>
        <label className={`confirmation-card ${frozen ? "checked" : ""}`}><input type="checkbox" checked={frozen} onChange={(event) => setFrozen(event.target.checked)} /><span><strong>My source is ready for a consistent snapshot</strong><small>Source writes are paused, or I am using an immutable snapshot. This confirmation does not pause the source database.</small></span><ShieldCheck size={24} /></label>
        <Notice>Creating the plan discovers and profiles the selected tables. Destination writes begin only after a permitted reviewer approves the exact plan.</Notice>
      </div></section>
      <div className="wizard-footer"><button className="button secondary" onClick={() => setStep(1)} disabled={busy}><ArrowLeft size={16} />Back to mapping</button><button className="button primary" onClick={submit} disabled={!frozen || busy || batchSize < 1 || batchSize > 10000 || rowBudget < 1 || rowBudget > 10000000}>{busy ? <LoaderCircle size={17} className="spin" /> : <Check size={17} />}{busy ? "Preparing plan…" : "Create reviewable plan"}</button></div>
    </>}
  </div>;
}

function ConnectionPicker({ label, value, onChange, connections, selected }: { label: string; value: string; onChange: (value: string) => void; connections: Connection[]; selected?: Connection }) {
  return <div className={`connection-picker ${selected ? "has-selection" : ""}`}><div className="connection-picker-title"><span>{label.startsWith("Source") ? "01" : "02"}</span><strong>{label.startsWith("Source") ? "Read from" : "Write to"}</strong><Database size={18} /></div><label className="sr-only" htmlFor={label.replaceAll(" ", "-")}>{label}</label><select id={label.replaceAll(" ", "-")} value={value} onChange={(event) => onChange(event.target.value)}><option value="">Select a connection</option>{connections.map((connection) => <option key={connection.id} value={connection.id}>{connection.label} · {kindName(connection.kind)}</option>)}</select><div className="connection-picker-detail">{selected ? <><DatabaseGlyph kind={selected.kind} /><span><strong>{kindName(selected.kind)}</strong><small>{selected.description || "Registered workspace connection"}</small></span></> : <><span className="empty-connection-icon"><Database size={20} /></span><p>Choose a registered database or file store.</p></>}</div></div>;
}

function QualityEditor({ mapping, onChange }: { mapping: TableMapping; onChange: (rules: QualityRule[]) => void }) {
  function patch(index: number, update: Partial<QualityRule>) {
    onChange(mapping.quality_rules.map((rule, position) => {
      if (position !== index) return rule;
      const next = { ...rule, ...update };
      if (mapping.keys.includes(next.column) || mapping.columns.find((column) => column.target === next.column)?.data_type !== "string") next.correction = "none";
      return next;
    }));
  }
  return <div className="quality-editor"><div className="subsection-heading"><div><SlidersHorizontal size={17} /><h3>Data quality checks</h3><span className="muted">Optional</span></div><button className="button ghost small" disabled={mapping.quality_rules.length >= 30} onClick={() => onChange([...mapping.quality_rules, { name: `check-${crypto.randomUUID().slice(0, 8)}`, column: mapping.columns[0].target, check: "not_null", correction: "none" }])}><Plus size={15} />Add check</button></div>
    {mapping.quality_rules.length ? <div className="quality-rules">{mapping.quality_rules.map((rule, index) => <div className="quality-rule" key={rule.name}><div className="quality-rule-main"><label>Column<select aria-label={`Quality column ${index + 1} for ${mapping.source}`} value={rule.column} onChange={(event) => patch(index, { column: event.target.value })}>{mapping.columns.map((column) => <option key={column.source} value={column.target}>{column.target}</option>)}</select></label><label>Check<select aria-label={`Quality check ${index + 1} for ${mapping.source}`} value={rule.check} onChange={(event) => patch(index, { check: event.target.value })}>{checks.map((check) => <option key={check} value={check}>{check.replaceAll("_", " ")}</option>)}</select></label><label>Approved correction<select aria-label={`Quality correction ${index + 1} for ${mapping.source}`} value={rule.correction} disabled={mapping.keys.includes(rule.column) || mapping.columns.find((column) => column.target === rule.column)?.data_type !== "string"} onChange={(event) => patch(index, { correction: event.target.value })}><option value="none">None</option><option value="trim">Trim whitespace</option><option value="lower">Lowercase</option><option value="upper">Uppercase</option></select></label><button className="icon-button danger-text" aria-label={`Remove quality check ${index + 1} from ${mapping.source}`} onClick={() => onChange(mapping.quality_rules.filter((_, position) => position !== index))}><Trash2 size={16} /></button></div>{rule.check === "regex" && <label className="rule-parameter">Regular expression<input value={rule.pattern || ""} maxLength={512} onChange={(event) => patch(index, { pattern: event.target.value })} placeholder="e.g. [A-Z]{3}-[0-9]+" /></label>}{rule.check === "range" && <div className="field-grid two rule-parameter"><label>Minimum<input inputMode="decimal" value={rule.minimum || ""} onChange={(event) => patch(index, { minimum: event.target.value || undefined })} /></label><label>Maximum<input inputMode="decimal" value={rule.maximum || ""} onChange={(event) => patch(index, { maximum: event.target.value || undefined })} /></label></div>}</div>)}</div> : <p className="quiet-note">Add checks to stop unexpected values before destination writes. Corrections become part of the reviewed plan.</p>}
  </div>;
}
