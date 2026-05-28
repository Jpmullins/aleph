"""Source-pack exporter — ZIP of raw + normalized source assets."""

from __future__ import annotations

import io
import json
import zipfile


def source_pack_bytes(
    *,
    sources: list[dict],
    raw_blobs: dict[str, bytes],
    normalized_md: dict[str, str],
) -> bytes:
    """Return a ZIP containing manifest, raw assets, normalized markdown.

    `sources` is a list of source metadata dicts (one per Source).
    `raw_blobs` keys = relative path; values = raw bytes.
    `normalized_md` keys = relative path; values = markdown text.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"sources": sources}, indent=2, default=str))
        for path, data in raw_blobs.items():
            zf.writestr(f"raw/{path}", data)
        for path, md in normalized_md.items():
            zf.writestr(f"normalized/{path}", md)
        zf.writestr(
            "LICENSE-NOTES.md",
            "Source license attribution is per the upstream source metadata. "
            "See `manifest.json` for per-source provenance.\n",
        )
    return buf.getvalue()
