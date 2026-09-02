"""Pipedrive CRM API Client (async, httpx-basiert).

Authentifizierung via x-api-token Header.
API v2 fuer Deals/Persons/Activities/Pipelines/Stages,
API v1 fuer Leads/Notes (noch nicht in v2 verfuegbar).
"""

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger("taskpilot.pipedrive")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0


@dataclass
class PipedriveConfig:
    api_token: str = ""
    company_domain: str = "innosmith"

    @classmethod
    def from_env(cls) -> "PipedriveConfig":
        return cls(
            api_token=os.environ.get("TP_PIPEDRIVE_API_TOKEN", ""),
            company_domain=os.environ.get("TP_PIPEDRIVE_DOMAIN", "innosmith"),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_token and self.company_domain)

    @property
    def base_url_v2(self) -> str:
        return f"https://{self.company_domain}.pipedrive.com/api/v2"

    @property
    def base_url_v1(self) -> str:
        return f"https://{self.company_domain}.pipedrive.com/api/v1"


class PipedriveClient:
    """Async Pipedrive API Client mit Rate-Limit-Retry."""

    def __init__(self, config: PipedriveConfig | None = None):
        self.config = config or PipedriveConfig.from_env()
        self._http: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                headers={"x-api-token": self.config.api_token},
                timeout=30.0,
            )
        return self._http

    async def _request(
        self, method: str, url: str, params: dict | None = None, json_body: dict | None = None
    ) -> dict:
        client = await self._ensure_client()
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.request(method, url, params=params, json=json_body)
                if resp.status_code == 429:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning("Pipedrive Rate-Limit (429), Retry in %.1fs", delay)
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                logger.warning("Pipedrive HTTP-Fehler: %s, Retry %d", exc, attempt + 1)
                await asyncio.sleep(RETRY_BASE_DELAY)
        return {}

    async def _get_v2(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", f"{self.config.base_url_v2}{path}", params=params)

    async def _post_v2(self, path: str, body: dict) -> dict:
        return await self._request("POST", f"{self.config.base_url_v2}{path}", json_body=body)

    async def _patch_v2(self, path: str, body: dict) -> dict:
        return await self._request("PATCH", f"{self.config.base_url_v2}{path}", json_body=body)

    async def _delete_v2(self, path: str) -> dict:
        return await self._request("DELETE", f"{self.config.base_url_v2}{path}")

    async def _get_v1(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", f"{self.config.base_url_v1}{path}", params=params)

    async def _post_v1(self, path: str, body: dict) -> dict:
        return await self._request("POST", f"{self.config.base_url_v1}{path}", json_body=body)

    async def _patch_v1(self, path: str, body: dict) -> dict:
        return await self._request("PATCH", f"{self.config.base_url_v1}{path}", json_body=body)

    async def _put_v1(self, path: str, body: dict) -> dict:
        return await self._request("PUT", f"{self.config.base_url_v1}{path}", json_body=body)

    # ── Vollstaendige Listen ─────────────────────────────────

    async def list_all(self, path: str, params: dict | None = None, seite: int = 500) -> list[dict]:
        """Alle Datensaetze eines v2-Endpunkts holen, ueber alle Seiten.

        Die ``list_*``-Methoden liefern bewusst nur eine Seite -- das genuegt fuer
        eine Ansicht im Cockpit. Fuer eine Auswertung ist es die schlimmste Sorte
        Fehler: das Ergebnis sieht vollstaendig aus und ist es nicht. Pipedrive v2
        blaettert per Cursor (``additional_data.next_cursor``); ohne diese Schleife
        endet jede Summe beim Seitenlimit.
        """
        alle: list[dict] = []
        cursor: str | None = None
        while True:
            anfrage = {**(params or {}), "limit": str(seite)}
            if cursor:
                anfrage["cursor"] = cursor
            antwort = await self._get_v2(path, anfrage)
            alle.extend(antwort.get("data") or [])
            cursor = (antwort.get("additional_data") or {}).get("next_cursor")
            if not cursor:
                return alle

    # ── Verbindungstest ──────────────────────────────────────

    async def test_connection(self) -> dict:
        """Verbindung testen via /users/me."""
        data = await self._get_v1("/users/me")
        user = data.get("data", {})
        return {
            "ok": data.get("success", False),
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "company": user.get("company_name", ""),
        }

    # ── Deals ────────────────────────────────────────────────

    async def list_deals(
        self,
        pipeline_id: int | None = None,
        stage_id: int | None = None,
        status: str = "open",
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, str] = {"status": status, "limit": str(limit)}
        if pipeline_id:
            params["pipeline_id"] = str(pipeline_id)
        if stage_id:
            params["stage_id"] = str(stage_id)
        data = await self._get_v2("/deals", params)
        return data.get("data", []) or []

    async def get_deal(self, deal_id: int) -> dict:
        data = await self._get_v2(f"/deals/{deal_id}")
        return data.get("data", {})

    async def create_deal(self, title: str, **kwargs) -> dict:
        body = {"title": title, **kwargs}
        data = await self._post_v2("/deals", body)
        return data.get("data", {})

    async def update_deal(self, deal_id: int, **kwargs) -> dict:
        data = await self._patch_v2(f"/deals/{deal_id}", kwargs)
        return data.get("data", {})

    # ── Leads (v1) ───────────────────────────────────────────

    async def list_leads(self, limit: int = 50) -> list[dict]:
        data = await self._get_v1("/leads", {"limit": str(limit)})
        return data.get("data", []) or []

    async def get_lead(self, lead_id: str) -> dict:
        data = await self._get_v1(f"/leads/{lead_id}")
        return data.get("data", {})

    async def create_lead(self, title: str, **kwargs) -> dict:
        body = {"title": title, **kwargs}
        data = await self._post_v1("/leads", body)
        return data.get("data", {})

    async def update_lead(self, lead_id: str, **kwargs) -> dict:
        data = await self._patch_v1(f"/leads/{lead_id}", kwargs)
        return data.get("data", {})

    # ── Persons ──────────────────────────────────────────────

    async def list_persons(self, limit: int = 50) -> list[dict]:
        data = await self._get_v2("/persons", {"limit": str(limit)})
        return data.get("data", []) or []

    async def get_person(self, person_id: int) -> dict:
        data = await self._get_v2(f"/persons/{person_id}")
        return data.get("data", {})

    async def get_person_v1(self, person_id: int) -> dict:
        """Person via v1-API laden (liefert Profilbild-Daten zuverlässiger)."""
        data = await self._get_v1(f"/persons/{person_id}")
        return data.get("data", {})

    async def create_person(self, name: str, **kwargs) -> dict:
        body = {"name": name, **kwargs}
        data = await self._post_v1("/persons", body)
        return data.get("data", {})

    async def update_person(self, person_id: int, **kwargs) -> dict:
        data = await self._put_v1(f"/persons/{person_id}", kwargs)
        return data.get("data", {})

    # ── Organizations ────────────────────────────────────────

    async def list_organizations(self, limit: int = 50) -> list[dict]:
        data = await self._get_v2("/organizations", {"limit": str(limit)})
        return data.get("data", []) or []

    async def get_organization(self, org_id: int) -> dict:
        data = await self._get_v2(f"/organizations/{org_id}")
        return data.get("data", {})

    async def create_organization(self, name: str, **kwargs) -> dict:
        body = {"name": name, **kwargs}
        data = await self._post_v2("/organizations", body)
        return data.get("data", {})

    # ── Activities ───────────────────────────────────────────

    async def list_activities(
        self,
        done: bool | None = None,
        deal_id: int | None = None,
        person_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, str] = {"limit": str(limit)}
        if done is not None:
            params["done"] = "1" if done else "0"
        if deal_id:
            params["deal_id"] = str(deal_id)
        if person_id:
            params["person_id"] = str(person_id)
        data = await self._get_v2("/activities", params)
        return data.get("data", []) or []

    async def create_activity(self, subject: str, activity_type: str = "task", **kwargs) -> dict:
        body = {"subject": subject, "type": activity_type, **kwargs}
        data = await self._post_v2("/activities", body)
        return data.get("data", {})

    async def update_activity(self, activity_id: int, **kwargs) -> dict:
        data = await self._patch_v2(f"/activities/{activity_id}", kwargs)
        return data.get("data", {})

    async def mark_activity_done(self, activity_id: int) -> dict:
        return await self.update_activity(activity_id, done=True)

    # ── Pipelines & Stages ───────────────────────────────────

    async def list_pipelines(self) -> list[dict]:
        data = await self._get_v2("/pipelines")
        return data.get("data", []) or []

    async def list_stages(self, pipeline_id: int | None = None) -> list[dict]:
        params = {}
        if pipeline_id:
            params["pipeline_id"] = str(pipeline_id)
        data = await self._get_v2("/stages", params or None)
        return data.get("data", []) or []

    # ── Notes (v1) ───────────────────────────────────────────

    async def list_notes(
        self,
        deal_id: int | None = None,
        person_id: int | None = None,
        org_id: int | None = None,
        limit: int = 20,
    ) -> list[dict]:
        params: dict[str, str] = {"limit": str(limit), "sort": "add_time DESC"}
        if deal_id:
            params["deal_id"] = str(deal_id)
        if person_id:
            params["person_id"] = str(person_id)
        if org_id:
            params["org_id"] = str(org_id)
        data = await self._get_v1("/notes", params)
        return data.get("data", []) or []

    async def create_note(self, content: str, **kwargs) -> dict:
        body = {"content": content, **kwargs}
        data = await self._post_v1("/notes", body)
        return data.get("data", {})

    # ── Person Fields ───────────────────────────────────────

    async def list_person_fields(self) -> list[dict]:
        """Alle Person-Felder laden (inkl. Custom-Fields)."""
        data = await self._get_v2("/personFields")
        return data.get("data", []) or []

    async def find_field_key(self, field_name: str) -> str | None:
        """API-Key eines Person-Feldes anhand des Namens finden (case-insensitive)."""
        fields = await self.list_person_fields()
        target = field_name.lower()
        for f in fields:
            if (f.get("name") or "").lower() == target:
                return f.get("key")
        return None

    # ── Person Picture ────────────────────────────────────────

    async def upload_person_picture(
        self, person_id: int, image_data: bytes, filename: str = "photo.jpg"
    ) -> dict:
        """Profilbild fuer Person hochladen (v1 multipart POST)."""
        client = await self._ensure_client()
        url = f"{self.config.base_url_v1}/persons/{person_id}/picture"
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post(
                    url, files={"file": (filename, image_data, "image/jpeg")}
                )
                if resp.status_code == 429:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning("Pipedrive Rate-Limit (429), Retry in %.1fs", delay)
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json().get("data", {})
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                logger.warning("Pipedrive picture upload Fehler: %s, Retry %d", exc, attempt + 1)
                await asyncio.sleep(RETRY_BASE_DELAY)
        return {}

    # ── Suche ────────────────────────────────────────────────

    # Pipedrive v2 itemSearch erwartet SINGULAR item_types. LLMs liefern oft Plural
    # ("deals,persons,organizations") -> 400 Bad Request. Defensiv normalisieren.
    _ITEM_TYPE_ALIASES = {
        "deals": "deal", "persons": "person", "people": "person",
        "organizations": "organization", "organisations": "organization",
        "orgs": "organization", "org": "organization", "leads": "lead",
        "products": "product", "files": "file", "projects": "project",
    }
    _VALID_ITEM_TYPES = {
        "deal", "person", "organization", "product", "lead", "file",
        "mail_attachment", "project",
    }

    @classmethod
    def _normalize_item_types(cls, item_types: str) -> str:
        out: list[str] = []
        for raw in (item_types or "").split(","):
            t = raw.strip().lower()
            if not t:
                continue
            t = cls._ITEM_TYPE_ALIASES.get(t, t)
            if t in cls._VALID_ITEM_TYPES and t not in out:
                out.append(t)
        return ",".join(out) or "deal,person,organization"

    async def search_items(self, term: str, item_types: str = "deal,person,organization", limit: int = 10) -> list[dict]:
        # Term sicherheitshalber kürzen: sehr lange Mehrwort-Suchen liefern bei
        # itemSearch ohnehin keine Treffer und erhöhen nur das Fehlerrisiko.
        clean_term = (term or "").strip()[:120]
        data = await self._get_v2("/itemSearch", {
            "term": clean_term,
            "item_types": self._normalize_item_types(item_types),
            "limit": str(limit),
        })
        items = data.get("data", {})
        if isinstance(items, dict):
            return items.get("items", [])
        return items or []

    async def search_persons_by_email(self, email: str, limit: int = 5) -> list[dict]:
        """Person anhand der E-Mail-Adresse suchen (v1 search_by_email)."""
        data = await self._get_v1("/persons/search", {
            "term": email,
            "search_by_email": "1",
            "limit": str(limit),
        })
        items = data.get("data", {})
        if isinstance(items, dict):
            return items.get("items", [])
        return items if isinstance(items, list) else []

    # ── Pipeline-Summary (aggregiert) ────────────────────────

    async def get_pipeline_summary(self, pipeline_id: int | None = None) -> list[dict]:
        """Kompakte Uebersicht: Stages mit Deal-Counts und Werten."""
        pipelines = await self.list_pipelines()
        if pipeline_id:
            pipelines = [p for p in pipelines if p.get("id") == pipeline_id]

        result = []
        for pl in pipelines:
            pid = pl.get("id")
            stages = await self.list_stages(pid)
            stage_data = []
            for st in stages:
                deals = await self.list_deals(stage_id=st.get("id"), limit=200)
                total_value = sum(d.get("value", 0) or 0 for d in deals)
                stage_data.append({
                    "id": st.get("id"),
                    "name": st.get("name"),
                    "order_nr": st.get("order_nr"),
                    "deal_count": len(deals),
                    "total_value": total_value,
                })
            result.append({
                "id": pid,
                "name": pl.get("name"),
                "stages": stage_data,
            })
        return result
