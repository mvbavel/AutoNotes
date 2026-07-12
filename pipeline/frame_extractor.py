import os
import json
import subprocess
import numpy as np

from pipeline._paths import FFMPEG, FFPROBE

MAX_FRAMES = 60                  # hard ceiling on selected frames
MIN_FRAMES = 8                   # floor for very short videos
TARGET_SECONDS_PER_FRAME = 30    # aim for one screenshot per 30s of video
INTERVAL_SECONDS = 5      # frame sampling interval
EXTRACT_WIDTH = 1280      # width frames are extracted at; DOCX embeds full res
API_MAX_WIDTH = 1000      # downscaled copy sent to Claude (controls token cost)

# Fraction of significantly-changed pixels (256x144 grayscale, per-pixel
# threshold 20) below which two frames are considered the same slide/scene
# and collapsed into one candidate. Must stay sensitive enough to catch a
# single bullet point appearing (~0.006) or a line being reworded (~0.003);
# identical frames re-encoded measure 0.000.
_SAME_SLIDE_DIFF = 0.002
_SIG_SIZE = (256, 144)
_SIG_PIXEL_THRESH = 20

# Region used for the dedup signature (x0, x1, y0, y1 as frame fractions).
# Excludes the right-hand strip and the top/bottom edges, where Teams meeting
# recordings place the live webcam gallery and speaker caption — constant
# motion there would otherwise make every 5s sample look like a new slide.
_SIG_REGION = (0.0, 0.78, 0.05, 0.92)

# Minimum spacing between selected frames. Guarantees coverage across the
# whole video: a burst of high-scoring frames (a demo with constant motion,
# or dedup defeated by an unusual layout) can't crowd out slides elsewhere.
MIN_GAP_SECONDS = 25

# Score bonus for frames near a transcript segment containing a visual cue
_CUE_BONUS = 0.05
_CUE_PHRASES = (
    "slide", "screen", "diagram", "chart", "graph", "figure", "table",
    "as you can see", "you can see", "shown here", "this shows",
    "look at", "looking at", "demo", "example", "code", "here we have",
    "terminal", "console", "browser", "dashboard", "window", "if i click",
    "when i click", "let me show", "i'll show", "pull up", "switch to",
)

# Bonus when the frame was cropped to a detected physical screen — a screen
# being shown is itself evidence the frame matters, whatever it displays
_CROP_BONUS = 0.05


def extract_frames(video_path: str, temp_dir: str, progress_cb=None,
                   segments: list[dict] | None = None,
                   cancel_check=None) -> list[dict]:
    """
    Extract candidate frames from the video, crop to a detected presentation
    screen where possible, collapse near-duplicate slides, score for
    slide-likeness (with a bonus near visual-cue transcript moments),
    and return the top frames sorted by timestamp. The selection budget
    scales with recording length (one frame per TARGET_SECONDS_PER_FRAME,
    clamped to [MIN_FRAMES, MAX_FRAMES]), so long recordings degrade toward
    one frame per minute or less, and scene-poor videos naturally yield
    fewer via dedup.
    Returns list of {timestamp, path, api_path, score, cropped}: "path" is
    the full-resolution image (embedded in the DOCX), "api_path" a copy
    downscaled to API_MAX_WIDTH (sent to Claude).
    """
    import cv2

    frames_dir = os.path.join(temp_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    duration = _get_duration(video_path)
    if duration <= 0:
        return []

    _extract_all_frames(video_path, frames_dir, duration, progress_cb, cancel_check)

    frame_files = sorted(
        f for f in os.listdir(frames_dir)
        if f.startswith("frame_") and f.endswith(".jpg")
    )
    if not frame_files:
        return []

    cue_windows = _cue_windows(segments)

    # Score every frame (cropping to the presentation screen when detected),
    # grouping consecutive near-identical frames so each slide/scene
    # contributes only its best-scoring frame.
    groups: list[dict] = []          # best candidate per slide group
    prev_hash = None

    for i, fname in enumerate(frame_files):
        if cancel_check:
            cancel_check()
        ts = _frame_index_to_ts(fname)
        path = os.path.join(frames_dir, fname)
        result = _process_frame(cv2, path)
        if result is None:
            continue
        score, img_hash, cropped = result

        if _in_cue_window(ts, cue_windows):
            score += _CUE_BONUS

        candidate = {"timestamp": ts, "path": path, "score": score,
                     "cropped": cropped}

        if prev_hash is not None and _frame_diff(img_hash, prev_hash) < _SAME_SLIDE_DIFF:
            # Same slide as previous frame — keep whichever scores higher
            if score > groups[-1]["score"]:
                groups[-1] = candidate
        else:
            groups.append(candidate)
        prev_hash = img_hash

        if progress_cb:
            progress_cb(60 + int((i + 1) / len(frame_files) * 35))

    top = _select_top(groups, _frame_budget(duration), MIN_GAP_SECONDS)

    for frame in top:
        # ffmpeg's mjpeg output lacks the JFIF/Exif marker python-docx
        # requires; rewrite via cv2 so the DOCX embed can't reject it
        if _needs_jfif_rewrite(frame["path"]):
            img = cv2.imread(frame["path"])
            if img is not None:
                cv2.imwrite(frame["path"], img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        frame["api_path"] = _make_api_copy(cv2, frame["path"])

    if progress_cb:
        progress_cb(100)
    return top


def _get_duration(video_path: str) -> float:
    cmd = [
        FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_format", video_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return 0.0
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def _extract_all_frames(video_path: str, frames_dir: str, duration: float,
                        progress_cb=None, cancel_check=None):
    """Single decode pass sampling one frame every INTERVAL_SECONDS."""
    pattern = os.path.join(frames_dir, "frame_%06d.jpg")
    cmd = [
        FFMPEG, "-y", "-i", video_path,
        "-vf", f"fps=1/{INTERVAL_SECONDS},scale={EXTRACT_WIDTH}:-2",
        "-q:v", "2",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        pattern,
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        for line in proc.stdout:
            if cancel_check:
                cancel_check()
            # ffmpeg reports out_time_ms in microseconds
            if line.startswith("out_time_ms=") and progress_cb:
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                progress_cb(min(60, int(seconds / duration * 60)))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _frame_index_to_ts(fname: str) -> int:
    # frame_000001.jpg is sampled at t=0, frame_000002 at t=INTERVAL, ...
    idx = int(fname[len("frame_"):-len(".jpg")])
    return (idx - 1) * INTERVAL_SECONDS


def _process_frame(cv2, path: str):
    """
    Score a frame, cropping to a detected presentation screen when that
    improves it. Overwrites the file with the crop. Returns
    (score, perceptual_hash, cropped) or None if unreadable.
    The hash is computed on the full frame so dedup is stable even when
    crop detection flickers between adjacent frames.
    """
    img = cv2.imread(path)
    if img is None:
        return None

    img_hash = _frame_hash(cv2, img)
    full_score = _score_image(cv2, img)

    quad = _detect_screen_quad(cv2, img)
    if quad is not None:
        cropped = _warp_crop(cv2, img, quad)
        # Accept the crop when it contains real content (edges): a blank
        # wall, door, or window crop measures near zero. Don't require it
        # to out-score the full frame — a busy demo screen in an otherwise
        # calm room can legitimately score lower than its surroundings.
        if cropped is not None and _edge_density(
            cv2, cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
        ) >= 0.01:
            crop_score = _score_image(cv2, cropped) + _CROP_BONUS
            cv2.imwrite(path, cropped, [cv2.IMWRITE_JPEG_QUALITY, 90])
            return crop_score, img_hash, True

    return full_score, img_hash, False


def _detect_screen_quad(cv2, img):
    """
    Find the largest convex quadrilateral that plausibly is a presentation
    screen / projected slide: 15–95% of the frame, screen-like aspect ratio,
    and nearly rectangular. Returns a 4x2 float32 corner array or None.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    frame_area = img.shape[0] * img.shape[1]
    best_quad = None
    best_area = 0.0

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 0.15 * frame_area or area > 0.95 * frame_area:
            continue
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        x, y, w, h = cv2.boundingRect(approx)
        if h == 0 or not (1.0 <= w / h <= 2.6):
            continue
        if area / (w * h) < 0.80:
            continue
        if area > best_area:
            best_area = area
            best_quad = approx.reshape(4, 2).astype(np.float32)

    return best_quad


def _warp_crop(cv2, img, quad):
    """Perspective-correct the quad to an upright rectangle."""
    ordered = _order_corners(quad)
    (tl, tr, br, bl) = ordered
    width = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if width < 200 or height < 150:
        return None
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, dst)
    return cv2.warpPerspective(img, matrix, (width, height))


def _order_corners(quad):
    """Order 4 corners as top-left, top-right, bottom-right, bottom-left."""
    sums = quad.sum(axis=1)
    diffs = np.diff(quad, axis=1).flatten()
    return np.array([
        quad[np.argmin(sums)],   # top-left: smallest x+y
        quad[np.argmin(diffs)],  # top-right: smallest y-x
        quad[np.argmax(sums)],   # bottom-right: largest x+y
        quad[np.argmax(diffs)],  # bottom-left: largest y-x
    ], dtype=np.float32)


def _frame_budget(duration: float) -> int:
    """Screenshot budget for a recording of the given length in seconds."""
    return int(min(MAX_FRAMES, max(MIN_FRAMES, duration / TARGET_SECONDS_PER_FRAME)))


def _select_top(groups: list[dict], max_frames: int, min_gap: float) -> list[dict]:
    """
    Pick the highest-scoring frames subject to a minimum time gap between
    picks, then fill any remaining slots by score alone (short videos may not
    have max_frames gap-respecting candidates). Returned sorted by timestamp.
    """
    by_score = sorted(groups, key=lambda g: g["score"], reverse=True)
    picked: list[dict] = []
    picked_ids: set[int] = set()

    for cand in by_score:
        if len(picked) >= max_frames:
            break
        if all(abs(cand["timestamp"] - p["timestamp"]) >= min_gap for p in picked):
            picked.append(cand)
            picked_ids.add(id(cand))

    for cand in by_score:
        if len(picked) >= max_frames:
            break
        if id(cand) not in picked_ids:
            picked.append(cand)
            picked_ids.add(id(cand))

    picked.sort(key=lambda g: g["timestamp"])
    return picked


def _frame_hash(cv2, img):
    """Downsampled grayscale signature for near-duplicate detection.

    Computed on the central content region only (_SIG_REGION), so webcam
    tiles and captions at the frame edges don't register as slide changes.
    """
    h, w = img.shape[:2]
    x0, x1, y0, y1 = _SIG_REGION
    region = img[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, _SIG_SIZE, interpolation=cv2.INTER_AREA).astype(np.int16)


def _frame_diff(a, b) -> float:
    """Fraction of pixels that changed significantly between two signatures."""
    return float(np.count_nonzero(np.abs(a - b) > _SIG_PIXEL_THRESH)) / a.size


def _cue_windows(segments: list[dict] | None) -> list[tuple[float, float]]:
    """Time windows around transcript moments that reference visuals."""
    windows = []
    for seg in segments or []:
        text = seg.get("text", "").lower()
        if any(phrase in text for phrase in _CUE_PHRASES):
            windows.append((seg["start"] - 2.0, seg["start"] + 8.0))
    return windows


def _in_cue_window(ts: float, windows: list[tuple[float, float]]) -> bool:
    return any(lo <= ts <= hi for lo, hi in windows)


def _needs_jfif_rewrite(path: str) -> bool:
    """True for JPEGs lacking a JFIF/Exif APP marker.

    ffmpeg's mjpeg encoder emits JPEGs that open with a comment segment
    ("Lavc…") instead of an APP0/APP1 marker; OpenCV reads them fine but
    python-docx raises UnrecognizedImageError on embed.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(10)
    except OSError:
        return False
    return head[:2] == b"\xff\xd8" and head[6:10] not in (b"JFIF", b"Exif")


def _make_api_copy(cv2, path: str) -> str:
    """
    Write a copy downscaled to API_MAX_WIDTH for sending to Claude,
    leaving the original at full resolution for the document.
    Returns the copy's path (the original if already small enough).
    """
    img = cv2.imread(path)
    if img is None:
        return path
    h, w = img.shape[:2]
    if w <= API_MAX_WIDTH:
        return path
    new_h = int(h * API_MAX_WIDTH / w)
    small = cv2.resize(img, (API_MAX_WIDTH, new_h), interpolation=cv2.INTER_AREA)
    api_path = path[:-len(".jpg")] + "_api.jpg"
    cv2.imwrite(api_path, small, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return api_path


def _score_image(cv2, img) -> float:
    """
    Score an image for screen-content likeness: slides, demos, terminals,
    code editors, browsers, dashboards. What all of these share — and what
    camera shots of people/rooms lack — is dense, crisp, axis-aligned edge
    structure (text and rectilinear UI).
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Edge density: text and UI produce lots of crisp edges (10%+ = max)
    edge_component = min(_edge_density(cv2, gray) / 0.10, 1.0)

    # Rectilinearity: fraction of strong gradients aligned to the axes.
    # Screen content measures 0.8+; natural images sit near the 0.33 baseline.
    rectilinearity = _rectilinearity(cv2, gray)

    # Color simplicity: quantize to 32x32 and count unique colors
    small = cv2.resize(img, (32, 32))
    unique = len(set(map(tuple, small.reshape(-1, 3))))
    color_simplicity = 1.0 - (unique / (32 * 32))

    # Uniform regions (slide/terminal backgrounds); busy demos score low
    # here, which is why this carries the smallest weight
    uniformity = max(0.0, 1.0 - float(np.std(gray)) / 80.0)

    return (edge_component * 0.4 + rectilinearity * 0.25
            + color_simplicity * 0.2 + uniformity * 0.15)


def _edge_density(cv2, gray) -> float:
    edges = cv2.Canny(gray, 50, 150)
    return float(np.sum(edges > 0)) / edges.size


def _rectilinearity(cv2, gray) -> float:
    """Fraction of strong-gradient pixels within 15° of horizontal/vertical."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    strong = np.hypot(gx, gy) > 60
    if int(strong.sum()) < 500:
        return 0.0
    ang = np.arctan2(np.abs(gy[strong]), np.abs(gx[strong]))
    to_axis = np.minimum(ang, np.pi / 2 - ang)
    return float(np.mean(to_axis < np.radians(15)))
