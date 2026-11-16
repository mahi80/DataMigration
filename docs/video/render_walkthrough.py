#!/usr/bin/env python3
"""Render an original 60-second DataMigration walkthrough using local assets.

Requirements: Python 3.11+, Pillow, NumPy, FFmpeg with libx264 and AAC.
No network access, external music, voice service, or generated product screenshots.
See README.md in this directory for capture inputs and factual scope.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
W, H = 1920, 1080
FPS, DURATION = 30, 60
INK = "#182c2a"
MUTED = "#647b73"
TEAL = "#087f69"
DARK = "#0b4638"
PALE = "#e8f5ef"
BORDER = "#d8e5df"
WHITE = "#ffffff"
GOLD = "#bf9555"

SCENES = [
    (0, 6, "Coordinate", "A clear plan.\nA shared workspace."),
    (6, 15, "Discover & map", "Discover. Map. Make it explicit."),
    (15, 24, "Review & approve", "Approve the exact plan."),
    (24, 33, "Ask Q", "A little guidance. In context."),
    (33, 42, "Validate & trace", "Reconcile. Explain. Trace."),
    (42, 51, "Deployment paths", "Your cloud. A reviewed execution path."),
    (51, 60, "Start building", "Migration, with a plan."),
]

CAPTIONS = [
    (0, 3, "Migration is a team effort."),
    (3, 6, "Bring plans, approvals and evidence into one workspace."),
    (6, 10.5, "Discover registered sources and targets."),
    (10.5, 15, "Choose tables, map columns and define quality rules."),
    (15, 19.5, "An authorized reviewer inspects the exact plan."),
    (19.5, 24, "Execution follows approval of that plan fingerprint."),
    (24, 28.5, "Ask Q guides setup and follows the selected, authorized run."),
    (28.5, 33, "It proposes next steps. You confirm permitted actions."),
    (33, 37.5, "Compare mapped data and inspect recorded quality outcomes."),
    (37.5, 42, "Keep reconciliation scope, corrections and lineage visible."),
    (42, 46.5, "Follow reference architectures for AWS, Azure and Databricks."),
    (46.5, 51, "Native cloud jobs use CLI or Python and need environment qualification."),
    (51, 55.5, "Explore the framework and run the synthetic local demo."),
    (55.5, 60, "github.com/mahi80/DataMigration"),
]

SCREENSHOTS = {
    0: "01-overview.png",
    1: "02-discovery-mapping.png",
    2: "03-plan-approval.png",
    3: "04-ask-q.png",
    4: "05-reconciliation-lineage.png",
}


def ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return 1 - (1 - value) ** 3


def font_path(bold: bool = False, mono: bool = False) -> str:
    names = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
        if mono else
        [f"/usr/share/fonts/truetype/open-sans/OpenSans-{'Semibold' if bold else 'Regular'}.ttf",
         f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
         "/System/Library/Fonts/Helvetica.ttc"]
    )
    for name in names:
        if Path(name).exists():
            return name
    raise RuntimeError("Install Open Sans or DejaVu Sans fonts, or adapt font_path().")


@lru_cache(maxsize=96)
def font(size: int, bold: bool = False, mono: bool = False):
    return ImageFont.truetype(font_path(bold, mono), size)


def text(draw, xy, value, size=26, fill=INK, bold=False, mono=False, anchor="lt"):
    draw.text(xy, value, font=font(size, bold, mono), fill=fill, anchor=anchor)


def lines(draw, xy, value, size=26, fill=INK, bold=False, gap=1.35):
    for n, line in enumerate(value.split("\n")):
        text(draw, (xy[0], xy[1] + n * size * gap), line, size, fill, bold)


def rr(draw, box, fill=WHITE, outline=BORDER, radius=22, width=2):
    draw.rounded_rectangle(tuple(round(v) for v in box), radius=radius, fill=fill,
                           outline=outline, width=width)


def pill(draw, xy, label, fill=PALE, color=TEAL, size=20):
    width = draw.textlength(label, font=font(size, True)) + 34
    rr(draw, (xy[0], xy[1], xy[0] + width, xy[1] + size + 24), fill, None, 12)
    text(draw, (xy[0] + 17, xy[1] + 11), label, size, color, True)
    return width


def check(draw, x, y, size=24, color=TEAL):
    draw.line([(x, y + size * .5), (x + size * .34, y + size * .82),
               (x + size, y)], fill=color, width=max(2, int(size / 8)), joint="curve")


def arrow(draw, start, end, color=TEAL, width=3, tip=12):
    draw.line([start, end], fill=color, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    points = [end]
    for delta in (-.55, .55):
        points.append((end[0] - tip * math.cos(angle + delta),
                       end[1] - tip * math.sin(angle + delta)))
    draw.polygon(points, fill=color)


def database(draw, x, y, size=60, color=TEAL):
    width, height = size, size * .76
    draw.rectangle((x, y + height * .2, x + width, y + height * .85), fill=WHITE)
    for offset in (0.15, 0.5, 0.85):
        draw.arc((x, y + height * (offset - .2), x + width,
                  y + height * (offset + .2)), 0, 180, fill=color, width=3)
    draw.ellipse((x, y, x + width, y + height * .4), outline=color, width=3)
    draw.line((x, y + height * .2, x, y + height * .85), fill=color, width=3)
    draw.line((x + width, y + height * .2, x + width, y + height * .85), fill=color, width=3)


def logo(draw, x, y, size=52, dark=False):
    rr(draw, (x, y, x + size, y + size), "#d0efdd" if dark else TEAL, None, 15)
    color = DARK if dark else WHITE
    for n, width in enumerate((19, 27, 19)):
        x0 = x + 12 if n != 1 else x + 17
        draw.line((x0, y + 15 + n * 11, x0 + width, y + 15 + n * 11), fill=color, width=4)
        draw.ellipse((x0 - 2, y + 13 + n * 11, x0 + 2, y + 17 + n * 11), fill=color)


@lru_cache(maxsize=2)
def background(dark=False):
    yy, xx = np.mgrid[0:H, 0:W]
    a = np.array([11, 70, 56] if dark else [247, 250, 247])
    b = np.array([19, 85, 66] if dark else [225, 240, 232])
    radial = np.clip(1 - np.sqrt(((xx - 1600) / 1400) ** 2 + ((yy - 280) / 1100) ** 2), 0, 1)
    pixels = (a[None, None, :] * (1 - radial[..., None] * .64)
              + b[None, None, :] * radial[..., None] * .64).astype(np.uint8)
    image = Image.fromarray(pixels, "RGB")
    draw = ImageDraw.Draw(image)
    color = "#2b6954" if dark else "#dfeae3"
    for cx, cy, r in ((1760, 100, 600), (1815, 130, 700), (-130, 1110, 390)):
        draw.ellipse((cx-r, cy-r, cx+r, cy+r), outline=color, width=1)
    return image


def header(draw, chapter, dark=False):
    logo(draw, 96, 55, dark=dark)
    text(draw, (163, 65), "DataMigration", 29, WHITE if dark else INK, True)
    text(draw, (1824, 69), chapter.upper(), 18, "#a3c6b5" if dark else MUTED, True, anchor="rt")


def chapter_title(draw, title, subline):
    text(draw, (96, 157), title, 58, INK, True)
    text(draw, (99, 243), subline, 26, MUTED)


def card(draw, box, label=None):
    x0, y0, x1, y1 = box
    rr(draw, (x0, y0 + 6, x1, y1 + 6), "#dbe8df", None, 25)
    rr(draw, box, WHITE, BORDER, 25)
    if label:
        text(draw, (x0 + 28, y0 + 25), label, 17, MUTED, True)


@lru_cache(maxsize=12)
def screenshot_image(path, max_width, max_height):
    original = Image.open(path).convert("RGB")
    original.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return original


def screenshot_stage(image, scene):
    path = HERE / "screenshots" / SCREENSHOTS.get(scene, "unavailable")
    if not path.is_file():
        return False
    draw = ImageDraw.Draw(image)
    card(draw, (96, 324, 1390, 900))
    text(draw, (123, 346), "ACTUAL CONSOLE · SYNTHETIC LOCAL DEMO", 16, MUTED, True)
    screenshot = screenshot_image(str(path), 1246, 502)
    image.paste(screenshot, (120 + (1246 - screenshot.width) // 2,
                             385 + (502 - screenshot.height) // 2))
    return True


def notes(draw, items, start=384):
    for i, (title, body) in enumerate(items):
        y = start + i * 165
        draw.ellipse((1450, y + 5, 1484, y + 39), fill=PALE)
        check(draw, 1459, y + 14, 16)
        text(draw, (1503, y + 4), title, 27, INK, True)
        lines(draw, (1503, y + 51), body, 23, MUTED, gap=1.4)


def illustration_label(draw):
    text(draw, (124, 341), "WORKFLOW ILLUSTRATION · SYNTHETIC EXAMPLE", 16, MUTED, True)


def mapping_panel(image, t):
    draw = ImageDraw.Draw(image)
    card(draw, (96, 318, 1390, 900))
    illustration_label(draw)
    database(draw, 139, 404, 49)
    text(draw, (212, 407), "customers", 30, INK, True)
    text(draw, (212, 449), "Registered source", 20, MUTED)
    database(draw, 836, 404, 49)
    text(draw, (909, 407), "customers", 30, INK, True)
    text(draw, (909, 449), "Registered target", 20, MUTED)
    for i, (source, transform, target) in enumerate((
            ("id", "identity", "customer_id"), ("name", "trim", "full_name"),
            ("email", "lower", "email"))):
        y = 526 + i * 91
        rr(draw, (133, y, 490, y + 65), "#f5f8f6", BORDER, 12)
        text(draw, (157, y + 20), source, 26, INK, mono=True)
        rr(draw, (821, y, 1352, y + 65), "#f0f8f3", BORDER, 12)
        text(draw, (849, y + 20), target, 26, INK, mono=True)
        arrow(draw, (515, y + 32), (798, y + 32), "#92baa9", 3)
        width = draw.textlength(transform, font=font(20, True)) + 30
        rr(draw, (656-width/2, y+12, 656+width/2, y+52), WHITE, BORDER, 10)
        text(draw, (656, y + 22), transform, 20, TEAL, True, anchor="mt")
        phase = (t * .15 + i * .28) % 1
        px = 518 + phase * 272
        draw.ellipse((px-5, y+27, px+5, y+37), fill=TEAL)
    pill(draw, (135, 827), "Key: customer_id", size=19)
    pill(draw, (392, 827), "Source stability confirmed", size=19)


def approval_panel(image, t):
    draw = ImageDraw.Draw(image)
    card(draw, (96, 318, 1390, 900))
    illustration_label(draw)
    rr(draw, (135, 397, 1351, 533), "#f0f7f3", BORDER, 18)
    text(draw, (165, 421), "Exact migration plan", 30, INK, True)
    text(draw, (165, 475), "Source · target · mappings · keys · quality rules", 24, MUTED)
    pill(draw, (1050, 440), "Fingerprint", size=20)
    steps = [("01", "Planner", "Creates the plan"), ("02", "Approver", "Reviews this version"),
             ("03", "Worker", "Runs approved scope")]
    for i, (number, label, body) in enumerate(steps):
        x = 136 + i * 414
        rr(draw, (x, 590, x + 385, 765), PALE if i == 1 else "#f7faf8", BORDER, 17)
        text(draw, (x + 23, 613), number, 19, TEAL, True)
        text(draw, (x + 23, 651), label, 30, INK, True)
        text(draw, (x + 23, 706), body, 22, MUTED)
        if i < 2:
            arrow(draw, (x + 388, 677), (x + 410, 677), TEAL, 3, 8)
    check(draw, 159, 825, 25)
    text(draw, (202, 821), "A changed plan needs a new review.", 25, TEAL, True)


@lru_cache(maxsize=72)
def mascot_image(size, pose):
    path = HERE / "assets" / f"ask-q-{pose}.png"
    im = Image.open(path).convert("RGBA")
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    return im


def mascot(image, x, y, size, t):
    blink = t % 4.8 > 4.61
    pose = "blink" if blink else str(round(4 + math.sin(t * 2.1)*4))
    asset = mascot_image(size, pose)
    image.paste(asset, (round(x), round(y + math.sin(t * 2)*6)), asset)


def prepare_mascot_assets():
    """Wave and blink the checked-in snapshot of the actual product SVG."""
    assets = HERE / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    svg = (assets / "ask-q.svg").read_text(encoding="utf-8")
    for pose in [*(str(x) for x in range(9)), "blink"]:
        variant = svg
        if pose == "blink":
            variant = variant.replace('ry="6"', 'ry="1"')
        else:
            angle = (int(pose)-4)*2.5
            variant = variant.replace('class="ask-q-arm ask-q-arm-left"',
                                      f'class="ask-q-arm ask-q-arm-left" transform="rotate({angle} 49 93)"')
            variant = variant.replace('class="ask-q-arm ask-q-arm-right"',
                                      f'class="ask-q-arm ask-q-arm-right" transform="rotate({-angle} 98 93)"')
        vector = assets/f"ask-q-{pose}.svg"
        vector.write_text(variant+"\n", encoding="utf-8")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(vector), "-frames:v", "1", str(assets/f"ask-q-{pose}.png")], check=True)
        vector.unlink()


def ask_panel(image, t):
    draw = ImageDraw.Draw(image)
    card(draw, (96, 318, 1390, 900))
    illustration_label(draw)
    mascot(image, 112, 420, 320, t)
    text(draw, (272, 776), "Ask Q", 36, INK, True, anchor="mt")
    text(draw, (272, 833), "Your contextual guide", 22, MUTED, anchor="mt")
    chat = [
        ("Guide setup", "Choose a registered source and target."),
        ("Follow the selected run", "Read-only updates while the workspace is open."),
        ("Propose the next step", "Open plan review or confirm a permitted retry."),
    ]
    for i, (title, detail) in enumerate(chat):
        y = 407 + i * 145
        fill = "#eaf6ef" if t >= 1.1 + i * 1.3 else "#f7faf8"
        rr(draw, (463, y, 1351, y+117), fill, BORDER, 17)
        text(draw, (494, y+23), title, 28, INK, True)
        text(draw, (494, y+69), detail, 23, MUTED)
    text(draw, (496, 860), "Suggestions respect your role. Actions stay explicit.", 20, TEAL, True)


def evidence_panel(image, t):
    draw = ImageDraw.Draw(image)
    card(draw, (96, 318, 1390, 900))
    illustration_label(draw)
    text(draw, (136, 402), "Evidence", 19, MUTED, True)
    text(draw, (574, 402), "What the run records", 19, MUTED, True)
    for i, (label, value) in enumerate((
            ("Mapped data", "Row and column comparison"),
            ("Key reconciliation", "Missing, extra and duplicate keys"),
            ("Quality rules", "Check outcomes and correction counts"))):
        y = 454+i*86
        draw.line((134,y+64,1350,y+64), fill=BORDER, width=1)
        text(draw, (138,y+14),label,27,INK,True)
        text(draw, (574,y+14),value,26,MUTED)
    rr(draw, (134, 750, 1351, 860), "#eff8f2", BORDER, 15)
    text(draw, (159, 772), "COLUMN LINEAGE", 16, TEAL, True)
    text(draw, (160, 813), "customers.name", 23, INK, mono=True)
    arrow(draw, (389, 824), (495, 824), TEAL, 3)
    text(draw, (527, 813), "trim", 23, TEAL, True)
    arrow(draw, (602, 824), (710, 824), TEAL, 3)
    text(draw, (747, 813), "customers.full_name", 23, INK, mono=True)


def cloud_panel(image, t):
    draw = ImageDraw.Draw(image)
    rr(draw, (529, 332, 1391, 442), WHITE, BORDER, 22)
    text(draw, (960, 355), "Next.js console + LangGraph control plane", 30, INK, True, anchor="mt")
    text(draw, (960, 405), "Plans, approvals and retained evidence", 22, MUTED, anchor="mt")
    draw.line((960,443,960,482), fill="#9cbcab", width=3)
    draw.line((360,482,1560,482), fill="#9cbcab", width=3)
    items = [
        ("AWS", "Step Functions", "Glue · reviewed DMS tasks", "EC2 pilot + Fargate blueprint"),
        ("Azure", "Data Factory", "Reviewed pipelines and adapters", "VM pilot + Container Apps blueprint"),
        ("Databricks", "Jobs + Delta Lake", "Unity Catalog · optional Mosaic AI", "Reviewed lakehouse execution"),
    ]
    for i, (label, execution, details, deploy) in enumerate(items):
        x = 96 + i*600
        arrow(draw, (x+264,482),(x+264,528),"#9cbcab",3)
        card(draw,(x,530,x+528,835))
        pill(draw,(x+27,557),label,size=24)
        text(draw,(x+28,639),execution,32,INK,True)
        text(draw,(x+28,698),details,21,MUTED)
        draw.line((x+27,751,x+501,751),fill=BORDER,width=1)
        text(draw,(x+28,780),deploy,21,MUTED)
    text(draw,(960,880),"Reference architectures · Native jobs via CLI/Python · Qualify each environment",23,TEAL,True,anchor="mt")


def intro(image,t):
    draw=ImageDraw.Draw(image)
    header(draw,"A shared migration workspace")
    text(draw,(99,220),"AGENTIC DATA MIGRATION",21,TEAL,True)
    lines(draw,(95,285),"A clear plan.\nA shared workspace.",85,INK,True,gap=1.2)
    lines(draw,(100,525),"Coordinate the people, policies\nand data behind every move.",32,MUTED,gap=1.5)
    x=100
    for label in ("Roles","Plans","Evidence"):
        x+=pill(draw,(x,708),label,size=23)+17
    card(draw,(1125,239,1789,869))
    text(draw,(1164,277),"ONE REVIEWABLE WORKFLOW",20,MUTED,True)
    steps=[("01","Discover","Understand the source"),("02","Plan","Make mappings explicit"),
           ("03","Approve","Bind review to the plan"),("04","Reconcile","Retain the evidence")]
    for i,(number,title,detail) in enumerate(steps):
        y=354+i*123
        draw.ellipse((1166,y,1222,y+56),fill=TEAL if t>=i*.9 else PALE)
        text(draw,(1194,y+16),number,20,WHITE if t>=i*.9 else TEAL,True,anchor="mt")
        if i<3:
            draw.line((1194,y+58,1194,y+120),fill=BORDER,width=3)
        text(draw,(1254,y+1),title,29,INK,True)
        text(draw,(1254,y+48),detail,24,MUTED)


def outro(image,t):
    draw=ImageDraw.Draw(image)
    header(draw,"Explore the framework",True)
    text(draw,(101,232),"FROM DISCOVERY TO EVIDENCE",21,"#a8cdb8",True)
    lines(draw,(95,301),"Migration,\nwith a plan.",100,WHITE,True,gap=1.16)
    text(draw,(101,588),"Explore the code. Run the local demo.",32,"#bcdac8")
    rr(draw,(98,696,1295,804),"#d6efdf",None,19)
    text(draw,(137,730),"github.com/mahi80/DataMigration",37,DARK,True)
    arrow(draw,(1184,751),(1239,751),DARK,4,16)
    text(draw,(102,857),"Next.js  /  LangGraph  /  Python",24,"#9fc5ae")
    mascot(image,1427,365,328,t)
    text(draw,(1591,725),"Ask Q",36,WHITE,True,anchor="mt")
    text(draw,(1591,782),"Guidance, in context.",24,"#bcdac8",anchor="mt")


def scene_frame(index,t):
    image=background(index==6).copy()
    draw=ImageDraw.Draw(image)
    if index==0:
        intro(image,t)
        return image
    if index==6:
        outro(image,t)
        return image
    header(draw,SCENES[index][2])
    subtitles={
        1:"Choose registered connections, tables, keys and quality rules.",
        2:"Human review applies to the plan that will execute.",
        3:"Ask Q works with your role and the selected migration.",
        4:"Inspect the result within its configured reconciliation scope.",
        5:"Start with documented pilots. Qualify the path for your workload.",
    }
    chapter_title(draw,SCENES[index][3],subtitles[index])
    if index==5:
        cloud_panel(image,t)
        return image
    if not screenshot_stage(image,index):
        {1:mapping_panel,2:approval_panel,3:ask_panel,4:evidence_panel}[index](image,t)
    items={
        1:[("Discover", "Use registered\nsource profiles."),("Map", "Choose columns\nand transformations."),("Set rules", "Declare keys and\nquality checks.")],
        2:[("Role-aware", "Planning and review\nuse allowed actions."),("Exact version", "Approval binds to\na plan fingerprint."),("Bounded writes", "Execution follows\nthe approved scope.")],
        3:[("Guide", "Get help with\nworkspace setup."),("Follow", "Opt-in updates while\nthe console is open."),("Confirm", "Review suggestions\nbefore any action.")],
        4:[("Compare", "Mapped rows\nand column values."),("Explain", "Quality outcomes\nand correction counts."),("Trace", "Source-to-target\ncolumn lineage.")],
    }
    notes(ImageDraw.Draw(image),items[index])
    return image


def frame_at(time):
    index=next((i for i,(start,end,*_) in enumerate(SCENES) if start<=time<end),6)
    start,end,*_=SCENES[index]
    local=time-start
    image=scene_frame(index,local)
    # Brief dissolves preserve reading time and avoid artificial cursor clicks.
    if index and local<.55:
        previous=scene_frame(index-1,SCENES[index-1][1]-SCENES[index-1][0])
        image=Image.blend(previous,image,ease(local/.55))
    draw=ImageDraw.Draw(image)
    dark=index==6
    draw.rectangle((0,952,W,H),fill=DARK if dark else "#f0f5f1")
    caption=next((v for a,b,v in CAPTIONS if a<=time<b),CAPTIONS[-1][2])
    text(draw,(960,982),caption,28,"#e4f5e9" if dark else INK,anchor="mt")
    draw.rectangle((96,1045,1824,1049),fill="#477c65" if dark else "#d5e2d8")
    draw.rectangle((96,1045,96+(1824-96)*min(time/DURATION,1),1049),fill="#c1e6ce" if dark else TEAL)
    return image


def synthesize_audio(path):
    """Original 80 BPM instrumental; analytic oscillators, no sampled recordings."""
    rate=48000
    count=rate*DURATION
    audio=np.zeros((count,2),dtype=np.float64)
    chords=[(57,60,64),(53,57,60),(48,55,60),(55,59,62)]
    for bar in range(20):
        a=bar*3
        length=min(4.4,DURATION-a)
        tt=np.arange(round(length*rate))/rate
        env=(1-np.exp(-tt*2))*np.exp(-tt*.48)
        release=np.clip((length-tt)/.8,0,1)
        for k,note in enumerate(chords[bar%4]):
            freq=440*2**((note-69)/12)
            wavelet=(np.sin(2*np.pi*freq*tt)+.18*np.sin(2*np.pi*freq*2*tt))*.032*env*release
            left=.9 if k%2 else .65
            right=.65 if k%2 else .9
            i=a*rate
            audio[i:i+len(tt),0]+=wavelet*left
            audio[i:i+len(tt),1]+=wavelet*right
        for beat in range(4):
            onset=a+beat*.75
            n=min(round(rate*.55),count-round(onset*rate))
            tt=np.arange(n)/rate
            kick=np.sin(2*np.pi*(58*tt-9*tt**2))*.035*np.exp(-tt*13)
            i=round(onset*rate)
            audio[i:i+n]+=kick[:,None]
        note=chords[bar%4][(bar//2)%3]+12
        freq=440*2**((note-69)/12)
        onset=a+1.5
        n=min(round(rate*2.2),count-round(onset*rate))
        tt=np.arange(n)/rate
        bell=(np.sin(2*np.pi*freq*tt)+.12*np.sin(2*np.pi*freq*3*tt))*.025*np.exp(-tt*2.7)*(1-np.exp(-tt*90))
        i=round(onset*rate)
        audio[i:i+n,bar%2]+=bell
        audio[i:i+n,1-bar%2]+=bell*.55
    timeline=np.arange(count)/rate
    fade=np.minimum(np.clip(timeline/1.8,0,1),np.clip((60-timeline)/2.8,0,1))
    # A quiet instrumental bed with ample headroom, audible without narration.
    audio*=fade[:,None]*4.0
    pcm=np.clip(audio*32767,-32768,32767).astype("<i2")
    with wave.open(str(path),"wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm.tobytes())


def contact_sheet(path):
    sheet=Image.new("RGB",(1920,642),"#eef4ef")
    draw=ImageDraw.Draw(sheet)
    for index,(start,end,label,_) in enumerate(SCENES):
        preview=frame_at((start+end)/2).resize((464,261),Image.Resampling.LANCZOS)
        x=16+(index%4)*480
        y=16+(index//4)*313
        sheet.paste(preview,(x,y))
        text(draw,(x+6,y+276),f"{start:02d}–{end:02d}s  {label}",21,INK,True)
    path.parent.mkdir(parents=True,exist_ok=True)
    sheet.save(path,quality=94)


def srt(path):
    def stamp(seconds):
        milliseconds=round(seconds*1000)
        minutes,ms=divmod(milliseconds,60000)
        sec,ms=divmod(ms,1000)
        return f"00:{minutes:02d}:{sec:02d},{ms:03d}"
    path.write_text("\n\n".join(f"{i+1}\n{stamp(a)} --> {stamp(b)}\n{value}" for i,(a,b,value) in enumerate(CAPTIONS))+"\n",encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--contact-sheet",type=Path)
    parser.add_argument("--preview-only",action="store_true")
    parser.add_argument("--height",type=int,choices=(720,1080),default=1080)
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required")
    prepare_mascot_assets()
    srt(HERE/"walkthrough.srt")
    contact=args.contact_sheet or args.output.with_name("datamigration-contact-sheet.jpg")
    contact_sheet(contact)
    print(f"Contact sheet: {contact}",flush=True)
    if args.preview_only:
        return
    with tempfile.TemporaryDirectory(prefix="datamigration-video-") as temp:
        audio=Path(temp)/"original-score.wav"
        synthesize_audio(audio)
        size=(args.height*16//9,args.height)
        command=["ffmpeg","-hide_banner","-loglevel","error","-y",
                 "-f","rawvideo","-pixel_format","rgb24","-video_size",f"{size[0]}x{size[1]}",
                 "-framerate",str(FPS),"-i","pipe:0","-i",str(audio),
                 "-map","0:v:0","-map","1:a:0","-frames:v",str(FPS*DURATION),"-t",str(DURATION),
                 "-c:v","libx264","-preset","fast","-crf","18","-pix_fmt","yuv420p",
                 "-c:a","aac","-b:a","192k","-movflags","+faststart","-video_track_timescale","90000",
                 "-metadata","title=DataMigration — A reviewed migration workspace",
                 "-metadata","comment=Original motion graphics and synthesized score; illustrations are labeled.",
                 str(args.output)]
        process=subprocess.Popen(command,stdin=subprocess.PIPE)
        try:
            for frame in range(FPS*DURATION):
                image=frame_at(frame/FPS)
                if image.size!=size:
                    image=image.resize(size,Image.Resampling.LANCZOS)
                process.stdin.write(image.tobytes())
                if frame%300==0:
                    print(f"Rendered {frame//FPS:02d}/{DURATION}s",flush=True)
        finally:
            process.stdin.close()
        if process.wait()!=0:
            raise RuntimeError("FFmpeg encode failed")
    result=subprocess.run(["ffprobe","-v","error","-show_entries",
                           "format=duration,size:stream=codec_name,width,height,r_frame_rate,duration,nb_frames",
                           "-of","json",str(args.output)],check=True,capture_output=True,text=True)
    metadata=json.loads(result.stdout)
    if abs(float(metadata["format"]["duration"])-60)>0.001:
        raise RuntimeError(f"Unexpected duration: {metadata}")
    verification=args.output.with_suffix(".verification.json")
    verification.write_text(json.dumps(metadata,indent=2)+"\n")
    print(f"Verified exact 60-second video: {args.output}",flush=True)
    print(result.stdout,flush=True)


if __name__=="__main__":
    main()
