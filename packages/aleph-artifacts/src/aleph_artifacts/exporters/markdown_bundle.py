"""Markdown-bundle exporter — ZIP of `report.md` + embedded assets."""

from __future__ import annotations

import io
import zipfile


def markdown_bundle_bytes(
    *,
    report_md: str,
    assets: dict[str, bytes] | None = None,
    manifest: dict | None = None,
) -> bytes:
    """Return a ZIP byte stream containing the report and its assets.

    `assets` is a map of relative path (under `assets/`) → bytes.
    `manifest` is written as `manifest.json`.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.md", report_md)
        if manifest is not None:
            import json

            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for path, data in (assets or {}).items():
            zf.writestr(f"assets/{path}", data)
    return buf.getvalue()
