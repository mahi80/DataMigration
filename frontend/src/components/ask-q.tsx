"use client";

import { ArrowDownLeft, ArrowRight, Check, ChevronDown, CircleHelp, Grip, LoaderCircle, MessageCircle, Move, Pause, Play, Send, ShieldCheck, Sparkles, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, PointerEvent } from "react";
import { ASSISTANT_EVENT } from "./assistant-events";
import type { AssistantInteraction } from "./assistant-events";
import { AskQMascot } from "./ask-q-mascot";
import { ASK_Q_LAUNCHER_SIZE as launcherSize, placeAskQPanel } from "./ask-q-placement";
import type { AskQPoint as Point, AskQViewport as Viewport } from "./ask-q-placement";
import { api, errorMessage } from "./types";
import type { Principal, RunDetail } from "./types";
import { StatusBadge } from "./ui";
import "./ask-q.css";

export type AssistantView = "overview" | "migrations" | "connections" | "new" | "run";
export type RunFocus = "review" | "retry";
interface Suggestion {
  id: string;
  label: string;
  description: string;
  action: "navigate" | "new_plan" | "open_run" | "review_plan" | "retry_run";
  view?: AssistantView;
  run_id?: string;
  confirmation_required: boolean;
}
interface AssistantResponse {
  reply: string;
  mode: "guided" | "model";
  context: { view: AssistantView; subject: string; role: string; observed_at?: string; run?: { run_id: string; status: string } };
  suggestions: Suggestion[];
  graph_steps: string[];
  sources?: Array<{ title: string; path: string }>;
}
interface Message {
  id: string;
  role: "user" | "assistant" | "status";
  text: string;
  mode?: "guided" | "model";
  suggestions?: Suggestion[];
  sources?: AssistantResponse["sources"];
  view?: AssistantView;
}
const POSITION_KEY = "datamigration.ask-q.position.v1";
const views: AssistantView[] = ["overview", "migrations", "connections", "new", "run"];
const viewNames: Record<AssistantView, string> = { overview: "Workspace overview", migrations: "Migration history", connections: "Connections", new: "Migration setup", run: "Selected migration" };
const quickPrompts = [
  { label: "Guide my setup", message: "Guide me through setting up a new migration, step by step." },
  { label: "Explain this run", message: "Explain the current migration status and what I should review next." },
  { label: "Help fix an issue", message: "Help me understand migration issues and propose safe next steps for review." },
  { label: "Compare databases", message: "Which databases and file formats can this tool migrate, and how should I choose a destination?" },
];

function boundedPosition(point: Point, viewport: Viewport): Point {
  return { x: Math.max(12, Math.min(point.x, viewport.width - launcherSize.width - 12)), y: Math.max(12, Math.min(point.y, viewport.height - launcherSize.height - 12)) };
}
function savePosition(point: Point) {
  try { window.localStorage.setItem(POSITION_KEY, JSON.stringify(point)); } catch { /* Position persistence is optional. */ }
}
function statusName(status: string) { return status.toLowerCase().replaceAll("_", " "); }
function sourceHref(path: string): string | undefined {
  return /^(?:docs\/|README\.md$)[a-zA-Z0-9_./#-]*$/.test(path) && !path.includes("..") ? `https://github.com/mahi80/DataMigration/blob/main/${path}` : undefined;
}

export function AskQ({ principal, view, runId, onNavigate, onRunStatusChange }: {
  principal: Principal;
  view: AssistantView;
  runId?: string | null;
  onNavigate: (view: AssistantView, runId?: string, focus?: RunFocus) => void;
  onRunStatusChange: (runId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [point, setPoint] = useState<Point | null>(null);
  const [viewport, setViewport] = useState<Viewport>({ width: 1280, height: 800 });
  const [dragging, setDragging] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<"guided" | "model">("guided");
  const [contextOpen, setContextOpen] = useState(false);
  const [contextCount, setContextCount] = useState(0);
  const [positionAnnouncement, setPositionAnnouncement] = useState("");
  const [monitoring, setMonitoring] = useState<{ runId?: string | null; enabled: boolean }>({ enabled: false });
  const [statusUpdateRun, setStatusUpdateRun] = useState<string | null>(null);
  const hasStatusUpdate = Boolean(runId && statusUpdateRun === runId);
  const launcher = useRef<HTMLButtonElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const conversationEnd = useRef<HTMLDivElement>(null);
  const interactions = useRef<AssistantInteraction[]>([]);
  const request = useRef<AbortController | null>(null);
  const requestBusy = useRef(false);
  const drag = useRef<{ pointerId: number; start: Point; origin: Point; current: Point; moved: boolean } | null>(null);
  const suppressClick = useRef(false);

  useEffect(() => {
    function resize() {
      const current = { width: window.innerWidth, height: window.innerHeight };
      setViewport(current);
      setPoint((previous) => boundedPosition(previous || { x: current.width - launcherSize.width - 24, y: current.height - launcherSize.height - 18 }, current));
    }
    const frame = window.requestAnimationFrame(() => {
      const current = { width: window.innerWidth, height: window.innerHeight };
      let saved: Point | null = null;
      try {
        const candidate: unknown = JSON.parse(window.localStorage.getItem(POSITION_KEY) || "null");
        if (candidate && typeof candidate === "object" && "x" in candidate && "y" in candidate && typeof candidate.x === "number" && typeof candidate.y === "number" && Number.isFinite(candidate.x) && Number.isFinite(candidate.y)) saved = { x: candidate.x, y: candidate.y };
      } catch { /* Fall back to the default corner. */ }
      setViewport(current);
      setPoint(boundedPosition(saved || { x: current.width - launcherSize.width - 24, y: current.height - launcherSize.height - 18 }, current));
    });
    function record(event: Event) {
      const detail = (event as CustomEvent<AssistantInteraction>).detail;
      if (!detail || !["navigation", "discovery", "plan_created", "approval_requested", "retry_requested", "request_failed", "validation_opened", "mapping_edited"].includes(detail.event)) return;
      const entry: AssistantInteraction = { event: detail.event, ...(["success", "failure", "pending"].includes(detail.outcome || "") ? { outcome: detail.outcome } : {}) };
      const last = interactions.current.at(-1);
      if (last?.event === entry.event && last?.outcome === entry.outcome) return;
      interactions.current = [...interactions.current, entry].slice(-8);
      setContextCount(interactions.current.length);
    }
    window.addEventListener("resize", resize);
    window.addEventListener(ASSISTANT_EVENT, record);
    return () => { window.cancelAnimationFrame(frame); window.removeEventListener("resize", resize); window.removeEventListener(ASSISTANT_EVENT, record); request.current?.abort(); request.current = null; };
  }, []);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => textarea.current?.focus({ preventScroll: true }));
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  useEffect(() => {
    if (open) conversationEnd.current?.scrollIntoView({ block: "nearest", behavior: "instant" });
  }, [messages.length, busy, open]);

  function closePanel() { setOpen(false); launcher.current?.focus({ preventScroll: true }); }
  function moveToOtherSide() {
    if (!point) return;
    const next = boundedPosition({ x: point.x > viewport.width / 2 ? 20 : viewport.width - launcherSize.width - 24, y: point.y }, viewport);
    setPoint(next); savePosition(next); setPositionAnnouncement("Ask Q moved to the other side of the screen.");
  }

  function startDrag(event: PointerEvent<HTMLButtonElement>) {
    if (event.button !== 0 || !point) return;
    drag.current = { pointerId: event.pointerId, start: { x: event.clientX, y: event.clientY }, origin: point, current: point, moved: false };
    suppressClick.current = false;
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function moveDrag(event: PointerEvent<HTMLButtonElement>) {
    const active = drag.current;
    if (!active || event.pointerId !== active.pointerId) return;
    const dx = event.clientX - active.start.x, dy = event.clientY - active.start.y;
    if (!active.moved && Math.hypot(dx, dy) < 5) return;
    active.moved = true;
    active.current = boundedPosition({ x: active.origin.x + dx, y: active.origin.y + dy }, viewport);
    setDragging(true); setPoint(active.current);
  }
  function endDrag(event: PointerEvent<HTMLButtonElement>) {
    const active = drag.current;
    if (!active || event.pointerId !== active.pointerId) return;
    suppressClick.current = active.moved;
    if (active.moved) savePosition(active.current);
    drag.current = null; setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  function moveWithKeyboard(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Escape") { closePanel(); return; }
    if (!point || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home"].includes(event.key)) return;
    event.preventDefault();
    const distance = event.shiftKey ? 40 : 16;
    const next = event.key === "Home" ? { x: viewport.width - launcherSize.width - 24, y: viewport.height - launcherSize.height - 18 } : { x: point.x + (event.key === "ArrowRight" ? distance : event.key === "ArrowLeft" ? -distance : 0), y: point.y + (event.key === "ArrowDown" ? distance : event.key === "ArrowUp" ? -distance : 0) };
    const bounded = boundedPosition(next, viewport);
    setPoint(bounded); savePosition(bounded); setPositionAnnouncement(event.key === "Home" ? "Ask Q returned to the lower right." : `Ask Q moved ${event.key.replace("Arrow", "").toLowerCase()}.`);
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || message.length > 2000 || requestBusy.current) return;
    const controller = new AbortController();
    request.current = controller; requestBusy.current = true;
    const requestView = view;
    setDraft(""); setError(""); setBusy(true);
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: "user", text: message } satisfies Message].slice(-40));
    try {
      const result = await api<AssistantResponse>("assistant/chat", { method: "POST", signal: controller.signal, body: JSON.stringify({ message, view: requestView, ...(runId ? { run_id: runId } : {}), interactions: interactions.current.slice(-8) }) });
      if (controller.signal.aborted) return;
      setMode(result.mode);
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: "assistant", text: result.reply, mode: result.mode, suggestions: result.suggestions, sources: result.sources, view: requestView } satisfies Message].slice(-40));
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (request.current === controller) { requestBusy.current = false; setBusy(false); request.current = null; }
    }
  }
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); void send(draft); }
  function clearConversation() {
    request.current?.abort(); request.current = null; requestBusy.current = false;
    setMessages([]); setBusy(false); setDraft(""); setError(""); setMode("guided");
    interactions.current = []; setContextCount(0);
    textarea.current?.focus();
  }
  function applySuggestion(suggestion: Suggestion) {
    if (suggestion.action === "new_plan") {
      if (!principal.actions.includes("plan")) return;
      setOpen(false); onNavigate("new"); return;
    }
    if (["open_run", "review_plan", "retry_run"].includes(suggestion.action)) {
      if (!suggestion.run_id || !/^[A-Za-z0-9_-]{1,64}$/.test(suggestion.run_id)) return;
      if (suggestion.action === "review_plan" && !principal.actions.includes("read_plan")) return;
      if (suggestion.action === "retry_run" && !principal.actions.includes("retry")) return;
      setOpen(false); onNavigate("run", suggestion.run_id, suggestion.action === "review_plan" ? "review" : suggestion.action === "retry_run" ? "retry" : undefined); return;
    }
    if (suggestion.action === "navigate" && suggestion.view && views.includes(suggestion.view)) {
      if (suggestion.view === "new" && !principal.actions.includes("plan")) return;
      if (suggestion.view === "connections" && !principal.actions.includes("discover")) return;
      if (suggestion.view === "run" && !suggestion.run_id) return;
      setOpen(false); onNavigate(suggestion.view, suggestion.run_id);
    }
  }
  const notifyStatus = useCallback((previous: string, current: string) => {
    setMessages((messages) => [...messages, { id: crypto.randomUUID(), role: "status", text: `The selected run changed from ${statusName(previous)} to ${statusName(current)}.` } satisfies Message].slice(-40));
    if (!open) setStatusUpdateRun(runId || null);
    if (runId) onRunStatusChange(runId);
  }, [open, runId, onRunStatusChange]);
  const monitorChanged = useCallback((id: string | null | undefined, enabled: boolean) => setMonitoring({ runId: id, enabled }), []);

  const anchor = point || { x: viewport.width - launcherSize.width - 24, y: viewport.height - launcherSize.height - 18 };
  const panel = placeAskQPanel(anchor, viewport);

  return <div className="ask-q-root">
    <p id="ask-q-move-help" className="sr-only">Drag Ask Q to move. With the button focused, use arrow keys to move, Shift for larger steps, Home to reset, or Enter to open.</p>
    <span className="sr-only" role="status">{positionAnnouncement}</span>
    <span className="sr-only" role="status">{hasStatusUpdate && !open ? "The monitored run changed status. Open Ask Q to review the update." : ""}</span>
    <section className="ask-q-panel" hidden={!open} style={panel.style} data-placement={panel.placement} role="dialog" aria-label="Ask Q assistant" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closePanel(); } }}>
      <header className="ask-q-header"><div className="ask-q-header-avatar"><AskQMascot thinking={busy} /></div><div className="ask-q-title"><h2>Ask Q<span className="ask-q-online-dot" /></h2><span>Your migration companion</span></div><button className="icon-button" aria-label="Clear Ask Q conversation" title="Clear conversation" onClick={clearConversation}><Trash2 size={15} /></button><button className="icon-button" aria-label="Close Ask Q" onClick={closePanel}><X size={19} /></button></header>
      <div className="ask-q-context"><button onClick={() => setContextOpen(!contextOpen)} aria-expanded={contextOpen}><span><span className="ask-q-context-dot" />{viewNames[view]}</span><ChevronDown size={13} className={contextOpen ? "flipped" : ""} /></button><span className="ask-q-mode">{mode === "model" ? <Sparkles size={11} /> : <GitBranchIcon />}{mode === "model" ? "Model assisted" : "Guided mode"}</span></div>
      {contextOpen && <div className="ask-q-context-details"><strong>Context you control</strong><p>Q receives the current screen name, your role, the selected run reference, and up to eight recent action names. Current buffer: {contextCount}.</p><p>Form values, credentials, raw rows, and page content are excluded. Your question goes to the workspace assistant service. Conversation history stays in memory; only Q’s position is saved in browser storage.</p></div>}
      <div className="ask-q-conversation" role="log" aria-label="Ask Q conversation" aria-live="polite" aria-relevant="additions">
        <div className="ask-q-welcome"><span className="ask-q-greeting">A little guidance. A clearer path.</span><h3>Hi, I’m Q <span aria-hidden="true">✦</span></h3><p>I can guide setup, explain migration evidence, and propose a next step for you to review.</p><div className="ask-q-quick-prompts">{quickPrompts.map((prompt) => <button key={prompt.label} onClick={() => void send(prompt.message)} disabled={busy || (prompt.label === "Explain this run" && !runId)}>{prompt.label}<ArrowDownLeft size={12} /></button>)}</div></div>
        {messages.map((message) => <div className={`ask-q-message ${message.role}`} key={message.id}>
          {message.role === "status" ? <div className="ask-q-status-message"><Check size={14} /><span>{message.text}</span></div> : <><span className="ask-q-message-author">{message.role === "user" ? "You" : "Ask Q"}{message.mode && <small>{message.mode === "model" ? "Model assisted" : "Guided response"}</small>}</span><div className="ask-q-message-body">{message.text}</div>{message.role === "assistant" && message.view && <span className="ask-q-message-context">About {viewNames[message.view].toLowerCase()}</span>}{Boolean(message.suggestions?.length) && <div className="ask-q-suggestions">{message.suggestions?.map((suggestion) => <button key={suggestion.id} onClick={() => applySuggestion(suggestion)}><span><strong>{suggestion.label}</strong><small>{suggestion.description}</small>{suggestion.confirmation_required && <em><ShieldCheck size={10} />Review & confirmation required</em>}</span><ArrowRight size={15} /></button>)}</div>}{Boolean(message.sources?.length) && <div className="ask-q-sources">{message.sources?.filter((source) => sourceHref(source.path)).map((source) => <a key={source.path} href={sourceHref(source.path)} target="_blank" rel="noreferrer">{source.title}<ArrowRight size={10} /></a>)}</div>}</>}
        </div>)}
        {busy && <div className="ask-q-thinking" role="status"><span /><span /><span /><small>Q is checking the context…</small></div>}
        {error && <div className="ask-q-error" role="alert"><CircleHelp size={16} /><span>{error}</span></div>}
        <div ref={conversationEnd} />
      </div>
      <RunMonitor key={runId || "no-run"} runId={runId} onStatusChange={notifyStatus} onMonitoringChange={monitorChanged} onHelp={() => void send("Explain the current run status and propose safe next steps for review.")} busy={busy} />
      <form className="ask-q-composer" onSubmit={submit}><label className="sr-only" htmlFor="ask-q-question">Ask Q a question</label><div className="ask-q-input-row"><textarea ref={textarea} id="ask-q-question" rows={2} maxLength={2000} placeholder="Ask about your migration…" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(draft); } }} /><button type="submit" aria-label="Send question to Ask Q" disabled={busy || !draft.trim()}>{busy ? <LoaderCircle size={17} className="spin" /> : <Send size={17} />}</button></div><div className="ask-q-composer-note"><span><ShieldCheck size={11} />You decide what runs.</span><span>{draft.length > 1800 ? `${draft.length}/2000` : "Shift + Enter for a new line"}</span></div></form>
    </section>
    <div className={`ask-q-launcher-wrap ${dragging ? "dragging" : ""} ${open ? "expanded" : ""}`} style={point ? { left: point.x, top: point.y } : { right: 24, bottom: 18 }}><button ref={launcher} className="ask-q-launcher" aria-label={open ? "Minimize Ask Q assistant" : "Open Ask Q assistant"} aria-expanded={open} aria-describedby="ask-q-move-help" onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={endDrag} onPointerCancel={endDrag} onLostPointerCapture={() => { drag.current = null; setDragging(false); }} onKeyDown={moveWithKeyboard} onClick={() => { if (suppressClick.current) { suppressClick.current = false; return; } if (!open) setStatusUpdateRun(null); setOpen(!open); }}><span className="ask-q-orbit" /><AskQMascot thinking={busy} /><span className="ask-q-launcher-label"><MessageCircle size={12} />Ask Q<Grip size={12} /></span>{((monitoring.enabled && monitoring.runId === runId) || hasStatusUpdate) && <span className={`ask-q-watch-badge ${hasStatusUpdate ? "changed" : ""}`} title={hasStatusUpdate ? "Run status changed" : "Monitoring selected run"} aria-label={hasStatusUpdate ? "Run status changed" : "Monitoring selected run"}>{hasStatusUpdate ? "!" : <span />}</span>}<span className="ask-q-drag-hint"><Move size={10} />Drag to move</span></button><button className="ask-q-shift" aria-label="Move Ask Q to opposite side" title="Move Q to the other side" onClick={moveToOtherSide}><Move size={12} /></button></div>
  </div>;
}

function GitBranchIcon() { return <span className="ask-q-guided-dot" aria-hidden="true" />; }

function RunMonitor({ runId, onStatusChange, onMonitoringChange, onHelp, busy }: { runId?: string | null; onStatusChange: (previous: string, current: string) => void; onMonitoringChange: (runId: string | null | undefined, enabled: boolean) => void; onHelp: () => void; busy: boolean }) {
  const [enabled, setEnabled] = useState(false);
  const [visible, setVisible] = useState(true);
  const [status, setStatus] = useState<string>();
  const [checkedAt, setCheckedAt] = useState<string>();
  const [error, setError] = useState("");
  const lastStatus = useRef<string | undefined>(undefined);
  useEffect(() => {
    function visibility() { setVisible(document.visibilityState === "visible"); }
    document.addEventListener("visibilitychange", visibility);
    return () => document.removeEventListener("visibilitychange", visibility);
  }, []);
  useEffect(() => () => onMonitoringChange(runId, false), [runId, onMonitoringChange]);
  useEffect(() => {
    if (!runId || !enabled || !visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      let continuePolling = true;
      try {
        const next = await api<RunDetail>(`runs/${encodeURIComponent(runId!)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (lastStatus.current && lastStatus.current !== next.status) onStatusChange(lastStatus.current, next.status);
        lastStatus.current = next.status;
        setStatus(next.status); setCheckedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })); setError("");
        if (["COMPLETED", "REJECTED"].includes(next.status)) { setEnabled(false); onMonitoringChange(runId, false); continuePolling = false; }
      } catch {
        if (!controller.signal.aborted) { setError("Status updates stopped. Refresh the run to check your access and connection."); setEnabled(false); onMonitoringChange(runId, false); }
        continuePolling = false;
      }
      if (!controller.signal.aborted && continuePolling) timer = setTimeout(() => void poll(), 8000);
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, enabled, visible, onStatusChange, onMonitoringChange]);

  if (!runId) return <div className="ask-q-monitor empty"><span><Play size={12} />Open a migration to enable status monitoring.</span></div>;
  return <div className={`ask-q-monitor ${enabled ? "enabled" : ""}`}><div className="ask-q-monitor-heading"><span><span className={`ask-q-monitor-dot ${enabled ? "active" : ""}`} />{enabled ? visible ? "Watching this run" : "Updates paused" : status && ["COMPLETED", "REJECTED"].includes(status) ? "Run finished" : "Monitor this run"}</span><button aria-label={enabled ? "Stop monitoring selected run" : "Start monitoring selected run"} onClick={() => { setError(""); setEnabled(!enabled); onMonitoringChange(runId, !enabled); }}>{enabled ? <Pause size={11} /> : <Play size={11} />}{enabled ? "Stop" : "Start"}</button></div>{status && <div className="ask-q-monitor-result"><StatusBadge status={status} /><button className="text-link" disabled={busy} onClick={onHelp}>Ask Q about this<ArrowRight size={11} /></button></div>}<small>{error || (enabled ? `Read-only checks every 8s, including while minimized.${checkedAt ? ` Checked ${checkedAt}.` : ""}` : "Continues when minimized. Pauses when this browser tab is hidden.")}</small></div>;
}
