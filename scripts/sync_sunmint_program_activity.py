#!/usr/bin/env python3
"""Sync SunMint program activity from verify_public_signatures into lineage-credentials.

Reads the public, per-event immutable JSON attestation ledger
(TrueSightDAO/verify_public_signatures) and, for every SunMint tree / monitoring /
boundary event whose ``Submission Source`` host matches a registered program
domain (Option B attribution, see ``scripts/sunmint_program_registry.json``),
writes one JSON file per event into::

    lineage-credentials/programs/<slug>/pk-<hash>/sunmint/<msg_id>.json

``sunmint/`` is a NEW, non-``practice`` subfolder name on purpose: reusing the
literal ``practice/`` folder for tree events would pollute capoeira's
``practice_count`` / ``total_practice_minutes`` semantics and render nonsensical
CV text like "Total practice time: 0 minutes" on a farmer's page
(plan CRF_ANAPU_SUNMINT_COHORT_PROPOSAL.md section 1.4 / section 2.2).

Mirrors ``truesight_autopilot:scripts/sync_sunmint_signatures.py`` -- the only
automated precedent on the autopilot box (an every-30-min cron):

* each event file is written with the exact bytes we would upload so the git-blob
  sha can be compared against the remote file's sha -- **idempotent,
  content-addressed skip** (no redundant commits on re-run);
* **dry-run by default** (plan section 2.2 point 2 / standing workspace convention);
  ``--push`` performs the Contents-API PUTs.

Usage::

    python3 scripts/sync_sunmint_program_activity.py --dry-run
    GITHUB_TOKEN=... python3 scripts/sync_sunmint_program_activity.py --push
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# --- Sources -----------------------------------------------------------------
RAW_BASE = (
    "https://raw.githubusercontent.com/TrueSightDAO/verify_public_signatures/main/"
)
VPS_API = "https://api.github.com/repos/TrueSightDAO/verify_public_signatures/contents/"
LC_API = "https://api.github.com/repos/TrueSightDAO/lineage-credentials/contents/"

# SunMint event folders we ingest, and the ``activity_type`` we record for each.
SOURCE_FOLDERS = {
    "tree_planting": "tree_planting",
    "tree_growth_monitoring": "tree_growth_monitoring",
    "farm_boundary_evidence_event": "farm_boundary_evidence",
}

# Real attestations carry the line as a markdown bullet, e.g.
# "- Submission Source: autopilot-sophia" (verified live on
# verify_public_signatures/tree_planting/Edgar_20260910213239_421.json);
# sometimes it is bare. Tolerate an optional leading "-"/"*" bullet.
SUBMISSION_SOURCE_RE = re.compile(
    r"^\s*[-*]?\s*Submission\s*Source\s*:\s*(.+?)\s*$", re.MULTILINE
)
_URL_HOST_RE = re.compile(r"^https?://([^/:]+)", re.IGNORECASE)
REGISTRY_PATH = Path(__file__).with_name("sunmint_program_registry.json")


# --- Pure helpers (unit-tested, no network) ----------------------------------
def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_pk_hash(public_key_b64: str) -> str:
    """Canonical pk-hash: 'pk-' + first 12 chars of base64url(SHA-256(pubkey bytes)).

    MUST match the browser + Python + GAS implementations
    (tokenomics program_admin_endpoint.js `attesteeSlug`).
    """
    if not public_key_b64:
        return ""
    key = public_key_b64.strip()
    # Some writers (the Edgar API submission path) persist the key PEM-armoured
    # ("-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----") rather than
    # as bare base64 SPKI DER. Both encode the SAME DER bytes, so strip the
    # armour before decoding -- otherwise b64decode raises "Incorrect padding"
    # and every such event is silently dropped (0 attributable).
    if "-----BEGIN" in key:
        key = "".join(
            line
            for line in key.splitlines()
            if line.strip() and not line.strip().startswith("-----")
        )
    try:
        decoded = base64.b64decode(key)
    except (ValueError, TypeError):
        return ""
    digest = hashlib.sha256(decoded).digest()
    b64 = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return "pk-" + b64[:12]


def parse_submission_source(signed_text: str) -> str:
    """Return the raw ``Submission Source`` value, or '' when absent."""
    m = SUBMISSION_SOURCE_RE.search(signed_text or "")
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip()


def source_host(value: str) -> str:
    """Normalise a Submission Source value to a lowercase host, when it is a URL.

    A non-URL sentinel (e.g. the legacy hard-coded ``sunmint-limites-da-fazenda``)
    is returned lowercased unchanged so it simply fails the host-registry lookup
    rather than raising.
    """
    if not value:
        return ""
    m = _URL_HOST_RE.match(value.strip())
    if m:
        return m.group(1).lower()
    return value.strip().lower()


def load_registry(path: Path | None = None) -> dict[str, str]:
    """Return {host: program_slug} from the registry (empty on missing/invalid)."""
    p = path or REGISTRY_PATH
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hosts = data.get("hosts") or {}
    return {str(k).lower(): str(v) for k, v in hosts.items()}


def resolve_slug(submission_source: str, registry: dict[str, str]) -> str:
    """Map a Submission Source value to a program slug ('' when unregistered)."""
    return registry.get(source_host(submission_source), "")


def build_activity_record(ev: dict, slug: str, activity_type: str) -> dict | None:
    """Build the per-event ``sunmint/<msg_id>.json`` payload, or None to skip.

    Skips events with no resolvable public_key / pk-hash (can't be attributed to a
    practitioner), mirroring the skip-don't-crash posture of the pattern script.
    """
    public_key = (ev.get("public_key") or "").strip()
    pk_hash = derive_pk_hash(public_key)
    if not pk_hash:
        return None
    msg_id = str(ev.get("telegram_message_id") or "").strip()
    if not msg_id:
        return None
    src = parse_submission_source(
        ev.get("signed_payload") or ev.get("signed_text") or ""
    )
    return {
        "program": slug,
        "activity_type": activity_type,
        "pk_hash": pk_hash,
        "contributor_name": ev.get("contributor_name") or "",
        "submitted_at": ev.get("submitted_at") or "",
        "telegram_message_id": msg_id,
        "submission_source": src,
        "source_host": source_host(src),
        "event_type": ev.get("event_type") or "",
        "linked_tree_id": ev.get("linked_tree_id") or "",
        "public_key": public_key,
        "signature": ev.get("signature") or "",
        "signed_payload": ev.get("signed_payload") or "",
        "record_url": f"{RAW_BASE}{_event_folder(activity_type)}/{msg_id}.json",
    }


def _event_folder(activity_type: str) -> str:
    for folder, kind in SOURCE_FOLDERS.items():
        if kind == activity_type:
            return folder
    return activity_type


def ledger_path(slug: str, pk_hash: str, msg_id: str) -> str:
    return f"programs/{slug}/{pk_hash}/sunmint/{msg_id}.json"


def plan_files(
    events_by_folder: dict[str, list[dict]], registry: dict[str, str]
) -> dict[str, dict]:
    """Return {repo_path: payload} for every attributable event (pure)."""
    files: dict[str, dict] = {}
    for folder, events in events_by_folder.items():
        activity_type = SOURCE_FOLDERS.get(folder, folder)
        for ev in events:
            src = parse_submission_source(
                ev.get("signed_payload") or ev.get("signed_text") or ""
            )
            slug = resolve_slug(src, registry)
            if not slug:
                continue
            rec = build_activity_record(ev, slug, activity_type)
            if rec is None:
                continue
            files[ledger_path(slug, rec["pk_hash"], rec["telegram_message_id"])] = rec
    return files


# --- Network -----------------------------------------------------------------
def _get_json(url: str, token: str | None = None) -> object:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_source_events(token: str | None = None) -> dict[str, list[dict]]:
    """Fetch every event file for each ingested folder from verify_public_signatures."""
    out: dict[str, list[dict]] = {}
    for folder in SOURCE_FOLDERS:
        try:
            listing = _get_json(VPS_API + folder, token)
        except urllib.error.HTTPError:
            out[folder] = []
            continue
        events: list[dict] = []
        if isinstance(listing, list):
            for entry in listing:
                if not isinstance(entry, dict) or not entry.get("name", "").endswith(
                    ".json"
                ):
                    continue
                if entry["name"] == "index.json":
                    continue
                try:
                    ev = _get_json(
                        entry.get("download_url")
                        or RAW_BASE + folder + "/" + entry["name"],
                        token,
                    )
                except urllib.error.HTTPError:
                    continue
                if isinstance(ev, dict):
                    events.append(ev)
        out[folder] = events
    return out


def _git_blob_sha(content: str) -> str:
    raw = content.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def _upload(path: str, payload: dict, token: str) -> bool:
    """PUT one ledger file; True when a new commit was written.

    Content-addressed skip: build the exact bytes, compute their git-blob sha and
    compare with the remote file's sha. Equal -> skip (GitHub's Contents API does
    not reliably no-op identical-content PUTs).
    """
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    local_sha = _git_blob_sha(body)
    remote_sha = None
    try:
        req0 = urllib.request.Request(LC_API + path, method="GET")
        req0.add_header("Authorization", f"Bearer {token}")
        req0.add_header("Accept", "application/vnd.github+json")
        with urllib.request.urlopen(req0, timeout=30) as r0:
            remote_sha = json.load(r0).get("sha")
    except urllib.error.HTTPError:
        pass  # not present yet -- PUT creates it
    if remote_sha == local_sha:
        print(f"[skip] {path} -> already current (blob sha match)")
        return False
    data = json.dumps(
        {
            "message": f"sync(sunmint): {path} (sync_sunmint_program_activity.py)",
            "content": base64.b64encode(body.encode()).decode(),
            "sha": remote_sha,
        }
    ).encode()
    req = urllib.request.Request(LC_API + path, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"[push] {path} -> {json.load(r).get('commit', {}).get('sha', '?')}")
            return True
    except urllib.error.HTTPError as e:
        if e.code == 422:
            print(f"[skip] {path} -> already current (422 unchanged)")
            return False
        raise


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument(
        "--push",
        action="store_true",
        help="Write to lineage-credentials (default: dry-run).",
    )
    p.add_argument("--registry", default=str(REGISTRY_PATH))
    p.add_argument(
        "--max-uploads",
        type=int,
        default=250,
        help="Cap PUTs per run (default 250) to respect GitHub rate limits.",
    )
    args = p.parse_args()

    registry = load_registry(Path(args.registry))
    if not registry:
        sys.exit(f"registry empty or unreadable: {args.registry}")
    print(f"[info] registry: {registry}")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    events_by_folder = fetch_source_events(token)
    for folder, evs in events_by_folder.items():
        print(f"[info] source {folder}: {len(evs)} events")

    files = plan_files(events_by_folder, registry)
    print(f"[info] attributable events -> {len(files)} ledger files")

    if not args.push:
        for path, payload in sorted(files.items()):
            print(
                f"[dry-run] would write {path}  ({payload['contributor_name']} / {payload['submitted_at']})"
            )
        print("[dry-run] nothing written; pass --push to commit.")
        return

    if not token:
        sys.exit("--push needs GITHUB_TOKEN or GH_TOKEN")
    written = 0
    for path in sorted(files):
        if written >= args.max_uploads:
            print(
                f"[info] hit --max-uploads {args.max_uploads}; remaining trickle on next run"
            )
            break
        if _upload(path, files[path], token):
            written += 1
    print(f"[info] done: {written} new/updated file(s)")


if __name__ == "__main__":
    main()
