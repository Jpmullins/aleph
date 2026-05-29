"""artificialanalysis.ai connector.

`output_kind = dataset_rows`. The connector returns benchmark rows
shaped `{model, metric, value, date}` for use by ChartCard / TableCard.
Stored as a `Dataset` (kind `tabular`) + `DatasetVersion` snapshot.
"""

from __future__ import annotations

import hashlib
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel

from aleph_connectors.base import (
    ConnectorContext,
    ConnectorResult,
    NotSupported,
    RawPayload,
    SearchQuery,
)

_API = "https://api.artificialanalysis.ai"


class ArtificialAnalysisMetadata(BaseModel):
    metric: str
    model: str | None = None
    date: str | None = None


class ArtificialAnalysisConnector:
    kind: ClassVar[str] = "artificialanalysis"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "dataset_rows"
    requires_auth: ClassVar[bool] = True
    metadata_schema: ClassVar[type[BaseModel]] = ArtificialAnalysisMetadata

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=30.0)

    async def search(self, ctx: ConnectorContext, query: SearchQuery) -> list[ConnectorResult]:
        if not ctx.credential_value:
            msg = "artificialanalysis.ai requires an API key"
            raise NotSupported(msg)
        # Endpoint shape: /v2/data/llms/models — returns benchmark rows.
        resp = await self._http.get(
            f"{_API}/v2/data/llms/models",
            headers={"x-api-key": ctx.credential_value},
            params={"include": "evaluations"},
        )
        if resp.status_code != 200:
            msg = f"artificialanalysis search failed: {resp.status_code}"
            raise NotSupported(msg)
        body = resp.json()
        # Each "result" represents the model+benchmark snapshot.
        rows: list[ConnectorResult] = []
        for model in body.get("data", []):
            model_id = model.get("id") or model.get("slug") or ""
            rows.append(
                ConnectorResult(
                    external_id=f"aa:{model_id}",
                    title=f"artificialanalysis: {model_id}",
                    url=f"https://artificialanalysis.ai/models/{model_id}",
                    snippet=model.get("description"),
                    metadata={
                        "model_id": model_id,
                        "name": model.get("name"),
                        "evaluations": model.get("evaluations", []),
                    },
                )
            )
        return rows

    async def fetch(self, ctx: ConnectorContext, result: ConnectorResult) -> RawPayload:
        # `dataset_rows` connectors don't return a single document; the
        # caller invokes `extract_rows` to turn the search result into
        # observation rows. We still implement `fetch` for the Protocol,
        # returning a thin JSON snapshot.
        import json

        data = json.dumps(result.metadata or {}, indent=2).encode("utf-8")
        return RawPayload(
            data=data,
            mime_type="application/json",
            sha256=hashlib.sha256(data).hexdigest(),
            extension="json",
            declared_metadata=result.metadata or {},
        )

    @staticmethod
    def extract_rows(result: ConnectorResult) -> list[dict[str, Any]]:
        """Flatten a ConnectorResult into `(model, metric, value, date)` rows."""
        meta = result.metadata or {}
        model_id = meta.get("model_id") or meta.get("name") or "unknown"
        rows: list[dict[str, Any]] = []
        for ev in meta.get("evaluations", []) or []:
            if not isinstance(ev, dict):
                continue
            rows.append(
                {
                    "model": model_id,
                    "metric": ev.get("benchmark") or ev.get("metric") or "unknown",
                    "value": ev.get("score") or ev.get("value"),
                    "date": ev.get("date") or ev.get("evaluated_at"),
                }
            )
        return rows
