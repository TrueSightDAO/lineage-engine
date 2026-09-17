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


def test_member_sunmint_aggregate_sums_across_programs():
    from scripts.build_cv_cache import member_sunmint_aggregate

    agg = member_sunmint_aggregate(
        {
            "crf-anapu": {
                "trees_planted_count": 2,
                "plots_registered_count": 1,
                "last_sunmint_activity_at": "2026-09-10T10:00:00Z",
            },
            "butterfly-effect": {
                "trees_planted_count": 1,
                "plots_registered_count": 0,
                "last_sunmint_activity_at": "2026-09-12T10:00:00Z",
            },
        }
    )
    assert agg["sunmint_trees_planted"] == 3
    assert agg["sunmint_plots_registered"] == 1
    assert agg["sunmint_last_activity_at"] == "2026-09-12T10:00:00Z"


def test_member_sunmint_aggregate_empty_and_missing_keys():
    from scripts.build_cv_cache import member_sunmint_aggregate

    assert member_sunmint_aggregate({}) == {
        "sunmint_trees_planted": 0,
        "sunmint_plots_registered": 0,
        "sunmint_last_activity_at": "",
    }
    # a program record with no SunMint keys must not raise
    assert member_sunmint_aggregate({"x": {}})["sunmint_trees_planted"] == 0


def test_member_sunmint_aggregate_matches_flat_keys_used_by_renderer():
    """member_sunmint_aggregate() emits exactly the keys program-shell.js reads.

    js/program-shell.js:296-298 reads flat ``sunmint_trees_planted`` /
    ``sunmint_plots_registered`` / ``sunmint_last_activity_at`` off the member
    record. Guard the aggregate's key names + types against renderer drift.
    """
    from scripts.build_cv_cache import member_sunmint_aggregate

    agg = member_sunmint_aggregate({})
    assert set(agg) == {
        "sunmint_trees_planted",
        "sunmint_plots_registered",
        "sunmint_last_activity_at",
    }
    assert agg == {
        "sunmint_trees_planted": 0,
        "sunmint_plots_registered": 0,
        "sunmint_last_activity_at": "",
    }


def test_build_surfaces_flat_sunmint_fields_on_index_members(tmp_path):
    """End-to-end: build() writes flat sunmint_* keys onto _cache/index.json members.

    This is the real PR5 regression guard. The renderer (truesight_me_beta #381 /
    the crf-anapu mirror) reads flat per-member fields off the directory index;
    if build() stops surfacing member_sunmint_aggregate() the badges silently go
    dead. Call build() for real and assert the seeded tree/plot counts land.
    """
    from scripts.build_cv_cache import build

    root = tmp_path / "lineage-credentials"
    _seed_program(
        root,
        "crf-anapu",
        "pk-TESTHASH1",
        sunmint=[
            {"activity_type": "tree_planting", "submitted_at": "2026-09-10T00:00:00Z"},
            {"activity_type": "tree_planting", "submitted_at": "2026-09-11T00:00:00Z"},
            {"activity_type": "farm_boundary_evidence", "submitted_at": "2026-09-12T00:00:00Z"},
        ],
    )
    build(root, write_pdfs=False, write_narratives=False)

    index = json.loads((root / "_cache" / "index.json").read_text(encoding="utf-8"))
    members = [m for m in index["members"] if m.get("display_name") == "Test Student"]
    assert members, "seeded student must appear in the directory index"
    m = members[0]
    # the exact keys + values js/program-shell.js:296-298 renders
    assert m["sunmint_trees_planted"] == 2
    assert m["sunmint_plots_registered"] == 1
    assert m["sunmint_last_activity_at"] == "2026-09-12T00:00:00Z"
    # uniform shape: EVERY member carries the keys (badges read a stable shape)
    assert all("sunmint_trees_planted" in mem for mem in index["members"])
