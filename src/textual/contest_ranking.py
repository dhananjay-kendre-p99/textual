"""Contest ranking helper module.

NEGATIVE CONTROL (Case T-12):
This file contains the substring 'test' as part of the word 'contest'.
The test_file_detector must NOT classify this file as a test file.
Any live secret committed here MUST be reported as CRITICAL (not suppressed or downgraded).
"""

from __future__ import annotations

# Case T-12: Live key in non-test production file with 'test' substring in filename
CONTEST_SECRET_KEY: str = "sk-live-Y8n2Kp9mQ4xR6wT3v7Bb1Dd"


def compute_contest_leaderboard(scores: list[int]) -> list[int]:
    """Sort contest participant scores descending."""
    return sorted(scores, reverse=True)
