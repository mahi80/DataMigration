# 60-second product walkthrough

This is a caption-driven, 16:9 introduction to the migration workflow. The renderer
produces an **exactly 60-second MP4**, with 1,800 frames at 30 fps, H.264 video,
stereo AAC audio, and captions burned into the image. The default resolution is
1920 × 1080; a 1280 × 720 option is available.

The video shows how a team can coordinate discovery, mappings, exact-plan
approval, contextual Ask Q guidance, reconciliation and deployment planning. It
does not assert measured performance, production certification, autonomous
correction, or monitoring after the browser closes.

## Render

Install Python 3.11 or later, Pillow, NumPy, and FFmpeg with `libx264`, AAC and SVG
input support (`librsvg`). Install Open Sans or DejaVu Sans on the rendering host.
The renderer makes no network calls and does not require an API, database,
browser, model provider or voice service.

From the repository root:

```bash
python -m pip install Pillow numpy
python docs/video/render_walkthrough.py \
  --output /tmp/datamigration-video/datamigration-60s.mp4
```

For a lower-resolution export:

```bash
python docs/video/render_walkthrough.py \
  --height 720 \
  --output /tmp/datamigration-video/datamigration-60s.mp4
```

For a quick storyboard preview without encoding the MP4:

```bash
python docs/video/render_walkthrough.py \
  --preview-only \
  --output /tmp/datamigration-video/datamigration-60s.mp4
```

The script writes a contact sheet and an `ffprobe` verification JSON beside the
MP4. It regenerates [walkthrough.srt](walkthrough.srt) from the timed captions.
Video and audio exports are intentionally kept outside the source tree.

## Visual provenance and optional screenshots

The current export uses original motion graphics, with the relevant scenes
clearly labeled **“Workflow illustration · synthetic example.”** These illustrate
implemented workflows; they are not screenshots or a screen recording. The
mapping example uses the synthetic `customers` fixture and the `id → customer_id`,
`name → full_name` with `trim`, and `email → email` with `lower` mappings in
[`src/datamigration/demo.py`](../../src/datamigration/demo.py).

The animated Ask Q character is exported from the actual
[`AskQMascot` SVG component](../../frontend/src/components/ask-q-mascot.tsx). The
renderer reuses its shapes, colors and gradients, then creates a small series of
wave and blink poses with FFmpeg. `assets/ask-q.svg` is a readable source snapshot;
the PNG poses are reproducible ignored build products. The animation does not
change the product character into a different design.

To substitute authentic captures for the illustrated stages, save screenshots
under `docs/video/screenshots/` using these filenames and rerender:

| Filename | Scene | Capture guidance |
|---|---|---|
| `02-discovery-mapping.png` | 6–15 seconds | Migration wizard with synthetic source/target discovery or explicit mappings |
| `03-plan-approval.png` | 15–24 seconds | A pending synthetic plan and its exact-fingerprint review controls |
| `04-ask-q.png` | 24–33 seconds | The Ask Q panel in a permitted demo session |
| `05-reconciliation-lineage.png` | 33–42 seconds | The completed synthetic run's validation or lineage tab |

Prefer 1600 × 900 captures. The renderer preserves aspect ratio and does not
invent, retouch, or rewrite UI content. Supplied captures automatically receive
the label **“Actual console · synthetic local demo.”** Use the local fixture,
without real credentials, connection strings, private data or production names.
The opening and closing scenes remain title cards.

## Audio and captions

The score is an original, restrained instrumental synthesized in Python from
analytic oscillators. It uses an 80 BPM progression, soft bass pulses and sparse
bell tones. It contains no sampled music, voice clone, stock track or external
recording. The first and last seconds fade gently.

The complete meaning is available without audio. Caption timing and copy are in
[walkthrough.srt](walkthrough.srt); an accessible text version is in
[transcript.md](transcript.md). [storyboard.md](storyboard.md) connects each scene
to the actual implementation and its scope limits.

## Review before publishing

Inspect the contact sheet and play the exported file. The script requires
`ffprobe` to report a total duration of `60.000` seconds before it reports success.
The video stream should report 1920 × 1080 (or 1280 × 720), `30/1` fps and 1,800
frames. The external verification JSON records the codec and timing evidence.

Keep Ask Q text aligned with the shipped behavior: its read-only monitoring is
opt-in for the selected authorized run while the workspace is open. Chat proposes
actions; execution uses the existing role checks and confirmation controls.
Native cloud jobs currently use the CLI or Python adapters. AWS/Azure container
pilots and future managed-service architectures are documented paths, not a claim
that all services or environments have been live-tested.
