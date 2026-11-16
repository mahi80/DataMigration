export interface AskQPoint { x: number; y: number }
export interface AskQViewport { width: number; height: number }
export const ASK_Q_LAUNCHER_SIZE = { width: 104, height: 126 } as const;

export interface AskQPanelPlacement {
  placement: "above" | "below" | "left" | "right" | "overlay";
  style: { left: number; top: number; width: number; height: number };
}

/** Prefer a roomy panel beside the launcher to clamping it over the launcher. */
export function placeAskQPanel(anchor: AskQPoint, viewport: AskQViewport): AskQPanelPlacement {
  const margin = 12;
  const gap = 8;
  const width = Math.min(400, Math.max(0, viewport.width - margin * 2));
  const height = Math.min(650, Math.max(220, viewport.height - 166), Math.max(0, viewport.height - margin * 2));
  const clamp = (value: number, maximum: number) => Math.max(margin, Math.min(value, maximum));
  const left = clamp(anchor.x + ASK_Q_LAUNCHER_SIZE.width - width, viewport.width - width - margin);
  const sideTop = clamp(anchor.y + (ASK_Q_LAUNCHER_SIZE.height - height) / 2, viewport.height - height - margin);
  const above = anchor.y - gap - margin;
  const below = viewport.height - (anchor.y + ASK_Q_LAUNCHER_SIZE.height) - gap - margin;

  if (above >= height) return { placement: "above", style: { left, top: anchor.y - gap - height, width, height } };
  if (below >= height) return { placement: "below", style: { left, top: anchor.y + ASK_Q_LAUNCHER_SIZE.height + gap, width, height } };
  if (anchor.x - gap - margin >= width) return { placement: "left", style: { left: anchor.x - gap - width, top: sideTop, width, height } };
  if (viewport.width - anchor.x - ASK_Q_LAUNCHER_SIZE.width - gap - margin >= width) return { placement: "right", style: { left: anchor.x + ASK_Q_LAUNCHER_SIZE.width + gap, top: sideTop, width, height } };

  // On narrow screens, a shorter panel can still keep its composer and the
  // launcher separate. Keep enough height for the header, conversation and input.
  const available = Math.max(above, below);
  if (available >= 320) {
    return above >= below
      ? { placement: "above", style: { left, top: margin, width, height: Math.min(height, above) } }
      : { placement: "below", style: { left, top: anchor.y + ASK_Q_LAUNCHER_SIZE.height + gap, width, height: Math.min(height, below) } };
  }

  // Very short viewports cannot fit both controls separately. The panel is
  // layered above the launcher, so its composer and Close button remain usable.
  return { placement: "overlay", style: { left, top: sideTop, width, height } };
}
