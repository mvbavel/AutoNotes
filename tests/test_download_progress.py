"""Tests for Teams/SharePoint download progress reporting.

Gap this covers: _run_download matched yt-dlp's "N%" but only fed it to
progress_cb, logging nothing — so a multi-minute Teams download showed a silent
log. Worse, for DASH the "%" is a share of a still-forming size estimate and is
non-monotonic; these are real captured lines where it reads 0.2 -> 0.1 -> 0.0
while the estimate grows 347KiB -> 226MiB. "(frag N/462)" is the trustworthy
signal.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.teams_downloader import _DownloadProgress

# Verbatim yt-dlp 2026.07.04 output for a SharePoint recording.
EARLY = [
    "[dashsegments] Total fragments: 462",
    "[download] Destination: /tmp/vid.fdash-vcopy.mp4",
    "[download]   0.2% of ~ 347.40KiB at    964.10B/s ETA Unknown (frag 0/462)",
    "[download]   0.1% of ~ 694.80KiB at    964.10B/s ETA Unknown (frag 1/462)",
    "[download]   0.0% of ~ 226.59MiB at    1.71KiB/s ETA Unknown (frag 1/462)",
]
MID = "[download]   6.9% of ~ 326.80MiB at    1.82MiB/s ETA 03:10 (frag 32/462)"


def run(lines):
    """Feed lines through the reporter, capturing both callbacks."""
    logs, pcts = [], []
    rep = _DownloadProgress(log_cb=logs.append, progress_cb=pcts.append)
    for ln in lines:
        rep.feed(ln)
    return logs, pcts


def frag_line(cur, total=462, pct=None, speed="1.82MiB/s", eta="03:10"):
    pct = pct if pct is not None else cur / total * 100
    return (f"[download]  {pct:.1f}% of ~ 326.80MiB at    {speed} "
            f"ETA {eta} (frag {cur}/{total})")


class TestProgressLogging(unittest.TestCase):
    def test_emits_progress_lines_to_the_log(self):
        """The actual complaint: nothing appeared in the log."""
        logs, _ = run(EARLY + [frag_line(i) for i in range(1, 463)])
        progress = [m for m in logs if "frag" in m and "%" in m]
        self.assertTrue(progress, "no progress lines reached the log")

    def test_log_line_carries_pct_fragments_speed_and_eta(self):
        logs, _ = run(EARLY + [frag_line(231)])   # 231/462 is exactly half
        line = [m for m in logs if "frag 231/462" in m][-1]
        self.assertIn("50%", line)
        self.assertIn("frag 231/462", line)
        self.assertIn("1.82MiB/s", line)
        self.assertIn("03:10", line)

    def test_throttled_not_one_line_per_update(self):
        """462 fragments must not become 462 log lines."""
        logs, _ = run(EARLY + [frag_line(i) for i in range(1, 463)])
        progress = [m for m in logs if "frag" in m and "%" in m]
        self.assertLessEqual(len(progress), 25)
        self.assertGreaterEqual(len(progress), 15)

    def test_labels_video_and_audio_streams(self):
        """DASH downloads video then audio; the log should say which."""
        logs, _ = run([
            "[download] Destination: /tmp/vid.fdash-vcopy.mp4",
            frag_line(46),
            "[download] Destination: /tmp/vid.fdash-audcopy.m4a",
            frag_line(46, total=100),
        ])
        joined = " ".join(logs)
        self.assertIn("video", joined)
        self.assertIn("audio", joined)


class TestPercentSource(unittest.TestCase):
    def test_prefers_fragments_over_unreliable_percent(self):
        """frag 0/462 is 0% — not the 0.2% yt-dlp claims against a bad estimate."""
        _, pcts = run(EARLY)
        self.assertTrue(pcts)
        self.assertEqual(pcts[0], 0)

    def test_falls_back_to_percent_when_no_fragments(self):
        """Progressive (non-DASH) downloads have no frag counter."""
        _, pcts = run([
            "[download] Destination: /tmp/vid.mp4",
            "[download]  50.0% of 226.59MiB at 1.82MiB/s ETA 01:00",
        ])
        self.assertEqual(pcts[-1], 40)  # 50% of the stage's 0-80 band

    def test_never_exceeds_stage_ceiling(self):
        _, pcts = run(EARLY + [frag_line(i) for i in range(1, 463)])
        self.assertTrue(pcts)
        self.assertLessEqual(max(pcts), 80)
        self.assertEqual(pcts[-1], 80)

    def test_never_goes_backwards_across_streams(self):
        """The frag counter restarts for audio; the bar must not rewind."""
        lines = ["[download] Destination: /tmp/vid.fdash-vcopy.mp4"]
        lines += [frag_line(i) for i in range(1, 463)]
        lines += ["[download] Destination: /tmp/vid.fdash-audcopy.m4a"]
        lines += [frag_line(i, total=50) for i in range(1, 51)]
        _, pcts = run(lines)
        self.assertEqual(pcts, sorted(pcts))


class TestPassThrough(unittest.TestCase):
    def test_non_progress_lines_still_logged(self):
        logs, _ = run([
            "[dashsegments] Total fragments: 462",
            "[Merger] Merging formats into /tmp/vid.mp4",
        ])
        self.assertIn("[dashsegments] Total fragments: 462", logs)
        self.assertIn("[Merger] Merging formats into /tmp/vid.mp4", logs)

    def test_debug_lines_suppressed(self):
        logs, _ = run(["[debug] Command-line config: [...]"])
        self.assertEqual(logs, [])

    def test_blank_lines_ignored(self):
        logs, pcts = run(["", "   "])
        self.assertEqual(logs, [])
        self.assertEqual(pcts, [])


if __name__ == "__main__":
    unittest.main()
