# Setup: Teams-Meeting-Transkripte via Graph API

**Zweck:** TaskPilot holt Transkripte beendeter Teams-Meetings automatisch ab
(Poller alle 15 Minuten) und speichert das Original (VTT). Die lokale
Zusammenfassung zum strukturierten Protokoll ist **standardmässig aus**: lange
Transkripte belasten das lokale Modell zu stark. Einschalten unter
**Einstellungen → Integrationen** oder **Agenten → Meetings**. Manuell bleibt
«Neu analysieren» und der Anonymisierungs-Pfad (danach öffentliches Modell).

**Zeitkritisch:** Microsoft erzwingt ab **Ende Juli 2026** neue Admin-Controls
für den Graph-Zugriff auf Transkripte und Aufzeichnungen. Ohne die untenstehende
Freigabe antwortet die API mit `403 GraphAccessToTranscriptsDisabled` — auch
wenn die App-Berechtigung selbst erteilt ist.

Alle Schritte sind **einmalig** und müssen von dir (Global Admin / Teams Admin)
ausgeführt werden. TaskPilot selbst braucht danach keine Anpassung.

---

## 1. Application Permission prüfen (Entra ID)

1. [Entra Admin Center](https://entra.microsoft.com) → **App-Registrierungen**
   → TaskPilot-App (Client-ID = `TP_GRAPH_CLIENT_ID`).
2. **API-Berechtigungen** → prüfen, dass vorhanden ist:
   - `OnlineMeetingTranscript.Read.All` (Application)
   - `OnlineMeetings.Read.All` (Application) — für das Auflisten der Meetings
3. Falls neu hinzugefügt: **Administratorzustimmung erteilen** (Grant admin
   consent) nicht vergessen.

## 2. Application Access Policy (PowerShell, einmalig)

Application Permissions auf `onlineMeetings` wirken erst, wenn die App per
Access Policy einem Benutzer zugeordnet ist. In einer PowerShell mit dem
**MicrosoftTeams**-Modul (`Install-Module MicrosoftTeams`):

```powershell
Connect-MicrosoftTeams

# App-ID = Client-ID der TaskPilot-App-Registrierung
New-CsApplicationAccessPolicy `
  -Identity "TaskPilot-Transcripts" `
  -AppIds "<TP_GRAPH_CLIENT_ID>" `
  -Description "TaskPilot: Zugriff auf Online-Meetings und Transkripte"

# Der Policy den Meeting-Organisator zuweisen (Anthonys Konto)
Grant-CsApplicationAccessPolicy `
  -PolicyName "TaskPilot-Transcripts" `
  -Identity "<GRAPH_USER_EMAIL>"
```

Hinweise:
- Die Policy greift nur für Meetings, die der zugewiesene Benutzer
  **organisiert** hat. Für Meetings fremder Organisatoren liefert Graph kein
  Transkript — das ist eine Microsoft-Einschränkung, kein TaskPilot-Fehler.
- Propagation kann bis zu 30 Minuten dauern.

## 3. Teams Admin Center: Graph-Zugriff auf Transkripte freigeben

**Das ist der neue, ab Ende Juli 2026 erzwungene Schritt.**

1. [Teams Admin Center](https://admin.teams.microsoft.com) →
   **Voice & AI** (bzw. **Einstellungen → KI und Aufzeichnungen**, der Menüpunkt
   wurde von Microsoft mehrfach umbenannt) → **Zugriff auf Aufzeichnungen und
   Transkripte über Graph API**.
2. Die TaskPilot-App (Client-ID) in die Liste der zugelassenen Apps aufnehmen
   und den Zugriff auf **Transkripte** erlauben.
3. Speichern. Auch hier: Propagation bis zu einigen Stunden möglich.

Alternativ per PowerShell (Teams-Modul, ab Version 6.x):

```powershell
# Aktuellen Zustand prüfen
Get-CsTeamsAIPolicy

# Falls der Tenant Graph-Zugriff global blockiert:
# im Admin Center die App-spezifische Ausnahme für TaskPilot eintragen.
```

## 4. Voraussetzung pro Meeting: Transkription aktivieren

Graph liefert nur, was Teams erzeugt: Das Meeting muss **Transkription**
(oder Aufzeichnung mit Transkript) aktiviert haben. Empfehlung:

- Teams Admin Center → **Besprechungsrichtlinien** → Transkription erlauben.
- In wichtigen Serien-Meetings die Transkription standardmässig starten.

## 5. Funktionsprüfung

Nach dem Setup (und dem nächsten Meeting mit Transkript):

```bash
# Im Backend-Container: Poller-Log beobachten
docker logs -f taskpilot-backend 2>&1 | grep -i meeting
```

Erwartete Sequenz: `Meeting-Poller: N beendete(s) Meeting(s) gefunden` →
`Transkript gespeichert (…)`. Mit eingeschaltetem Auto-Protokoll folgt
`AgentJob meeting_summary erzeugt`. Das Transkript (und ggf. das Protokoll)
erscheint unter **Agenten → Meetings**.

## Fehlerbilder

| Symptom | Ursache | Behebung |
|---|---|---|
| `403 GraphAccessToTranscriptsDisabled` | Schritt 3 fehlt (Teams Admin Center) | App im Admin Center freigeben |
| `403 Forbidden` auf `/onlineMeetings` | Access Policy fehlt/nicht propagiert | Schritt 2, 30 Min warten |
| Meetings gelistet, aber keine Transkripte | Transkription im Meeting nicht aktiv | Schritt 4 |
| Transkript da, kein Protokoll | Auto-Zusammenfassung ist aus (Default) | Schalter an, oder «Neu analysieren» |
| Fremd organisierte Meetings fehlen | Microsoft-Einschränkung (nur eigene) | erwartetes Verhalten |
