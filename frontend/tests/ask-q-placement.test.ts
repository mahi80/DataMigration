import { describe, expect, it } from "vitest";
import { ASK_Q_LAUNCHER_SIZE, placeAskQPanel } from "../src/components/ask-q-placement";
import type { AskQPoint, AskQViewport } from "../src/components/ask-q-placement";

function expectContained(anchor: AskQPoint, viewport: AskQViewport) {
  const result = placeAskQPanel(anchor, viewport);
  const panel = result.style;
  expect(panel.left).toBeGreaterThanOrEqual(12);
  expect(panel.top).toBeGreaterThanOrEqual(12);
  expect(panel.left + panel.width).toBeLessThanOrEqual(viewport.width - 12);
  expect(panel.top + panel.height).toBeLessThanOrEqual(viewport.height - 12);
  if (result.placement !== "overlay") {
    const separated = panel.left + panel.width <= anchor.x - 8
      || panel.left >= anchor.x + ASK_Q_LAUNCHER_SIZE.width + 8
      || panel.top + panel.height <= anchor.y - 8
      || panel.top >= anchor.y + ASK_Q_LAUNCHER_SIZE.height + 8;
    expect(separated).toBe(true);
  }
  return result;
}

describe("Ask Q panel placement", () => {
  it("places the panel beside an upward-dragged launcher instead of over its composer", () => {
    const result = expectContained({ x: 944, y: 454 }, { width: 1280, height: 720 });
    expect(result.placement).toBe("left");
    expect(result.style).toMatchObject({ left: 536, width: 400, height: 554 });
  });

  it("keeps the initial 390 by 844 mobile panel above its launcher and inside the viewport", () => {
    const result = expectContained({ x: 262, y: 700 }, { width: 390, height: 844 });
    expect(result).toEqual({ placement: "above", style: { left: 12, top: 42, width: 366, height: 650 } });
  });

  it("uses free space below a top-edge launcher", () => {
    expect(expectContained({ x: 1100, y: 12 }, { width: 1280, height: 800 }).placement).toBe("below");
  });

  it("keeps both controls separate throughout ordinary desktop and mobile drag positions", () => {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
      for (const horizontal of [0, .25, .5, .75, 1]) {
        for (const vertical of [0, .25, .5, .75, 1]) {
          const anchor = {
            x: 12 + horizontal * (viewport.width - ASK_Q_LAUNCHER_SIZE.width - 24),
            y: 12 + vertical * (viewport.height - ASK_Q_LAUNCHER_SIZE.height - 24),
          };
          expect(expectContained(anchor, viewport).placement).not.toBe("overlay");
        }
      }
    }
  });

  it("uses a bounded foreground panel when a short viewport cannot fit both controls", () => {
    expect(expectContained({ x: 140, y: 100 }, { width: 390, height: 360 }).placement).toBe("overlay");
  });
});
