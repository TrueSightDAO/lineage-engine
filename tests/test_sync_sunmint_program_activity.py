"""Unit tests for scripts/sync_sunmint_program_activity.py (no network)."""

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sync_sunmint_program_activity import (  # noqa: E402
    _git_blob_sha,
    build_activity_record,
    derive_pk_hash,
    ledger_path,
    load_registry,
    parse_submission_source,
    plan_files,
    resolve_slug,
    source_host,
)

# A real RSA-2048 SPKI public key (base64) so pk-hash derivation is exercised on
# genuine bytes. Its canonical pk-hash is asserted below.
_REAL_PK = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnyyiWvqyJm/4V4GslBnCDBDQWhsot86xYF0y/fL87Eq9eYdK3S3BZpTw7ZJo9l2/ZF1RD712DvLSlDH2ERszU122wXoqQFV9wbO1Lc+SQCxRjwzcmuKQljmUMmbFakCxTOowUvDX9K1sZdDlF2T0FZpwVIXPbHti/ERnI/8bR+0Sm9DNj6YvuV85JdUPPrPYh5BB7WetlvGmD1vpBCA7m3Jelp8IjK4eVZthDKuveFlg6/fS68nMdZa3uN4e18QAnfWdDOTimtmtEoYO1hkebPTZhkLo6qPdcFw+mlowC1dkoi6jQCXPGgWQjktRh62s3yvPtfDHMuAmKieuEBSJXwIDAQAB"


def test_derive_pk_hash_is_canonical():
    # Copied verbatim from a live lineage-credentials identity.json
    # (programs/butterfly-effect/pk-eIdKUyv9Hc2-/identity.json).
    assert derive_pk_hash(_REAL_PK) == "pk-eIdKUyv9Hc2-"


def test_derive_pk_hash_empty_is_blank():
    assert derive_pk_hash("") == ""


def test_derive_pk_hash_accepts_pem_armoured_key():
    # The Edgar API submission path persists the key PEM-armoured. The SAME DER
    # bytes must hash identically to the bare base64 SPKI form, or the event is
    # silently dropped (0 attributable) during attribution.
    import textwrap

    body = "\n".join(textwrap.wrap(_REAL_PK, 64))
    pem = "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----\n"
    assert derive_pk_hash(pem) == "pk-eIdKUyv9Hc2-"
    assert derive_pk_hash(pem) == derive_pk_hash(_REAL_PK)


def test_parse_submission_source_url():
    text = "[TREE PLANTING EVENT]\n- Latitude: 1.0\nSubmission Source: https://cfr.truesight.me\n--------\n"
    assert parse_submission_source(text) == "https://cfr.truesight.me"


def test_parse_submission_source_sentinel():
    text = "Submission Source: sunmint-limites-da-fazenda\n"
    assert parse_submission_source(text) == "sunmint-limites-da-fazenda"


def test_parse_submission_source_bullet_form():
    # Real events emit the line as a markdown bullet (verified live on
    # verify_public_signatures/tree_planting/Edgar_20260910213239_421.json).
    text = "[TREE PLANTING EVENT]\n- Latitude: 1.0\n- Submission Source: https://cfr.truesight.me/\n--------\n"
    assert parse_submission_source(text) == "https://cfr.truesight.me/"


def test_parse_submission_source_bullet_sentinel_not_attributed():
    # An autopilot/smoke sentinel must not be mis-attributed to a program.
    reg = {"cfr.truesight.me": "crf-anapu"}
    text = "\n- Submission Source: autopilot-sophia\n"
    assert parse_submission_source(text) == "autopilot-sophia"
    assert resolve_slug(parse_submission_source(text), reg) == ""


def test_parse_submission_source_absent():
    assert parse_submission_source("[TREE PLANTING EVENT]\n") == ""


def test_source_host_from_url():
    assert source_host("https://cfr.truesight.me/") == "cfr.truesight.me"
    assert source_host("https://CFR.Truesight.ME/path?x=1") == "cfr.truesight.me"


def test_source_host_from_sentinel_is_lowercased():
    assert source_host("SunMint-Limites") == "sunmint-limites"


def test_load_registry_real_file():
    reg = load_registry()
    assert reg.get("cfr.truesight.me") == "crf-anapu"


def test_resolve_slug_registered_and_unregistered():
    reg = {"cfr.truesight.me": "crf-anapu"}
    assert resolve_slug("https://cfr.truesight.me/", reg) == "crf-anapu"
    assert resolve_slug("https://sunmint.truesight.me/", reg) == ""


def _event(msg_id="123", src="https://cfr.truesight.me/", pk=_REAL_PK):
    return {
        "event_type": "[TREE PLANTING EVENT]",
        "telegram_message_id": msg_id,
        "contributor_name": "Maria Silva",
        "submitted_at": "2026-09-17",
        "public_key": pk,
        "signature": "sig",
        "linked_tree_id": "469027268",
        "signed_payload": f"[TREE PLANTING EVENT]\nSubmission Source: {src}\n--------\n",
    }


def test_build_activity_record_shape():
    rec = build_activity_record(_event(), "crf-anapu", "tree_planting")
    assert rec["program"] == "crf-anapu"
    assert rec["activity_type"] == "tree_planting"
    assert rec["pk_hash"] == "pk-eIdKUyv9Hc2-"
    assert rec["source_host"] == "cfr.truesight.me"
    assert rec["telegram_message_id"] == "123"
    assert rec["record_url"].endswith("tree_planting/123.json")


def test_build_activity_record_skips_without_public_key():
    assert build_activity_record(_event(pk=""), "crf-anapu", "tree_planting") is None


def test_build_activity_record_skips_without_msg_id():
    assert (
        build_activity_record(_event(msg_id=""), "crf-anapu", "tree_planting") is None
    )


def test_plan_files_filters_by_registered_host():
    reg = {"cfr.truesight.me": "crf-anapu"}
    events = {
        "tree_planting": [
            _event(msg_id="1", src="https://cfr.truesight.me/"),
            _event(msg_id="2", src="https://sunmint.truesight.me/"),  # unregistered
            _event(msg_id="3", src="sunmint-limites-da-fazenda"),  # sentinel
        ]
    }
    files = plan_files(events, reg)
    assert list(files) == ["programs/crf-anapu/pk-eIdKUyv9Hc2-/sunmint/1.json"]
    assert (
        files["programs/crf-anapu/pk-eIdKUyv9Hc2-/sunmint/1.json"]["program"]
        == "crf-anapu"
    )


def test_ledger_path_uses_sunmint_subfolder_not_practice():
    p = ledger_path("crf-anapu", "pk-abc", "9")
    assert p == "programs/crf-anapu/pk-abc/sunmint/9.json"
    assert "/practice/" not in p


def test_plan_files_is_deterministic_and_idempotent():
    reg = {"cfr.truesight.me": "crf-anapu"}
    events = {"tree_planting": [_event(msg_id="7")]}
    a = plan_files(events, reg)
    b = plan_files(events, reg)
    assert a == b


def test_git_blob_sha_is_stable_and_sensitive():
    assert _git_blob_sha("{}") == _git_blob_sha("{}")
    assert _git_blob_sha('{"a":1}') != _git_blob_sha('{"a":2}')


def test_registry_file_is_valid_json():
    p = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "sunmint_program_registry.json"
    )
    json.loads(p.read_text())
