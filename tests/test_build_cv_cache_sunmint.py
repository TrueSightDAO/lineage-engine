"""Unit tests for the SunMint activity kind in scripts/build_cv_cache.py (no network).

Covers plan CRF_ANAPU_SUNMINT_COHORT_PROPOSAL.md sections 2.2 points 3-4 and
section 6 decision #4: a per-program sunmint/ folder folds into clean
tree/monitoring/plot counts, a tree-only contributor still resolves a
primary_program, and program_modes tolerates both manifest shapes.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_cv_cache import (  # noqa: E402
    _program_modes,
    build_unified_cv,
    collect_practitioners,
    program_activity_score,
    render_markdown,
)


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _seed_program(root: Path, slug: str, pk: str, *, sunmint=None, practice=None, manifest_extra=None):
    """Build a minimal lineage-credentials tree: programs/<slug>/<pk>/..."""
    pdir = root / "programs" / slug / pk
    manifest = {
        "program": slug,
        "display_name": slug.replace("-", " ").title(),
        "source_pages": ["https://example.org"],
    }
    manifest.update(manifest_extra or {})
    _write(root / "programs" / slug / "manifest.json", manifest)
    _write(pdir / "identity.json", {"names": ["Test Student"]})
    for i, e in enumerate(sunmint or []):
        _write(pdir / "sunmint" / f"msg_{i}.json", e)
    for i, e in enumerate(practice or []):
        _write(pdir / "practice" / f"msg_{i}.json", e)


def test_program_modes_array_and_legacy_string():
    assert _program_modes({"program_modes": ["cohort_credentialing", "sunmint_cohort"]}) == [
        "cohort_credentialing",
        "sunmint_cohort",
    ]
    assert _program_modes({"program_mode": "cohort_credentialing"}) == ["cohort_credentialing"]
    assert _program_modes({}) == []


def test_sunmint_activity_counts_by_kind(tmp_path):
    _seed_program(
        tmp_path,
        "crf-anapu",
        "pk-abc",
        sunmint=[
            {"activity_type": "tree_planting", "submitted_at": "2026-09-10T10:00:00Z"},
            {"activity_type": "tree_planting", "submitted_at": "2026-09-11T10:00:00Z"},
            {"activity_type": "tree_growth_monitoring", "submitted_at": "2026-09-12T10:00:00Z"},
            {"activity_type": "farm_boundary_evidence", "submitted_at": "2026-09-09T10:00:00Z"},
        ],
    )
    prac = collect_practitioners(tmp_path)
    rec = prac["pk-abc"]["programs"]["crf-anapu"]
    assert rec["trees_planted_count"] == 2
    assert rec["monitoring_events_count"] == 1
    assert rec["plots_registered_count"] == 1
    assert rec["sunmint_event_count"] == 4
    assert rec["last_sunmint_activity_at"] == "2026-09-12T10:00:00Z"
    # practice semantics untouched
    assert rec["practice_count"] == 0
    assert rec["total_practice_minutes"] == 0


def test_sunmint_carries_program_modes_from_manifest(tmp_path):
    _seed_program(
        tmp_path,
        "crf-anapu",
        "pk-abc",
        sunmint=[{"activity_type": "tree_planting", "submitted_at": "2026-09-10T10:00:00Z"}],
        manifest_extra={"program_modes": ["cohort_credentialing", "sunmint_cohort"]},
    )
    prac = collect_practitioners(tmp_path)
    assert prac["pk-abc"]["programs"]["crf-anapu"]["program_modes"] == [
        "cohort_credentialing",
        "sunmint_cohort",
    ]


def test_tree_only_contributor_scores_nonzero():
    # A program with only SunMint activity must outrank an empty one.
    tree_only = {"practice_count": 0, "total_practice_minutes": 0, "sunmint_event_count": 3}
    empty = {"practice_count": 0, "total_practice_minutes": 0, "sunmint_event_count": 0}
    assert program_activity_score(tree_only) > program_activity_score(empty)
    assert program_activity_score(tree_only) == (3, 0)


def test_build_unified_cv_carries_sunmint_fields(tmp_path):
    _seed_program(
        tmp_path,
        "crf-anapu",
        "pk-abc",
        sunmint=[
            {"activity_type": "tree_planting", "submitted_at": "2026-09-10T10:00:00Z"},
        ],
    )
    prac = collect_practitioners(tmp_path)
    cv = build_unified_cv("test-student", prac["pk-abc"], None)
    prog = cv["programs"]["crf-anapu"]
    assert prog["trees_planted_count"] == 1
    assert prog["sunmint_event_count"] == 1
    assert len(prog["sunmint_events"]) == 1
    # has_elective_records must now be True off sunmint alone
    assert cv["has_elective_records"] is True


def test_render_markdown_omits_zero_practice_line_for_sunmint_only(tmp_path):
    _seed_program(
        tmp_path,
        "crf-anapu",
        "pk-abc",
        sunmint=[
            {"activity_type": "tree_planting", "submitted_at": "2026-09-10T10:00:00Z"},
            {"activity_type": "tree_growth_monitoring", "submitted_at": "2026-09-11T10:00:00Z"},
        ],
    )
    prac = collect_practitioners(tmp_path)
    cv = build_unified_cv("test-student", prac["pk-abc"], None)
    md = render_markdown(cv)
    # the nonsensical line the plan warns about must NOT appear
    assert "Total practice time" not in md
    assert "Practice sessions logged" not in md
    # SunMint aggregates + itemized rows must
    assert "Trees planted: **1**" in md
    assert "Monitoring visits: **1**" in md
    assert "### SunMint activity" in md
    assert "tree planting" in md
