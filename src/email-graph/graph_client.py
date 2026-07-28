"""Microsoft Graph API Client (OAuth2 Client Credentials Flow).

Scopes (Application-Level):
  Mail.Read, Mail.ReadWrite, Mail.Send,
  Calendars.Read, Calendars.ReadWrite,
  Chat.Read.All, ChannelMessage.Read.All,
  Files.ReadWrite.All, Sites.Read.All,
  Tasks.ReadWrite.All,
  OnlineMeetingTranscript.Read.All (optional, für Meeting-Transkripte).

Konfig via Umgebungsvariablen: GRAPH_TENANT_ID, GRAPH_CLIENT_ID,
GRAPH_CLIENT_SECRET, GRAPH_USER_EMAIL.
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("taskpilot.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL_TPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


@dataclass
class GraphConfig:
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    user_email: str = ""

    @classmethod
    def from_env(cls) -> "GraphConfig":
        return cls(
            tenant_id=os.environ.get("GRAPH_TENANT_ID", ""),
            client_id=os.environ.get("GRAPH_CLIENT_ID", ""),
            client_secret=os.environ.get("GRAPH_CLIENT_SECRET", ""),
            user_email=os.environ.get("GRAPH_USER_EMAIL", ""),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret and self.user_email)


@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0

    @property
    def is_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - 60


class GraphClient:
    """Async MS Graph API Client mit automatischem Token-Refresh."""

    def __init__(self, config: GraphConfig | None = None):
        self.config = config or GraphConfig.from_env()
        self._token = _TokenCache()
        self._http: httpx.AsyncClient | None = None
        # Objekt-ID (GUID) des konfigurierten Users; für Meeting-Endpunkte nötig,
        # die den UPN nicht akzeptieren. Wird einmalig aufgelöst und gecacht.
        self._user_id: str = ""

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def _get_token(self) -> str:
        if self._token.is_valid:
            return self._token.access_token

        client = await self._ensure_client()
        url = TOKEN_URL_TPL.format(tenant=self.config.tenant_id)
        resp = await client.post(
            url,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token.access_token = data["access_token"]
        self._token.expires_at = time.time() + data.get("expires_in", 3600)
        logger.info("Graph API Token erneuert (gültig für %ds)", data.get("expires_in", 3600))
        return self._token.access_token

    async def _headers(self) -> dict[str, str]:
        token = await self._get_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _calendar_headers(self) -> dict[str, str]:
        """Headers mit Prefer-Timezone für Kalender-Requests (Europe/Zurich)."""
        headers = await self._headers()
        headers["Prefer"] = 'outlook.timezone="Europe/Zurich"'
        return headers

    async def _get(self, path: str, params: dict | None = None, extra_headers: dict | None = None) -> dict:
        client = await self._ensure_client()
        headers = await self._headers()
        if extra_headers:
            headers.update(extra_headers)
        resp = await client.get(f"{GRAPH_BASE}{path}", headers=headers, params=params)
        if resp.status_code == 403:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                pass
            raise PermissionError(
                f"Graph API 403 Forbidden -- die App-Registration braucht passende "
                f"Application Permissions mit Admin Consent. "
                f"Prüfe: Mail.Read, Mail.ReadWrite, Mail.Send, Calendars.Read, Calendars.ReadWrite. "
                f"Detail: {detail}"
            )
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json_body: dict | None = None) -> dict:
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.post(f"{GRAPH_BASE}{path}", headers=headers, json=json_body)
        if resp.status_code == 403:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                pass
            raise PermissionError(
                f"Graph API 403 Forbidden -- fehlende Application Permissions. Detail: {detail}"
            )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def _patch(self, path: str, json_body: dict) -> dict:
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.patch(f"{GRAPH_BASE}{path}", headers=headers, json=json_body)
        if resp.status_code == 403:
            raise PermissionError(
                "Graph API 403 Forbidden -- fehlende Application Permissions mit Admin Consent."
            )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def _delete(self, path: str) -> None:
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.delete(f"{GRAPH_BASE}{path}", headers=headers)
        if resp.status_code == 403:
            raise PermissionError("Graph API 403 Forbidden -- fehlende Permissions.")
        resp.raise_for_status()

    async def _get_text(self, path: str, params: dict | None = None) -> str:
        """GET-Request der Text statt JSON zurückgibt (z.B. VTT-Transkripte)."""
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.get(f"{GRAPH_BASE}{path}", headers=headers, params=params)
        resp.raise_for_status()
        return resp.text

    async def _get_bytes(self, path: str) -> bytes:
        """GET-Request der Binärdaten zurückgibt (z.B. Datei-Downloads).

        Graph API antwortet auf /content-Endpunkte mit 302 Redirect zur
        Pre-Auth-Download-URL. Deshalb follow_redirects=True.
        """
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.get(
            f"{GRAPH_BASE}{path}",
            headers=headers,
            follow_redirects=True,
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.content

    @property
    def _user_path(self) -> str:
        return f"/users/{self.config.user_email}"

    async def _get_user_id(self) -> str:
        """Löst die Objekt-ID (GUID) des konfigurierten Users auf und cached sie.

        Die Online-Meeting-/Transkript-Endpunkte von Graph akzeptieren
        ausschliesslich die GUID, nicht den UPN (``anthony@…``). Braucht die
        Application-Permission ``User.Read.All``. Ist ``user_email`` bereits eine
        GUID, wird sie direkt übernommen.
        """
        if self._user_id:
            return self._user_id
        email = self.config.user_email or ""
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", email):
            self._user_id = email
            return self._user_id
        data = await self._get(f"/users/{email}", {"$select": "id"})
        self._user_id = data.get("id") or email
        return self._user_id

    # ── E-Mail CRUD ──────────────────────────────────────────────

    async def list_folders(self) -> list[dict]:
        """Alle Mail-Ordner des konfigurierten Users."""
        data = await self._get(f"{self._user_path}/mailFolders", {"$top": "100"})
        return data.get("value", [])

    # Well-known-Ordner, die vom Such-Index ausgenommen bleiben (kein Nutzwert,
    # potenziell riesig/rauschig): Junk-E-Mail und Geloeschte Elemente.
    _INDEX_SKIP_WELLKNOWN = {"junkemail", "deleteditems"}

    async def iter_all_mail_folders(self) -> list[dict]:
        """Enumeriert ALLE Mail-Ordner inkl. verschachtelter, ohne Junk/Geloeschte.

        Steigt rekursiv in ``childFolders`` ab und paginiert ueber ``@odata.nextLink``.
        Junk und Geloeschte Elemente werden ueber ihre Well-Known-IDs ausgeschlossen
        (inkl. Unterordner, da nicht abgestiegen wird). ``$select`` wird bewusst NICHT
        gesetzt: v1.0 lehnt ``$select=wellKnownName`` auf ``mailFolders`` mit 400 ab;
        die Default-Antwort enthaelt ``displayName``/``childFolderCount`` ohnehin. Gibt
        ``{id, displayName, wellKnownName}`` zurueck. Nur Lesezugriff.
        """
        # Well-Known-Ordner (Junk/Geloescht) per Namen aufloesen -> deren echte IDs
        # ausschliessen. So ist der Ausschluss unabhaengig von Anzeige-Sprache/Namen.
        skip_ids: set[str] = set()
        for wk in ("junkemail", "deleteditems"):
            try:
                f = await self._get(f"{self._user_path}/mailFolders/{wk}", {"$select": "id"})
                fid = f.get("id")
                if fid:
                    skip_ids.add(fid)
            except Exception as exc:  # noqa: BLE001 - Ordner evtl. nicht vorhanden
                logger.info("Well-Known-Ordner '%s' nicht aufloesbar: %s", wk, exc)

        out: list[dict] = []

        async def _recurse(endpoint: str) -> None:
            data = await self._get(endpoint, {"$top": "100"})
            while True:
                for f in data.get("value", []):
                    fid = f.get("id")
                    if not fid or fid in skip_ids:
                        continue  # Junk/Geloescht inkl. Unterordner (kein Abstieg)
                    wkn = (f.get("wellKnownName") or "").lower()
                    if wkn in self._INDEX_SKIP_WELLKNOWN:
                        continue
                    out.append({
                        "id": fid,
                        "displayName": f.get("displayName"),
                        "wellKnownName": wkn or None,
                    })
                    if (f.get("childFolderCount") or 0) > 0:
                        await _recurse(f"{self._user_path}/mailFolders/{fid}/childFolders")
                nxt = data.get("@odata.nextLink")
                if not nxt:
                    break
                data = await self._get_raw_url(nxt)

        await _recurse(f"{self._user_path}/mailFolders")
        return out

    async def iter_folder_messages(
        self, folder_id: str, page_size: int = 200, max_total: int = 0
    ):
        """Async-Generator ueber ALLE Nachrichten eines Ordners (volle Pagination).

        Folgt echtem ``@odata.nextLink`` (statt ``$skip``, das Graph bei Deep-Paging
        deckelt). Schlankes ``$select=id,receivedDateTime`` -- der volle Body wird
        beim Indexieren ohnehin einzeln via ``get_email`` geladen. ``max_total=0``
        bedeutet unbegrenzt.
        """
        params = {
            "$top": str(page_size),
            "$select": "id,receivedDateTime",
            "$orderby": "receivedDateTime desc",
        }
        data = await self._get(f"{self._user_path}/mailFolders/{folder_id}/messages", params)
        yielded = 0
        while True:
            for m in data.get("value", []):
                yield m
                yielded += 1
                if max_total and yielded >= max_total:
                    return
            nxt = data.get("@odata.nextLink")
            if not nxt:
                return
            data = await self._get_raw_url(nxt)

    async def list_emails(
        self,
        folder: str = "inbox",
        top: int = 20,
        skip: int = 0,
        filter_str: str | None = None,
    ) -> dict:
        """E-Mails aus einem Ordner lesen. Gibt {value, @odata.nextLink} zurück."""
        params: dict[str, str] = {
            "$top": str(top),
            "$skip": str(skip),
            "$orderby": "receivedDateTime desc",
            # ``meetingMessageType`` gehoert zum abgeleiteten Typ ``eventMessage`` und
            # muss per OData-Cast selektiert werden -- ein direktes Select auf der
            # ``message``-Collection liefert sonst 400 (Property nicht auf message).
            # Kommt im Ergebnis trotzdem als Schluessel ``meetingMessageType`` zurueck.
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,"
                       "bodyPreview,categories,inferenceClassification,hasAttachments,"
                       "importance,conversationId,flag,"
                       "microsoft.graph.eventMessage/meetingMessageType",
        }
        if filter_str:
            params["$filter"] = filter_str
        return await self._get(f"{self._user_path}/mailFolders/{folder}/messages", params)

    async def get_email(self, message_id: str) -> dict:
        """Einzelne E-Mail mit Body laden."""
        return await self._get(
            f"{self._user_path}/messages/{message_id}",
            {"$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                        "body,bodyPreview,categories,inferenceClassification,"
                        "hasAttachments,importance,isRead,conversationId"},
        )

    async def get_message_headers(self, message_id: str) -> list[dict]:
        """Internet-Header einer E-Mail als ``[{name, value}, ...]``.

        Fakten aus dem Umschlag (z. B. ``Auto-Submitted`` nach RFC 3834), die im
        normalen ``$select`` bewusst fehlen -- eine Mail traegt typisch 45-75 Header,
        die in einer Listenabfrage die Antwort unnoetig aufblaehen wuerden.
        """
        data = await self._get(
            f"{self._user_path}/messages/{message_id}",
            {"$select": "id,internetMessageHeaders"},
        )
        headers = data.get("internetMessageHeaders")
        return headers if isinstance(headers, list) else []

    async def get_email_attachments(self, message_id: str) -> list[dict]:
        """Anhänge einer E-Mail laden (inkl. ``contentBytes`` für fileAttachments).

        Gibt die Roh-Attachment-Objekte von Graph zurück. Bild-Anhänge enthalten
        ``contentBytes`` (base64), die der Aufrufer auf die Platte schreiben und
        z. B. mit vision_analyze auswerten kann.
        """
        data = await self._get(f"{self._user_path}/messages/{message_id}/attachments")
        return data.get("value", [])

    async def get_email_categories(self, message_id: str) -> dict:
        """Kategorien, Klassifizierung und Gelesen-Status einer E-Mail.

        ``isRead`` wird mitgeliefert, weil ein ``set_categories``-PATCH den Status in
        Exchange auf ``true`` kippt. Wer eine Kategorie nachtraeglich korrigiert, muss
        den vorherigen Zustand kennen, um ihn wiederherzustellen.
        """
        data = await self._get(
            f"{self._user_path}/messages/{message_id}",
            {"$select": "id,categories,inferenceClassification,isRead"},
        )
        return {
            "id": data.get("id"),
            "categories": data.get("categories", []),
            "inferenceClassification": data.get("inferenceClassification"),
            "isRead": data.get("isRead"),
        }

    async def create_draft(
        self,
        subject: str,
        body_html: str,
        to_recipients: list[str],
        cc_recipients: list[str] | None = None,
        reply_to_id: str | None = None,
        reply_all: bool = True,
    ) -> dict:
        """Entwurf im Drafts-Ordner erstellen.

        Bei ``reply_to_id`` wird ein Reply-Entwurf erzeugt: Microsoft Graph
        befuellt Empfaenger und Betreff ("RE: ...") korrekt vor und garantiert die
        Thread-Zugehoerigkeit (gleiche ``conversationId``). Mit ``reply_all=True``
        (Default) nutzen wir ``createReplyAll`` -- wie ein Mensch beim "Allen
        antworten": To = Absender + urspruengliche To-Empfaenger, CC = urspruengliche
        CC. Bei einer 1:1-Mail kollabiert das automatisch auf den Absender. Mit
        ``reply_all=False`` wird ``createReply`` (nur an den Absender) genutzt.
        Wir ueberschreiben die Empfaenger-Defaults NICHT mit eigenen
        ``toRecipients`` -- das hatte zu falschen/fehlenden Empfaengern und (bei
        fehlendem ``reply_to_id``) zu neuen Threads gefuehrt. Wir setzen nur den
        Body und ergaenzen optionale CC-Empfaenger additiv.
        """
        if reply_to_id:
            # Reply-Pfad: Empfaenger-Defaults von createReply(All) uebernehmen,
            # nur Body setzen (+ optional CC ergaenzen). Betreff bleibt der
            # Reply-Default ("RE: ..."), Thread-Zugehoerigkeit garantiert.
            reply_action = "createReplyAll" if reply_all else "createReply"
            reply_message: dict = {
                "body": {"contentType": "HTML", "content": body_html},
            }
            if cc_recipients:
                reply_message["ccRecipients"] = [
                    {"emailAddress": {"address": addr}} for addr in cc_recipients
                ]

            # Exchange Online kippt bei createReply den isRead-Status der
            # Originalmail auf true, sofern diese kurz zuvor (≈10 Min.) von
            # einer App ge-PATCHt wurde (z.B. set_categories in der Triage).
            # Es gibt keinen Header dagegen -- darum den Zustand vorher merken
            # und nachher wiederherstellen, falls die Mail ungelesen war.
            was_unread = False
            try:
                original = await self._get(
                    f"{self._user_path}/messages/{reply_to_id}",
                    {"$select": "isRead"},
                )
                was_unread = original.get("isRead") is False
            except Exception:  # noqa: BLE001 - Restore ist Best-Effort
                logger.warning(
                    "Konnte isRead-Status vor createReply nicht lesen "
                    "(message_id=%s)",
                    reply_to_id,
                )

            try:
                draft = await self._post(
                    f"{self._user_path}/messages/{reply_to_id}/{reply_action}",
                    {"message": reply_message},
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                # createReplyAll quittiert Exchange gelegentlich mit 400 (z.B. wenn
                # keine weiteren "Allen antworten"-Empfaenger existieren). Dann
                # deterministisch auf createReply (nur Absender) ausweichen, statt
                # den Entwurf scheitern zu lassen.
                if status == 400 and reply_action == "createReplyAll":
                    logger.warning(
                        "createReplyAll mit 400 fehlgeschlagen (message_id=%s) -- "
                        "Fallback auf createReply",
                        reply_to_id,
                    )
                    draft = await self._post(
                        f"{self._user_path}/messages/{reply_to_id}/createReply",
                        {"message": reply_message},
                    )
                else:
                    # 404 (z.B. CC-only-Mail nicht im Postfach) ist nicht reparierbar
                    # -- sauber weiterreichen, damit der Worker als Task fortfaehrt.
                    raise

            if was_unread:
                try:
                    await self.mark_as_unread(reply_to_id)
                except Exception:  # noqa: BLE001 - darf Draft nicht scheitern lassen
                    logger.warning(
                        "Konnte Originalmail nach createReply nicht wieder als "
                        "ungelesen markieren (message_id=%s)",
                        reply_to_id,
                    )
            return draft

        # Neue Mail (kein Reply): Empfaenger explizit setzen.
        message: dict = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in to_recipients
            ],
        }
        if cc_recipients:
            message["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc_recipients
            ]
        return await self._post(f"{self._user_path}/messages", message)

    async def send_draft(self, message_id: str) -> None:
        """Existierenden Entwurf versenden."""
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.post(
            f"{GRAPH_BASE}{self._user_path}/messages/{message_id}/send",
            headers=headers,
        )
        resp.raise_for_status()

    async def update_draft(
        self,
        message_id: str,
        subject: str | None = None,
        body_html: str | None = None,
        to_recipients: list[str] | None = None,
        cc_recipients: list[str] | None = None,
    ) -> dict:
        """Bestehenden Entwurf aktualisieren (Betreff, Body, Empfaenger)."""
        patch_body: dict = {}
        if subject is not None:
            patch_body["subject"] = subject
        if body_html is not None:
            patch_body["body"] = {"contentType": "HTML", "content": body_html}
        if to_recipients is not None:
            patch_body["toRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in to_recipients
            ]
        if cc_recipients is not None:
            patch_body["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc_recipients
            ]
        if not patch_body:
            return {}
        return await self._patch(
            f"{self._user_path}/messages/{message_id}",
            patch_body,
        )

    async def delete_message(self, message_id: str) -> None:
        """E-Mail oder Entwurf löschen."""
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.delete(
            f"{GRAPH_BASE}{self._user_path}/messages/{message_id}",
            headers=headers,
        )
        resp.raise_for_status()

    async def mark_as_read(self, message_id: str) -> None:
        """E-Mail als gelesen markieren."""
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.patch(
            f"{GRAPH_BASE}{self._user_path}/messages/{message_id}",
            headers=headers,
            json={"isRead": True},
        )
        resp.raise_for_status()

    async def mark_as_unread(self, message_id: str) -> None:
        """E-Mail als ungelesen markieren."""
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.patch(
            f"{GRAPH_BASE}{self._user_path}/messages/{message_id}",
            headers=headers,
            json={"isRead": False},
        )
        resp.raise_for_status()

    async def set_categories(self, message_id: str, categories: list[str]) -> dict:
        """Outlook-Kategorien auf einer E-Mail setzen (ersetzt bestehende)."""
        return await self._patch(
            f"{self._user_path}/messages/{message_id}",
            {"categories": categories},
        )

    async def get_or_create_folder(
        self, display_name: str, parent_folder: str = "inbox"
    ) -> dict:
        """Mail-Subfolder suchen. Gibt {id, displayName} zurück.

        Erstellt KEINE neuen Ordner. Wirft ValueError wenn nicht gefunden.
        """
        parent_path = (
            f"{self._user_path}/mailFolders/{parent_folder}/childFolders"
        )
        data = await self._get(
            parent_path,
            {"$filter": f"displayName eq '{display_name}'", "$top": "1"},
        )
        folders = data.get("value", [])
        if folders:
            return {"id": folders[0]["id"], "displayName": folders[0]["displayName"]}

        raise ValueError(
            f"Ordner '{display_name}' existiert nicht unter {parent_folder}. "
            "Neue Ordner duerfen nicht automatisch erstellt werden."
        )

    async def move_to_folder(self, message_id: str, folder_name: str) -> dict:
        """E-Mail in einen bestehenden Subfolder verschieben."""
        folder = await self.get_or_create_folder(folder_name)
        return await self._post(
            f"{self._user_path}/messages/{message_id}/move",
            {"destinationId": folder["id"]},
        )

    async def archive_email(self, message_id: str) -> dict:
        """E-Mail in den Outlook-Archiv-Ordner verschieben (Well-Known Folder)."""
        return await self._post(
            f"{self._user_path}/messages/{message_id}/move",
            {"destinationId": "archive"},
        )

    async def get_conversation_messages(
        self, conversation_id: str, top: int = 10
    ) -> list[dict]:
        """Alle Nachrichten einer Konversation (Thread) chronologisch."""
        try:
            data = await self._get(
                f"{self._user_path}/messages",
                {
                    "$filter": f"conversationId eq '{conversation_id}'",
                    "$top": str(top),
                    "$select": "id,subject,from,toRecipients,receivedDateTime,"
                               "bodyPreview,body,conversationId",
                },
            )
            msgs = data.get("value", [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                logger.warning(
                    "get_conversation_messages $filter fehlgeschlagen (400), Fallback auf $search"
                )
                data = await self._get(
                    f"{self._user_path}/messages",
                    {
                        "$search": f'"conversationId:{conversation_id}"',
                        "$top": str(top),
                        "$select": "id,subject,from,toRecipients,receivedDateTime,"
                                   "bodyPreview,body,conversationId",
                    },
                )
                msgs = data.get("value", [])
            else:
                raise
        msgs.sort(key=lambda m: m.get("receivedDateTime", ""))
        return msgs

    async def search_sender_emails(
        self, sender_email: str, top: int = 5
    ) -> list[dict]:
        """Letzte E-Mails eines bestimmten Absenders (neueste zuerst)."""
        try:
            data = await self._get(
                f"{self._user_path}/messages",
                {
                    "$filter": f"from/emailAddress/address eq '{sender_email}'",
                    "$top": str(top),
                    "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId",
                },
            )
            msgs = data.get("value", [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                logger.warning(
                    "search_sender_emails $filter fehlgeschlagen (400), Fallback auf $search"
                )
                data = await self._get(
                    f"{self._user_path}/messages",
                    {
                        "$search": f'"from:{sender_email}"',
                        "$top": str(top),
                        "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId",
                    },
                )
                msgs = data.get("value", [])
            else:
                raise
        msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
        return msgs

    async def search_my_replies_to(
        self, recipient_email: str, top: int = 3
    ) -> list[dict]:
        """Letzte vom Owner GESENDETE E-Mails an einen bestimmten Empfänger.

        Liest aus dem Ordner "Gesendete Elemente" (sentitems) und filtert auf
        Empfänger == recipient_email. Dient als Stil-Anker: So schreibt Anthony
        wirklich an genau diesen Kontakt (Ton, Länge, Schlussformel).
        Neueste zuerst.
        """
        select = (
            "id,subject,toRecipients,sentDateTime,bodyPreview,body,conversationId"
        )
        # $search ist der Primaerweg: Der frueher zuerst versuchte
        # ``toRecipients/any(...)``-$filter wird von Graph auf der messages-
        # Collection nicht unterstuetzt und scheiterte deshalb praktisch immer mit
        # 400 (tausende Warnungen + doppelte Requests). Direkt $search spart den
        # nutzlosen Erst-Request.
        data = await self._get(
            f"{self._user_path}/mailFolders/sentitems/messages",
            {
                "$search": f'"to:{recipient_email}"',
                "$top": str(top),
                "$select": select,
            },
        )
        msgs = data.get("value", [])
        msgs.sort(key=lambda m: m.get("sentDateTime", ""), reverse=True)
        return msgs

    async def list_sent_messages(self, top: int = 200, skip: int = 0) -> list[dict]:
        """Zuletzt GESENDETE E-Mails (Ordner ``sentitems``), neueste zuerst.

        Grundlage fuer den Style-Store: Anthonys eigene Antworten werden lokal
        indexiert und pro Draft als Few-Shot-Stil-Anker abgerufen. Liefert Roh-
        Nachrichten inkl. ``body`` und ``toRecipients``.
        """
        data = await self._get(
            f"{self._user_path}/mailFolders/sentitems/messages",
            {
                "$top": str(top),
                "$skip": str(skip),
                "$orderby": "sentDateTime desc",
                "$select": "id,subject,toRecipients,sentDateTime,bodyPreview,body,conversationId",
            },
        )
        return data.get("value", [])

    async def search_emails(self, query: str, top: int = 5) -> list[dict]:
        """Volltextsuche über alle E-Mails (Graph $search)."""
        data = await self._get(
            f"{self._user_path}/messages",
            {
                "$search": f'"{query}"',
                "$top": str(top),
                "$select": "id,subject,from,receivedDateTime,bodyPreview,conversationId,webLink",
            },
        )
        return data.get("value", [])

    async def list_flagged_emails(self, top: int = 20, since_days: int = 180) -> list[dict]:
        """Markierte E-Mails (Outlook-Fahne gesetzt) laden, nur aus den letzten since_days Tagen."""
        import datetime as _dt
        since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = await self._get(
                f"{self._user_path}/messages",
                {
                    "$filter": f"flag/flagStatus eq 'flagged' and receivedDateTime ge {since}",
                    "$top": str(top),
                    "$select": "id,subject,from,receivedDateTime,bodyPreview,"
                               "flag,categories,importance,hasAttachments,conversationId",
                },
            )
        except Exception:
            data = await self._get(
                f"{self._user_path}/messages",
                {
                    "$filter": "flag/flagStatus eq 'flagged'",
                    "$top": "100",
                    "$select": "id,subject,from,receivedDateTime,bodyPreview,"
                               "flag,categories,importance,hasAttachments,conversationId",
                },
            )
        msgs = data.get("value", [])
        msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
        return msgs[:top]

    # ── Kalender CRUD ────────────────────────────────────────────

    @staticmethod
    def _ensure_tz_offset(s: str) -> str:
        """Stellt sicher, dass ein ISO-Datetime-String einen Zeitzonen-Offset trägt.

        Microsoft Graph interpretiert `startDateTime`/`endDateTime` der
        calendarView-Abfrage **ohne** Offset als UTC — der
        `Prefer: outlook.timezone`-Header wirkt nur auf die Antwort, nicht auf
        die Query. Ohne Offset entsteht so eine Verschiebung (z.B. 16:00 lokal
        wird als 16:00 UTC = 18:00 Zürich gelesen), wodurch reale Termine
        ausserhalb des Fensters landen und fälschlich als frei gelten.

        Naive Strings werden daher in Europe/Zurich lokalisiert (DST-sicher) und
        mit explizitem Offset versehen.
        """
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        clean = s.strip()
        # Bereits ein Offset (Z oder ±HH:MM nach dem Datums-Teil)?
        if clean.endswith("Z") or "+" in clean[10:] or "-" in clean[10:]:
            return clean
        try:
            naive = _dt.fromisoformat(clean)
        except ValueError:
            return clean
        zurich = naive.replace(tzinfo=ZoneInfo("Europe/Zurich"))
        return zurich.isoformat()

    async def list_events(
        self,
        start: str,
        end: str,
        top: int = 50,
    ) -> list[dict]:
        """Termine in einem Zeitraum (ISO 8601 datetime strings).

        Zeiten werden in Europe/Zurich zurückgegeben (Prefer-Header).
        """
        tz_header = {"Prefer": 'outlook.timezone="Europe/Zurich"'}
        data = await self._get(
            f"{self._user_path}/calendarView",
            {
                "startDateTime": self._ensure_tz_offset(start),
                "endDateTime": self._ensure_tz_offset(end),
                "$top": str(top),
                "$orderby": "start/dateTime",
                "$select": "id,subject,start,end,location,isAllDay,isCancelled,"
                           "organizer,attendees,bodyPreview,showAs,importance,"
                           "categories,sensitivity,isOrganizer",
            },
            extra_headers=tz_header,
        )
        return data.get("value", [])

    async def get_event(self, event_id: str) -> dict:
        """Einzelnen Kalender-Eintrag laden (Zeiten in Europe/Zurich)."""
        tz_header = {"Prefer": 'outlook.timezone="Europe/Zurich"'}
        return await self._get(
            f"{self._user_path}/events/{event_id}",
            {
                "$select": "id,subject,start,end,location,body,isAllDay,"
                           "organizer,attendees,showAs,importance,recurrence",
            },
            extra_headers=tz_header,
        )

    async def create_event(
        self,
        subject: str,
        start: str,
        end: str,
        body: str | None = None,
        is_all_day: bool = False,
        location: str | None = None,
        show_as: str = "busy",
        categories: list[str] | None = None,
    ) -> dict:
        """Neuen Termin / Zeitblocker erstellen."""
        tz = "Europe/Zurich"
        event: dict = {
            "subject": subject,
            "start": {"dateTime": start, "timeZone": tz},
            "end": {"dateTime": end, "timeZone": tz},
            "isAllDay": is_all_day,
            "showAs": show_as,
        }
        if body:
            event["body"] = {"contentType": "HTML", "content": body}
        if location:
            event["location"] = {"displayName": location}
        if categories:
            event["categories"] = categories
        return await self._post(f"{self._user_path}/events", event)

    async def update_event(self, event_id: str, **fields) -> dict:
        """Termin-Felder aktualisieren."""
        tz = "Europe/Zurich"
        patch: dict = {}
        if "subject" in fields:
            patch["subject"] = fields["subject"]
        if "start" in fields:
            patch["start"] = {"dateTime": fields["start"], "timeZone": tz}
        if "end" in fields:
            patch["end"] = {"dateTime": fields["end"], "timeZone": tz}
        if "show_as" in fields:
            patch["showAs"] = fields["show_as"]
        if "body" in fields:
            patch["body"] = {"contentType": "HTML", "content": fields["body"]}
        return await self._patch(f"{self._user_path}/events/{event_id}", patch)

    async def delete_event(self, event_id: str) -> None:
        """Termin löschen."""
        await self._delete(f"{self._user_path}/events/{event_id}")

    @staticmethod
    def _parse_dt(s: str) -> "datetime":
        """ISO-8601-String parsen — naive und UTC-Zeiten als Europe/Zurich behandeln.

        Mit Prefer-Header liefert Graph naive Strings in Zurich-Zeit.
        Ohne Header käme "Z"-Suffix (UTC). Beides wird zu naiven Datetimes
        normalisiert, da alle Zeiten konsistent in Europe/Zurich sind.
        """
        from datetime import datetime as dt
        clean = s.replace("Z", "")
        if "." in clean:
            clean = clean.rstrip("0").rstrip(".")
        if "+" in clean[10:] or clean.endswith(("-00:00", "-01:00", "-02:00")):
            clean = clean.rsplit("+", 1)[0].rsplit("-", 1)[0]
        return dt.fromisoformat(clean)

    async def find_free_slots(
        self,
        start: str,
        end: str,
        duration_minutes: int = 60,
    ) -> list[dict]:
        """Freie Zeitfenster berechnen (Lücken zwischen Terminen).

        Alle Zeiten in Europe/Zurich (via Prefer-Header in list_events).
        """
        from datetime import timedelta
        events = await self.list_events(start, end, top=100)
        busy = []
        for ev in events:
            if ev.get("isCancelled") or ev.get("showAs") == "free":
                continue
            s = ev.get("start", {}).get("dateTime", "")
            e = ev.get("end", {}).get("dateTime", "")
            if s and e:
                busy.append((self._parse_dt(s), self._parse_dt(e)))
        busy.sort()

        range_start = self._parse_dt(start)
        range_end = self._parse_dt(end)
        duration = timedelta(minutes=duration_minutes)

        free = []
        cursor = range_start
        for bs, be in busy:
            if cursor + duration <= bs:
                free.append({
                    "start": cursor.isoformat(),
                    "end": bs.isoformat(),
                    "duration_minutes": int((bs - cursor).total_seconds() / 60),
                    "timezone": "Europe/Zurich",
                })
            cursor = max(cursor, be)
        if cursor + duration <= range_end:
            free.append({
                "start": cursor.isoformat(),
                "end": range_end.isoformat(),
                "duration_minutes": int((range_end - cursor).total_seconds() / 60),
                "timezone": "Europe/Zurich",
            })
        return free

    # ── Teams Chat ────────────────────────────────────────────────

    async def list_chats(self, top: int = 20) -> list[dict]:
        """Alle 1:1- und Gruppen-Chats des Users (neueste zuerst)."""
        data = await self._get(
            f"{self._user_path}/chats",
            {
                "$top": str(top),
                "$orderby": "lastMessagePreview/createdDateTime desc",
                "$expand": "lastMessagePreview",
                "$select": "id,topic,chatType,lastMessagePreview,createdDateTime",
            },
        )
        return data.get("value", [])

    async def list_chat_messages(self, chat_id: str, top: int = 20) -> list[dict]:
        """Letzte Nachrichten eines Chats (neueste zuerst)."""
        data = await self._get(
            f"/chats/{chat_id}/messages",
            {"$top": str(top)},
        )
        return data.get("value", [])

    async def get_chat_message(self, chat_id: str, message_id: str) -> dict:
        """Einzelne Chat-Nachricht laden."""
        return await self._get(f"/chats/{chat_id}/messages/{message_id}")

    async def list_chat_members(self, chat_id: str) -> list[dict]:
        """Teilnehmer eines Chats."""
        data = await self._get(f"/chats/{chat_id}/members")
        return data.get("value", [])

    # ── Online Meetings / Transkripte ────────────────────────────

    async def list_recent_transcripts(self, start: str, end: str) -> list[dict]:
        """Alle Meeting-Transkripte, bei denen der konfigurierte User Organisator ist.

        Nutzt die ``getAllTranscripts``-Funktion (tenant-weit, GUID-basiert).
        ``start``/``end`` sind ISO-8601-Zeitpunkte und filtern auf den
        Erstellungszeitpunkt des Transkript-Artefakts. Jeder Eintrag enthält
        u. a. ``id``, ``meetingId``, ``meetingOrganizer``, ``createdDateTime``
        und die absolute ``transcriptContentUrl``.
        """
        uid = await self._get_user_id()
        fn = (
            f"/users/{uid}/onlineMeetings/getAllTranscripts("
            f"meetingOrganizerUserId='{uid}',startDateTime={start},endDateTime={end})"
        )
        data = await self._get(fn)
        return data.get("value", [])

    async def get_online_meeting(self, meeting_id: str) -> dict:
        """Metadaten eines Online-Meetings (Betreff, Start/Ende, Teilnehmer)."""
        uid = await self._get_user_id()
        return await self._get(f"/users/{uid}/onlineMeetings/{meeting_id}")

    async def list_meeting_transcripts(self, meeting_id: str) -> list[dict]:
        """Transkripte eines Online-Meetings auflisten (GUID-basiert)."""
        uid = await self._get_user_id()
        data = await self._get(
            f"/users/{uid}/onlineMeetings/{meeting_id}/transcripts",
        )
        return data.get("value", [])

    async def get_meeting_transcript_content(
        self, meeting_id: str, transcript_id: str
    ) -> str:
        """Transkript-Inhalt als VTT-Text laden (GUID-basiert)."""
        uid = await self._get_user_id()
        return await self._get_text(
            f"/users/{uid}/onlineMeetings/{meeting_id}"
            f"/transcripts/{transcript_id}/content",
            {"$format": "text/vtt"},
        )

    async def get_transcript_content(self, content_url: str) -> str:
        """Transkript-Inhalt über die absolute ``transcriptContentUrl`` laden.

        Fällt bei deaktivierter Speaker-Attribution automatisch auf das
        unattribuierte Textformat zurück (``SpeakerAttributionNotAllowed``).
        """
        client = await self._ensure_client()
        headers = await self._headers()
        url = content_url if content_url.startswith("http") else f"{GRAPH_BASE}{content_url}"
        resp = await client.get(
            url, headers=headers, params={"$format": "text/vtt"},
            follow_redirects=True, timeout=120.0,
        )
        if resp.status_code == 403:
            resp = await client.get(
                url, headers=headers,
                params={"$format": "application/vnd.microsoft.graph.transcript+text"},
                follow_redirects=True, timeout=120.0,
            )
        resp.raise_for_status()
        return resp.text

    # ── OneDrive / SharePoint Files ──────────────────────────────

    async def list_drive_items(self, path: str = "/", top: int = 20) -> list[dict]:
        """Inhalte eines OneDrive-Ordners auflisten."""
        if path == "/":
            endpoint = f"{self._user_path}/drive/root/children"
        else:
            clean = path.strip("/")
            endpoint = f"{self._user_path}/drive/root:/{clean}:/children"
        data = await self._get(
            endpoint,
            {
                "$top": str(top),
                "$select": "id,name,size,lastModifiedDateTime,file,folder,webUrl,"
                           "parentReference",
            },
        )
        return data.get("value", [])

    async def get_drive_item(self, item_id: str) -> dict:
        """Metadaten eines einzelnen OneDrive-Elements."""
        return await self._get(f"{self._user_path}/drive/items/{item_id}")

    async def download_drive_item(self, item_id: str) -> bytes:
        """Datei-Inhalt als Bytes herunterladen."""
        return await self._get_bytes(
            f"{self._user_path}/drive/items/{item_id}/content"
        )

    async def search_drive(self, query: str, top: int = 10) -> list[dict]:
        """Volltextsuche über OneDrive-Dateien."""
        data = await self._get(
            f"{self._user_path}/drive/root/search(q='{query}')",
            {
                "$top": str(top),
                "$select": "id,name,size,lastModifiedDateTime,file,folder,webUrl,"
                           "parentReference",
            },
        )
        return data.get("value", [])

    async def _get_raw_url(self, url: str) -> dict:
        """GET auf eine absolute Graph-URL (z. B. ``@odata.nextLink``)."""
        client = await self._ensure_client()
        headers = await self._headers()
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def walk_drive_files(self, *, max_files: int = 100000) -> list[dict]:
        """Rekursiv ALLE Datei-Items in OneDrive auflisten (ohne Ordner selbst).

        Paginiert über ``@odata.nextLink`` und steigt in Unterordner ab. Best-effort-
        Cap ``max_files`` gegen Runaway. Gibt driveItem-Objekte mit
        ``id,name,size,lastModifiedDateTime,file,webUrl,parentReference`` zurück.
        Nur Lesezugriff (Files.Read(.All)). Für den semantischen Such-Index gedacht.
        """
        select = "id,name,size,lastModifiedDateTime,file,folder,webUrl,parentReference"
        out: list[dict] = []
        stack: list[str] = [f"{self._user_path}/drive/root/children"]
        while stack and len(out) < max_files:
            endpoint = stack.pop()
            data = await self._get(endpoint, {"$top": "200", "$select": select})
            while True:
                for item in data.get("value", []):
                    if item.get("folder"):
                        stack.append(f"{self._user_path}/drive/items/{item['id']}/children")
                    elif item.get("file"):
                        out.append(item)
                        if len(out) >= max_files:
                            break
                nxt = data.get("@odata.nextLink")
                if nxt and len(out) < max_files:
                    data = await self._get_raw_url(nxt)
                else:
                    break
        return out

    async def get_drive_item_thumbnail(self, item_id: str) -> str | None:
        """Kleine Vorschau-URL (Thumbnail) eines OneDrive-Items, falls vorhanden."""
        try:
            data = await self._get(
                f"{self._user_path}/drive/items/{item_id}/thumbnails",
                {"$select": "medium,small"},
            )
        except Exception:  # noqa: BLE001 - best-effort, kein Thumbnail ist ok
            return None
        for entry in data.get("value", []):
            for size in ("medium", "small", "large"):
                thumb = entry.get(size)
                if thumb and thumb.get("url"):
                    return thumb["url"]
        return None

    async def get_search_region(self) -> str | None:
        """Ermittelt die für die Microsoft Search API (app-only) gültige ``region``.

        Im App-only-Modus ist ``region`` Pflicht. Wir bestimmen sie zero-config und
        cachen das Ergebnis (auch negativ) instanzweit:

        1. **Multi-Geo-Tenant:** ``siteCollection.dataLocationCode`` (z. B. "CHE").
        2. **Single-Geo-Tenant** (Feld leer): eine Probe-Anfrage ``/search/query``
           ohne Region -> Graph antwortet ``400`` mit *"Only valid regions are X"*;
           daraus parsen wir die tatsächliche Region (z. B. "EMEA").

        Gibt None zurück, wenn nichts ermittelbar ist (dann greift der Namens-Fallback).
        """
        if getattr(self, "_search_region_resolved", False):
            return self._search_region
        self._search_region = None

        # 1) Multi-Geo: expliziter dataLocationCode
        try:
            data = await self._get("/sites/root", {"$select": "siteCollection"})
            code = (data.get("siteCollection") or {}).get("dataLocationCode")
            if code:
                self._search_region = code
        except Exception:  # noqa: BLE001
            pass

        # 2) Single-Geo: Region aus der Graph-Fehlermeldung ableiten. Wichtig: Es muss
        #    eine BEWUSST UNGÜLTIGE Region gesendet werden. Ohne Region antwortet Graph
        #    nur "Region is required ..." (ohne Liste); mit ungültiger Region dagegen
        #    "Requested region ZZZ not found. Only valid regions are EMEA." -> parsebar.
        if not self._search_region:
            try:
                await self._post("/search/query", {"requests": [{
                    "entityTypes": ["driveItem"],
                    "query": {"queryString": "probe"},
                    "region": "ZZZ",
                    "from": 0,
                    "size": 1,
                }]})
            except httpx.HTTPStatusError as exc:
                m = re.search(r"valid regions are ([A-Za-z]+)", exc.response.text or "")
                if m:
                    self._search_region = m.group(1)
            except Exception:  # noqa: BLE001
                pass

        self._search_region_resolved = True
        if self._search_region:
            logger.info("Search-API-Region ermittelt: %s", self._search_region)
        return self._search_region

    async def search_query(
        self,
        query: str,
        *,
        entity_types: list[str] | None = None,
        region: str = "",
        top: int = 20,
        include_private: bool = True,
    ) -> list[dict]:
        """Microsoft Search API (``POST /search/query``) -- Cross-Entity-Suche.

        APP-ONLY-HINWEIS: Mit Application Permissions sind ausschliesslich die
        EntityTypes ``site/list/listItem/drive/driveItem`` unterstützt (NICHT
        ``message``/``event`` -- die laufen über ``search_emails``) und ``region``
        ist PFLICHT. Für privaten OneDrive-Content muss
        ``sharePointOneDriveOptions.includeContent=privateContent`` gesetzt sein.
        Liefert Hits inkl. ``summary`` (Snippet mit Highlight ``<c0>…</c0>``).
        """
        ent = entity_types or ["driveItem"]
        req: dict = {
            "entityTypes": ent,
            "query": {"queryString": query},
            "from": 0,
            "size": top,
        }
        if region:
            req["region"] = region
        if include_private and any(e in ("driveItem", "drive", "list", "listItem") for e in ent):
            req["sharePointOneDriveOptions"] = {"includeContent": "privateContent,sharedContent"}
        data = await self._post("/search/query", {"requests": [req]})
        hits: list[dict] = []
        for resp in data.get("value", []):
            for container in resp.get("hitsContainers", []):
                hits.extend(container.get("hits", []))
        return hits

    async def list_sites(self, search: str = "") -> list[dict]:
        """SharePoint-Sites auflisten oder durchsuchen."""
        params: dict[str, str] = {"$top": "20"}
        if search:
            params["search"] = search
        data = await self._get("/sites", params)
        return data.get("value", [])

    # ── Microsoft Planner ────────────────────────────────────────

    async def list_planner_tasks(self, top: int = 30) -> list[dict]:
        """Eigene Planner-Aufgaben des Users."""
        data = await self._get(
            f"{self._user_path}/planner/tasks",
            {"$top": str(top)},
        )
        return data.get("value", [])

    async def get_planner_task(self, task_id: str) -> dict:
        """Einzelne Planner-Aufgabe mit Details."""
        return await self._get(f"/planner/tasks/{task_id}")

    async def get_planner_task_details(self, task_id: str) -> dict:
        """Erweiterte Details (Beschreibung, Checkliste) einer Planner-Aufgabe."""
        return await self._get(f"/planner/tasks/{task_id}/details")

    async def create_planner_task(
        self,
        plan_id: str,
        title: str,
        bucket_id: str | None = None,
        due_date: str | None = None,
        assignments: dict | None = None,
    ) -> dict:
        """Neue Aufgabe in einem Planner-Plan erstellen."""
        body: dict = {"planId": plan_id, "title": title}
        if bucket_id:
            body["bucketId"] = bucket_id
        if due_date:
            body["dueDateTime"] = due_date
        if assignments:
            body["assignments"] = assignments
        return await self._post("/planner/tasks", body)

    async def update_planner_task(
        self, task_id: str, etag: str, **fields
    ) -> dict:
        """Planner-Aufgabe aktualisieren (erfordert @odata.etag für Concurrency)."""
        client = await self._ensure_client()
        headers = await self._headers()
        headers["If-Match"] = etag
        patch: dict = {}
        if "title" in fields:
            patch["title"] = fields["title"]
        if "percent_complete" in fields:
            patch["percentComplete"] = fields["percent_complete"]
        if "due_date" in fields:
            patch["dueDateTime"] = fields["due_date"]
        if not patch:
            return {}
        resp = await client.patch(
            f"{GRAPH_BASE}/planner/tasks/{task_id}",
            headers=headers,
            json=patch,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def list_planner_plans(self) -> list[dict]:
        """Alle Planner-Pläne des Users."""
        data = await self._get(f"{self._user_path}/planner/plans")
        return data.get("value", [])

    # ── Lifecycle ────────────────────────────────────────────────

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
