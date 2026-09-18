"""Trim the flat bands a composited recording leaves above and below its content.

Conference and webinar recordings often place the real content in a centred
band and fill the rest with a branded backdrop, so every extracted frame — and
so every screenshot in the DOCX — carries two dead stripes separated from the
content by a hard horizontal line.

Detection is per frame, not per video. A single video mixes letterboxed scenes
with full-bleed slides that run edge to edge, so a crop derived from the video
as a whole slices the title off the full-bleed ones. Measured on a 60-frame
keynote run: 44 frames were letterboxed, 14 were full-bleed.

Two signals separate them, both measured on the same frames:

  band row energy / content row energy    letterboxed 0.027   full-bleed 0.50

"Row energy" is the mean absolute horizontal difference within a row. A bar has
almost none even when it carries a vertical gradient, because a gradient varies
down the frame rather than across it — which is what makes this hold up against
the textured backdrops a flat-colour or pure-black test misses entirely.
"""
import numpy as np

# A band qualifies only if its detail is a small fraction of the frame's own
# content. Measured separation is 0.027 against 0.50, so this sits an order of
# magnitude clear of both.
_BAR_ENERGY_RATIO = 0.10

# Rows sampled at each edge to estimate the bar's own level.
_PROBE_ROWS = 12

# A row counts as bar while its energy stays under this share of the content
# level. The band is not flat — a branded backdrop drifts (measured 0.08 at the
# frame edge rising to 0.21 by the boundary) — so the walk cannot key off the
# level at row 0 or it stops a third of the way in. Keying off the content level
# tolerates that drift while still landing at the step where content begins.
_CONTENT_SHARE = 0.10

# Never remove more than this from one side. A larger "bar" is a dark scene or a
# misdetection, and losing a quarter of the frame is worse than keeping a stripe.
_MAX_TRIM_FRACTION = 0.25

# Below this a bar is not worth the re-encode.
_MIN_BAR_FRACTION = 0.02


def _row_energy(cv2, img):
    """Mean absolute horizontal difference per row."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return np.abs(np.diff(gray, axis=1)).mean(axis=1)


def _bar_depth(energy, content_level: float) -> int:
    """How many rows from the start of `energy` belong to a flat band."""
    probe = energy[:_PROBE_ROWS]
    if probe.size == 0:
        return 0
    bar_level = float(np.median(probe))
    if content_level <= 0 or bar_level / content_level > _BAR_ENERGY_RATIO:
        return 0            # the edge is as busy as the content: no bar here
    limit = content_level * _CONTENT_SHARE
    depth = 0
    for value in energy:
        if value > limit:
            break
        depth += 1
    return depth


def detect_bars(cv2, img) -> tuple[int, int]:
    """Rows of flat band at the top and bottom. (0, 0) when there are none."""
    if img is None or img.ndim != 3:
        return 0, 0
    height = img.shape[0]
    if height < 4 * _PROBE_ROWS:
        return 0, 0

    energy = _row_energy(cv2, img)
    # The middle half stands in for "content": it excludes both bands whatever
    # their depth, without needing to know where they end.
    content_level = float(np.median(energy[height // 4:3 * height // 4]))

    top = _bar_depth(energy, content_level)
    bottom = _bar_depth(energy[::-1], content_level)

    cap = int(height * _MAX_TRIM_FRACTION)
    floor = int(height * _MIN_BAR_FRACTION)
    top = 0 if top > cap or top < floor else top
    bottom = 0 if bottom > cap or bottom < floor else bottom
    # A frame that is entirely flat (a fade to black) reads as all bar.
    if top + bottom >= height:
        return 0, 0
    return top, bottom


def trim_letterbox(cv2, img):
    """Frame with its flat bands removed, or None when there is nothing to trim."""
    top, bottom = detect_bars(cv2, img)
    if not top and not bottom:
        return None
    return img[top:img.shape[0] - bottom]
