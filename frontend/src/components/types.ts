export interface Principal {
  subject: string;
  role: string;
  actions: string[];
}

export interface Connection {
  id: string;
  label: string;
  kind: string;
  url_env: string;
  options: Record<string, unknown>;
  description?: string;
  allowed_tables?: string[] | null;
}

export interface Column {
  name: string;
  data_type: string;
  nullable: boolean;
}

export interface TableSchema {
  name: string;
  columns: Column[];
  primary_key: string[];
  metadata?: {
    decimal_types?: Record<string, { precision: number; scale: number }>;
    timestamp_timezones?: Record<string, string | null>;
  };
}

export interface Discovery {
  table_names: string[];
  tables: TableSchema[];
  resource_exists?: boolean;
}

export interface ColumnMapping {
  source: string;
  target: string;
  data_type: string;
  nullable: boolean;
  transform: string;
  precision?: number | null;
  scale?: number | null;
  timezone?: string | null;
}

export interface QualityRule {
  name: string;
  column: string;
  check: string;
  correction: string;
  pattern?: string;
  minimum?: string;
  maximum?: string;
  values?: unknown[];
}

export interface TableMapping {
  source: string;
  target: string;
  keys: string[];
  columns: ColumnMapping[];
  depends_on: string[];
  quality_rules: QualityRule[];
}

export interface MigrationSpec {
  spec_version: 1;
  name: string;
  mode: "snapshot";
  workload: string;
  source: Pick<Connection, "kind" | "url_env" | "options">;
  target: Pick<Connection, "kind" | "url_env" | "options">;
  tables: TableMapping[];
  policy: {
    source_quiesced: boolean;
    batch_size: number;
    max_rows_per_table: number;
    target_scope: string;
    allow_create_tables: boolean;
  };
  advisor?: { provider: string };
}

export interface ValidationSummary {
  passed: boolean;
  tables: number;
  expected_rows: number;
  target_rows: number;
  missing_keys: number;
  extra_keys: number;
  duplicate_keys: number;
  mismatched_rows: number;
  corrected_values: number;
}

export interface RunSummary {
  run_id: string;
  name: string;
  status: string;
  source_kind: string;
  target_kind: string;
  source_connection: string;
  target_connection: string;
  table_count: number;
  plan_hash?: string | null;
  created_at?: string;
  updated_at?: string;
  validation?: ValidationSummary;
}

export interface RunList {
  runs: RunSummary[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface ValidationTable {
  table: string;
  passed: boolean;
  expected_rows: number;
  target_rows: number;
  missing_keys: number;
  extra_keys: number;
  duplicate_keys: number;
  mismatched_rows: number;
  scope: string;
  quality_rules?: Record<string, { column: string; checked: number; corrected: number; check: string }>;
  transformed_values_by_column?: Record<string, number>;
}

export interface RunDetail {
  run_id: string;
  status: string;
  plan_hash?: string;
  error?: string;
  next_nodes?: string[];
  created_at?: string;
  updated_at?: string;
  validation?: {
    passed: boolean;
    tables: ValidationTable[];
    plan_hash: string;
  };
}

export interface Plan {
  spec: MigrationSpec;
  plan_hash: string;
  contract: string;
  lineage?: Array<{
    source: string;
    target: string;
    columns: Array<{ source: string; target: string; transform: string }>;
    quality_rules: QualityRule[];
  }>;
}

export interface Health {
  status: string;
  draining: boolean;
  active_runs: number;
}

export interface ConnectorCapability {
  source: boolean;
  target: boolean;
  cdc: boolean;
  status: string;
  notes: string;
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/backend/${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("migration-session-expired"));
    let message = "The request could not be completed. Refresh the run status before trying again.";
    if (data && typeof data === "object") {
      const record = data as Record<string, unknown>;
      if (typeof record.detail === "string") message = record.detail;
      else if (typeof record.error === "string") message = record.error;
      else if (Array.isArray(record.detail)) {
        message = record.detail.map((item: { msg?: string }) => item.msg || "Invalid input").join(". ");
      }
    }
    throw new ApiError(message, response.status);
  }
  return data as T;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}

const names: Record<string, string> = {
  postgresql: "PostgreSQL", postgres: "PostgreSQL", sqlite: "SQLite", mysql: "MySQL",
  sqlserver: "SQL Server", mssql: "SQL Server", azure_sql: "Azure SQL", mongodb: "MongoDB",
  mongo: "MongoDB", redis: "Redis", cassandra: "Cassandra", redshift: "Redshift",
  databricks: "Databricks", delta: "Delta Lake", deltalake: "Delta Lake",
  csv: "CSV", jsonl: "JSONL", parquet: "Parquet",
};

export function kindName(kind: string): string {
  return names[kind] || kind || "Database";
}

export function dateLabel(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat().format(value);
}
