#!/usr/bin/env python3
"""trend.py — momentum baselines read out of git history. Zero network.

This module owns the pipeline's only git access, the way ghclient.py owns its only
network access: render.py stays pure layout and every impure read lives here.

The daily refresh already commits a full helpers/repos.json snapshot, so git history
holds a daily star series going back months — enough to compute both rank movement and
star growth over a window, with no extra API calls and no new state file to maintain.
One baseline snapshot feeds both, so the two always describe the same span.

Everything here is best-effort by design. A shallow clone, a missing git binary, a
repo younger than the window, or a corrupt blob returns None; render then omits the
affected columns instead of failing. Star counts are never invented.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

REPOS_REL = "helpers/repos.json"
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------------
# timestamps (repos.json uses "2026-07-29T09:20:02Z" throughout)
# --------------------------------------------------------------------------

def parse_ts(iso: str) -> datetime | None:
    try:
        return datetime.strptime(iso, TS_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def shift_days(ts: datetime, days: int) -> str:
    return (ts - timedelta(days=days)).strftime(TS_FMT)


# --------------------------------------------------------------------------
# git plumbing (the only impure code in the render half of the pipeline)
# --------------------------------------------------------------------------

def _git(root, *args: str) -> bytes | None:
    """Run git in `root`; return stdout on success, None on any failure."""
    try:
        p = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _revs(root, rel: str, before: str) -> list[str]:
    out = _git(root, "log", "-1", "--format=%H", f"--before={before}", "--", rel)
    return (out or b"").decode("utf-8", "replace").split()


def _read_snapshot(root, rev: str, rel: str) -> tuple[str, list[dict]] | None:
    """(generated_at, repos) from `rel` at `rev`, or None if unusable."""
    blob = _git(root, "show", f"{rev}:{rel}")
    if not blob:
        return None
    try:
        doc = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    repos = doc.get("repos")
    if not isinstance(repos, list) or not repos:
        return None
    return doc.get("generated_at") or "", repos


# ISO-8601 "…Z" strings sort lexicographically in chronological order, so plain
# string comparison is a correct (and cheaper) staleness check throughout.

def snapshot_at(root, cutoff: str, current_at: str, rel: str = REPOS_REL
                ) -> tuple[str, list[dict]] | None:
    """Newest committed `rel` whose commit date is at or before `cutoff`.

    Returns None when nothing that old is reachable — no history, a depth-1 clone,
    no git — or when the blob found is not actually older than the snapshot being
    rendered (a re-render must never diff a snapshot against itself).
    """
    for rev in _revs(root, rel, before=cutoff):
        snap = _read_snapshot(root, rev, rel)
        if snap and snap[0] and snap[0] < current_at:
            return snap
    return None


# --------------------------------------------------------------------------
# pure delta math
# --------------------------------------------------------------------------

def stars_map(universe: list[dict]) -> dict[str, int]:
    return {r["full_name"]: int(r.get("stars", 0))
            for r in universe if r.get("full_name")}


def rank_map(published: list[dict]) -> dict[str, int]:
    """{full_name: 1-based rank} for an already-selected published set."""
    return {r["full_name"]: i for i, r in enumerate(published, 1)
            if r.get("full_name")}


def top_movers(repos: list[dict], size: int) -> list[dict]:
    """The `size` rows with the largest absolute star gain over the window.

    Ranks on absolute gain (what "trending" means here); ties break on relative
    gain then name, so the order is stable for identical inputs. Rows without a
    baseline carry no gain and are excluded rather than counted as zero.
    """
    movers = [r for r in repos if r.get("delta_stars") is not None]
    movers.sort(key=lambda r: (-r["delta_stars"], -(r.get("pct_stars") or 0.0),
                               r["full_name"]))
    return movers[:size]
