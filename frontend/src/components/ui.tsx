"use client";

import { AlertCircle, ArrowRight, Check, CircleDot, Database, FileText, Layers3, LoaderCircle, ShieldCheck } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { kindName } from "./types";

export function Brand({ compact = false }: { compact?: boolean }) {
  return <div className="brand"><span className="brand-symbol"><Layers3 size={22} strokeWidth={2.1} /></span><span>DataMigration{!compact && <small>THE MIGRATION WORKSPACE</small>}</span></div>;
}

export function StatusBadge({ status }: { status: string }) {
  const tone = status === "COMPLETED" || status === "VALIDATED" ? "success"
    : status === "AWAITING_APPROVAL" ? "warning"
    : status === "FAILED" || status === "ERROR" ? "danger"
    : status === "REJECTED" ? "neutral" : "info";
  const label = ({ AWAITING_APPROVAL: "Awaiting review", COMPLETED: "Completed", RUNNING: "Running", REJECTED: "Rejected", FAILED: "Failed", CREATED: "Created", VALIDATED: "Validated", APPROVED: "Approved" } as Record<string, string>)[status] || status.toLowerCase().replaceAll("_", " ");
  return <span className={`status-badge ${tone}`}><span className="status-dot" />{label}</span>;
}

export function Notice({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "danger" | "success" | "warning" }) {
  const Icon = tone === "success" ? ShieldCheck : AlertCircle;
  return <div className={`notice ${tone}`} role={tone === "danger" ? "alert" : "status"}><Icon size={18} aria-hidden="true" /><div>{children}</div></div>;
}

export function EmptyState({ icon: Icon = Database, title, description, action }: { icon?: LucideIcon; title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={28} /></span><h3>{title}</h3><p>{description}</p>{action}</div>;
}

export function Loading({ label = "Loading your workspace" }: { label?: string }) {
  return <div className="loading-state" role="status"><LoaderCircle size={23} className="spin" /><span>{label}…</span></div>;
}

export function Metric({ label, value, detail, icon: Icon, tone = "teal" }: { label: string; value: string | number; detail: string; icon: LucideIcon; tone?: string }) {
  return <div className="metric-card"><div className="metric-top"><span>{label}</span><span className={`metric-icon ${tone}`}><Icon size={18} /></span></div><strong>{value}</strong><p>{detail}</p></div>;
}

export function DatabaseGlyph({ kind, size = "normal" }: { kind: string; size?: "normal" | "small" }) {
  const file = ["csv", "jsonl", "parquet"].includes(kind);
  const Icon = file ? FileText : kind === "delta" || kind === "databricks" ? Layers3 : Database;
  return <span className={`database-glyph ${size} db-${kind}`}><Icon size={size === "small" ? 16 : 21} aria-hidden="true" /></span>;
}

export function RouteLabel({ source, target, compact = false }: { source: string; target: string; compact?: boolean }) {
  return <div className={`route-label ${compact ? "compact" : ""}`}><DatabaseGlyph kind={source} size="small" /><span>{kindName(source)}</span><ArrowRight size={14} className="muted" /><DatabaseGlyph kind={target} size="small" /><span>{kindName(target)}</span></div>;
}

export function StepIndicator({ steps, current }: { steps: string[]; current: number }) {
  return <ol className="step-indicator" aria-label="Migration setup progress">{steps.map((step, index) => <li key={step} className={index === current ? "current" : index < current ? "done" : ""}><span>{index < current ? <Check size={15} /> : index + 1}</span><strong>{step}</strong></li>)}</ol>;
}

export function Lifecycle({ status, hasPlan, passed }: { status: string; hasPlan: boolean; passed?: boolean }) {
  const done = status === "COMPLETED";
  const steps = [
    { name: "Discover & plan", ready: hasPlan, active: !hasPlan && status !== "FAILED" },
    { name: "Owner review", ready: hasPlan && ["APPROVED", "RUNNING", "VALIDATED", "COMPLETED"].includes(status), active: status === "AWAITING_APPROVAL" },
    { name: "Move data", ready: done, active: status === "RUNNING" && hasPlan },
    { name: "Reconcile", ready: passed === true, active: status === "VALIDATED" },
  ];
  return <div className="lifecycle">{steps.map((step) => <div key={step.name} className={step.ready ? "done" : step.active ? "active" : ""}><span>{step.ready ? <Check size={16} /> : <CircleDot size={16} />}</span><strong>{step.name}</strong></div>)}</div>;
}
