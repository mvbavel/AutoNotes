import os
import json
import subprocess
import numpy as np

from pipeline._paths import FFMPEG, FFPROBE

MAX_FRAMES = 25
INTERVAL_SECONDS = 10


def extract_frames(video_path: str, temp_dir: str, progress_cb=None) -> list[dict]:
    """
    Extract candidate frames from the video, score them for slide-likeness,
    and return the top MAX_FRAMES sorted by timestamp.
    Returns list of {timestamp, path, score}.
    """
    import cv2

    frames_dir = os.path.join(temp_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    duration = _get_duration(video_path)
    if duration <= 0:
        return []

    timestamps = list(range(0, int(duration), INTERVAL_SECONDS))
    candidates = []

    for i, ts in enumerate(timestamps):
        frame_path = os.path.join(frames_dir, f"frame_{ts:06d}.jpg")
        _extract_frame(video_path, ts, frame_path)
        if os.path.exists(frame_path):
            score = _score_frame(frame_path)
            candidates.append({"timestamp": ts, "path": frame_path, "score": score})
        if progress_cb:
            progress_cb(int((i + 1) / len(timestamps) * 100))

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:MAX_FRAMES]
    top.sort(key=lambda x: x["timestamp"])
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


def _extract_frame(video_path: str, timestamp: float, output_path: str):
    cmd = [
        FFMPEG, "-y", "-ss", str(timestamp), "-i", video_path,
        "-vframes", "1", "-q:v", "3", "-vf", "scale=1280:-1",
        output_path
    ]
    subprocess.run(cmd, capture_output=True)


def _score_frame(frame_path: str) -> float:
    """
    Score a frame for slide-likeness.
    Slides have high edge density (text/diagrams) and low color complexity.
    """
    import cv2

    img = cv2.imread(frame_path)
    if img is None:
        return 0.0

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Edge density: slides have lots of crisp text edges
    edges = cv2.Canny(gray, 50, 150)
    edge_density = float(np.sum(edges > 0)) / edges.size

    # Color simplicity: quantize to 32x32 and count unique colors
    small = cv2.resize(img, (32, 32))
    pixels = small.reshape(-1, 3)
    unique = len(set(map(tuple, map(tuple, pixels))))
    color_simplicity = 1.0 - (unique / (32 * 32))

    # Uniform background detection: large areas of near-uniform color
    # Slide backgrounds are typically very uniform
    std_dev = float(np.std(gray))
    uniformity = max(0.0, 1.0 - std_dev / 80.0)

    score = edge_density * 0.6 + color_simplicity * 0.25 + uniformity * 0.15
    return score
