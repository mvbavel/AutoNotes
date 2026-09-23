#!/usr/bin/env python3
"""AutoNotes agent driver — launch, screenshot and exercise the app headlessly.

Run from the repo root with the venv active. Subcommands:

    gui        render MainWindow offscreen, screenshot it, dump widget state
    pipeline   offline pipeline smoke: synth video -> extract_frames -> DOCX
    probe URL  resolve a YouTube/Teams/SharePoint URL (network, no API spend)
    tests      the unittest suite

`gui` and `pipeline` are fully offline and cost nothing. Neither ever calls
Claude — note generation is the only paid stage and this driver never invokes
it, so an agent can iterate freely.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, REPO)


# ── gui ────────────────────────────────────────────────────────────────────────

def cmd_gui(args) -> int:
    # Must precede any Qt import. "offscreen" renders into a buffer, so
    # widget.grab() works with no display and no macOS screen-recording grant.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    app = QApplication([])
    app.setApplicationName("AutoNotes")
    app.setOrganizationName("AutoNotes")
    win = MainWindow()

    w, h = (int(x) for x in args.size.split("x"))
    win.resize(w, h)
    win.show()

    # MainWindow._load_settings() pulls the real API key from the login
    # Keychain and the real Join URL from QSettings. Blank them before any
    # screenshot so agent artifacts don't carry the user's secrets.
    if not args.keep_secrets:
        win.api_key_edit.clear()
        win.hf_token_edit.clear()
        win.ms_client_id_edit.clear()
        win.ms_join_url_edit.clear()

    # process_btn needs BOTH a URL and a non-empty key (_on_input_changed), so
    # blanking secrets above leaves it disabled. A dummy key reaches the
    # "Ready" state without touching the real one.
    if args.api_key:
        win.api_key_edit.setText(args.api_key)

    if args.url:
        win.url_edit.setText(args.url)

    app.processEvents()

    state = {
        "platform": app.platformName(),
        "version_title": win.windowTitle(),
        "url_edit": win.url_edit.text(),
        "process_btn": {
            "text": win.process_btn.text(),
            "enabled": win.process_btn.isEnabled(),
        },
        "stages": [lbl.text() for lbl in win.stage_labels],
        "whisper_model": win.model_combo.currentText(),
        "output_dir": win.output_dir_edit.text(),
        "reuse_enabled": win.reuse_transcript_check.isEnabled(),
        "reuse_info": win.reuse_info_label.text(),
    }

    if args.screenshot:
        pm = win.grab()
        if not pm.save(args.screenshot):
            print(f"FAIL: could not save {args.screenshot}", file=sys.stderr)
            return 1
        state["screenshot"] = {
            "path": args.screenshot,
            "size": f"{pm.width()}x{pm.height()}",
        }

    print(json.dumps(state, indent=2))

    # Guard against a silently-broken build: an all-white or 0-size grab means
    # the window never rendered, which would otherwise read as a pass.
    if args.screenshot and os.path.getsize(args.screenshot) < 5000:
        print("FAIL: screenshot suspiciously small — window likely blank",
              file=sys.stderr)
        return 1
    return 0


# ── pipeline ───────────────────────────────────────────────────────────────────

_SLIDES = [
    ("Q3 Architecture Review", "Platform Engineering"),
    ("Current State", "3 regions - 12 services - 4 datastores"),
    ("The Problem", "Cross-region latency exceeds 400ms at p99"),
    ("Proposal A", "Read replicas in each region"),
    ("Proposal B", "Event-sourced cache invalidation"),
    ("Cost Comparison", "A: $14k/mo    B: $9k/mo"),
    ("Rollout Plan", "Phase 1 pilot - Phase 2 GA - Phase 3 deprecate"),
    ("Next Steps", "Sign-off by Friday"),
]


def _synth_video(path: str, seconds_per_slide: int, log) -> float:
    """Render distinct text slides and mux them into a silent H.264 video.

    Real slides (not ffmpeg's testsrc) so the screen-detection, dedup and
    slide-likeness scoring in frame_extractor get something representative.
    """
    from PIL import Image, ImageDraw, ImageFont
    from pipeline._paths import FFMPEG

    # PIL's default bitmap font is ~11px. frame_extractor's dedup signature is
    # a 256x144 grayscale diff, at which scale 11px text vanishes and every
    # slide looks identical — 8 slides collapse to 2. Real fonts at real sizes
    # are what make the slides distinguishable to dedup.
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    f_title = ImageFont.truetype(font_path, 96)
    f_body = ImageFont.truetype(font_path, 60)
    f_item = ImageFont.truetype(font_path, 40)

    tmp = tempfile.mkdtemp(prefix="autonotes_slides_")
    for i, (title, body) in enumerate(_SLIDES):
        img = Image.new("RGB", (1920, 1080), (250, 250, 248))
        d = ImageDraw.Draw(img)
        # A dark banner plus rules gives strong rectilinear gradients, which is
        # what the slide-likeness scorer keys on.
        d.rectangle([0, 0, 1920, 190], fill=(28, 42, 74))
        d.text((80, 55), title, fill=(255, 255, 255), font=f_title)
        d.text((80, 290), body, fill=(20, 20, 20), font=f_body)
        for r in range(4):
            y = 460 + r * 130
            d.rectangle([80, y, 1840, y + 5], fill=(200, 200, 205))
            d.text((110, y + 30), f"- detail {r + 1}: {title}",
                   fill=(60, 60, 60), font=f_item)
        img.save(os.path.join(tmp, f"slide{i:03d}.png"))

    duration = len(_SLIDES) * seconds_per_slide
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-framerate", f"1/{seconds_per_slide}",
        "-i", os.path.join(tmp, "slide%03d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "10",
        path,
    ]
    log(f"ffmpeg: rendering {duration}s / {len(_SLIDES)} slides")
    subprocess.run(cmd, check=True, capture_output=True)
    return float(duration)


def cmd_pipeline(args) -> int:
    from pipeline.frame_extractor import extract_frames
    from output.docx_writer import write_docx

    out_dir = args.out or tempfile.mkdtemp(prefix="autonotes_smoke_")
    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="autonotes_work_")

    def log(m):
        print(f"  {m}", flush=True)

    print("[1/3] synthesising slide video")
    video = os.path.join(work, "slides.mp4")
    duration = _synth_video(video, args.seconds_per_slide, log)
    log(f"video: {video} ({os.path.getsize(video) // 1024} KB)")

    print("[2/3] extract_frames")
    # Visual-cue phrases exercise the transcript-proximity score bonus.
    segments = [
        {"start": i * args.seconds_per_slide,
         "end": (i + 1) * args.seconds_per_slide,
         "text": ("as you can see here, " if i % 2 == 0 else "moving on, ")
                 + title,
         "speaker": "Speaker"}
        for i, (title, _) in enumerate(_SLIDES)
    ]
    frames = extract_frames(video, work, segments=segments,
                            progress_cb=lambda p: None)
    log(f"selected {len(frames)} frames from a {int(duration)}s video")
    for f in frames:
        log(f"  t={f['timestamp']:7.1f}s score={f['score']:.3f} "
            f"cropped={f['cropped']} {os.path.basename(f['path'])}")

    if not frames:
        print("FAIL: extract_frames returned nothing", file=sys.stderr)
        return 1

    print("[3/3] write_docx")
    notes = {
        "title": "Q3 Architecture Review",
        "chapters": [
            {
                "title": "Current State and Problem",
                "speakers": ["Speaker"],
                "key_points": [
                    {"text": "Three regions serve **12 services**.",
                     "screenshot_idx": 1},
                    {"text": "Cross-region p99 latency exceeds 400ms."},
                ],
            },
            {
                "title": "Proposals",
                "speakers": ["Speaker"],
                "key_points": [
                    {"text": "Proposal B is **cheaper** at $9k/mo.",
                     "screenshot_idx": min(2, len(frames))},
                    {"text": "Rollout is phased over three stages."},
                ],
            },
        ],
    }
    source = {
        "type": "Local file (driver synth)",
        "url": video,
        "summary": "Synthetic slide deck generated by the AutoNotes driver.",
    }
    docx = write_docx(notes, frames, out_dir, "driver_smoke",
                      log_cb=log, source_info=source)
    size = os.path.getsize(docx)
    log(f"docx: {docx} ({size // 1024} KB)")

    if size < 20000:
        print("FAIL: docx too small — screenshots probably not embedded",
              file=sys.stderr)
        return 1

    print(f"\nPASS  {len(frames)} frames -> {docx}")
    return 0


# ── probe ──────────────────────────────────────────────────────────────────────

def cmd_probe(args) -> int:
    from pipeline.teams_downloader import is_teams_url, _fetch_info, _BROWSERS

    url = args.url
    if not is_teams_url(url):
        print("Not a Teams/SharePoint URL; use yt-dlp directly for YouTube.")
        return 2

    print(f"browser cookie order: {_BROWSERS}")
    info = _fetch_info(url, log_cb=lambda m: print(f"  LOG: {m[:160]}"))
    if not info:
        print("\nFAIL: metadata fetch failed for every browser",
              file=sys.stderr)
        return 1
    print(json.dumps({
        "title": info.get("title"),
        "duration": info.get("duration"),
        "formats": len(info.get("formats") or []),
    }, indent=2))
    return 0


# ── tests ──────────────────────────────────────────────────────────────────────

def cmd_tests(args) -> int:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "tests", "-v"],
        cwd=REPO,
    ).returncode


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gui", help="render MainWindow offscreen + screenshot")
    g.add_argument("--screenshot", metavar="PATH")
    g.add_argument("--url", help="prefill the video input field")
    g.add_argument("--size", default="1200x800")
    g.add_argument("--api-key", metavar="STR",
                   help="dummy key to enable the button (never sent anywhere)")
    g.add_argument("--keep-secrets", action="store_true",
                   help="do NOT blank Keychain/QSettings values (leaks them)")
    g.set_defaults(func=cmd_gui)

    pl = sub.add_parser("pipeline", help="offline frames+docx smoke test")
    pl.add_argument("--out", metavar="DIR", help="where to write the DOCX")
    pl.add_argument("--seconds-per-slide", type=int, default=30,
                    help="30 clears MIN_GAP_SECONDS=25 so slides survive dedup")
    pl.set_defaults(func=cmd_pipeline)

    pr = sub.add_parser("probe", help="resolve a Teams/SharePoint URL")
    pr.add_argument("url")
    pr.set_defaults(func=cmd_probe)

    t = sub.add_parser("tests", help="run the unittest suite")
    t.set_defaults(func=cmd_tests)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
