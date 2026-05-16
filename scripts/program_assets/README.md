# program_assets/

Vendored partner logos used by `build_cv_cache.py` (§ Phase 3a) when it emits per-program QR codes.

Each subdirectory is named for a `<program-slug>` matching the directory under `truesight_me/programs/`, and contains a `logo.png` that gets composited into the **centre** of the QR code generated for any CV participating in that program.

## Layout

```
program_assets/
├── README.md                   (this file)
├── registry.json               maps data-side cv.programs[] keys → URL-side <program-slug>
├── tribomirim/
│   └── logo.png
├── butterfly-effect/
│   └── logo.png
└── <future-program-slug>/
    └── logo.png
```

## Adding a new program

1. Partner sends the logo (or extract from their site). Pre-process so:
   - Format: PNG.
   - Aspect: as close to square as the source allows; cropped landscape is OK but the QR centre will be a wider-than-tall strip.
   - Background: transparent (`RGBA`) is best; solid white works.
   - Size: ≥256px on the long edge. Larger is fine — `qr_code.py::generate_qr_with_logo` thumbnails via Lanczos before compositing.
2. Commit to `program_assets/<url-program-slug>/logo.png`. The `<url-program-slug>` MUST match the directory name under `truesight_me/programs/` and the `program_slug` field on that program's `truesight_me` `manifest.json`.
3. Register the program in `registry.json` so `build_cv_cache.py` can map the data-side `cv.programs[]` key (often longer / legacy, e.g. `capoeira-tribo-mirim`) to the URL-side slug (e.g. `tribomirim`). If the two slugs are identical, still register it — the entry signals "this program is officially onboarded".
4. Next `build_cv_cache.py` run picks it up automatically. Force a rebuild with `gh workflow run build-cv-cache.yml` on `lineage-credentials` if you need it immediately.

## registry.json schema

```json
{
  "schema_version": 1,
  "programs": {
    "<url-program-slug>": {
      "data_program_slugs": ["<data-program-slug>", "..."]
    }
  }
}
```

`data_program_slugs` is an array so a single URL-side program can absorb multiple data-side slugs (e.g., during a slug migration). Most entries are single-element arrays.

## Why vendored here instead of fetched from truesight_me manifests

The truesight_me-side `programs/<slug>/manifest.json::co_brand.partner_logo_url` drives the **web** co-brand strip (where network fetch is fine). The vendored PNG here drives the **QR-centre overlay** during the deterministic build — see `CREDENTIALING_PROGRAM_PAGES.md` §15.3 for the full rationale (build determinism, audit-on-diff, explicit consent licensing).

The two URLs may legitimately diverge — e.g., the manifest URL points at a hi-DPI marketing asset, while the vendored PNG is a smaller, cleanly-square version optimized for QR-centre placement.

## What happens if a logo is missing

If `program_assets/<program-slug>/logo.png` doesn't exist when `build_cv_cache.py` runs, the engine silently skips the per-program QR + PDF for that program on every CV. No error. The partner's program pages still work (canonical credentials artifacts are unaffected); the per-program PDF download just won't be available yet, and the on-page QR (`js/program-shell.js`) falls back to the canonical `<slug>.qr.png` per the spec's graceful-fallback rule (§15.5).

This lets partner onboarding land in stages: program directory + manifest can ship without the QR pipeline being immediately complete, then a follow-up commit vendoring the logo completes the picture.
