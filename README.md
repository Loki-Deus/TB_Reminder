# TB-Reminder Discord Bot

Automatisiert Territory-Battle-Phasenankündigungen. Die Auswahl, wer erinnert wird, läuft in einem gemeinsamen Kanal (`rote-organisation`), nicht mehr per DM an einen einzelnen Officer. Dazu Platoon-Auswertung gegen den Gilden-Roster und Statistik-Tracking.

Läuft im gemeinsamen Stack mit TW-Counter (`docker-compose.yml`) und liest dessen Roster-Datenbank read-only.

## Ablauf

T = Phasenende (Phasen dauern 24 h). Die Offsets stehen in `config.py` (`PLAYER_REMINDER_OFFSET`, `GENERIC_REMINDER_OFFSET`).

| Zeitpunkt | Wo | Was passiert |
|---|---|---|
| `/tbreminder_start` bzw. `_timer` | TB-Kanal (`TB_CHANNEL_ID`) | `@everyone Ein neues Territory Battle hat gestartet!` |
| T − 4 h (20 h nach Start, danach alle 24 h) | `rote-organisation` | Auswahl-Nachricht: Spieler auswählen, die eine DM-Erinnerung bekommen. **Jeder** im Kanal kann auswählen und bestätigen; die aktuelle Auswahl ist für alle sichtbar. |
| ↳ bestätigt | DMs an die Ausgewählten | `Phase N endet bald!`. Die Nachricht in `rote-organisation` wird zum Ergebnis umgeschrieben, inkl. **Namen und Grund** derer, die nicht erreicht wurden. Keine generische Nachricht. |
| ↳ „Überspringen" | TB-Kanal | sofort `@everyone Bitte denkt dran im Territory Battle zu stationieren!` |
| ↳ keine Bestätigung bis T − 1 h | TB-Kanal | dieselbe generische Nachricht |
| direkt danach | `rote-organisation` | „Wer hat NICHT stationiert?"-Auswahl, Frist bis zum nächsten Trigger (nach Phase 6: 22 h) |
| nach Phase 6 | `rote-organisation` | TB-Abschlussbericht (Erinnerungen + Fehlende) |

Die Frist der Auswahl (3 h) ist hart: ein Klick kurz vor Fristende verlängert sie nicht.

## Befehle

**Ablaufsteuerung** — Administrator oder Eintrag in `MANAGER_IDS`:

| Befehl | Beschreibung |
|---|---|
| `/tbreminder_start` | Startet die TB-Sequenz sofort |
| `/tbreminder_timer start_time: DD.MM.YYYY HH:MM` | Plant den TB-Start (Zeit in `BOT_TIMEZONE`, Standard `Europe/Vienna`), mit Bestätigungsdialog |
| `/tbreminder_resume phase: <1-6> [hours_elapsed: <h>]` | Setzt eine unterbrochene Sequenz fort |
| `/tbreminder_status` | Phasenbeginn, Zeitpunkt der Spielerauswahl, generische Erinnerung, Phasenende |
| `/tbreminder_cancel` | Bricht Timer oder aktive Sequenz ab |
| `/tbreminder_results` | Postet den Abschlussbericht des letzten TBs im aktuellen Kanal |
| `/tbreminder_requirements_upload file` | Lädt die Platoon-Anforderungsliste (CSV: `planet, unit_name, required_count, required_relic`) hoch; ersetzt die bestehende mit Bestätigung und Backup |

**Platoons** — für jeden nutzbar, der den Befehl in `rote-organisation` ausführt (Admins/Manager auch in jedem anderen Kanal):

| Befehl | Beschreibung |
|---|---|
| `/tbreminder_planets_check planet` | Fehlbestand pro Einheit eines Planeten (Roster-Abdeckung, nicht der Ingame-Füllstand) |
| `/tbreminder_planets_ping planet` | Wie `planets_check`, mit Ping-Button je Einheit plus „Alle pingen". Der Ping erscheint im TB-Kanal. |
| `/tbreminder_units_check unit [relic]` | Besitzer einer Einheit ab einem Relic-Level. Aus der Liste gezielt Spieler auswählen (z. B. die, die noch nicht stationiert haben) und dann **im TB-Kanal pingen** oder **per DM** („Bitte stationiere …") anschreiben. Nur für den Aufrufer sichtbar. Nicht erreichte Spieler bleiben nach dem DM-Versand ausgewählt, sodass „Im Kanal pingen" der direkte Fallback ist. |
| `/tbreminder_units_ping unit [relic]` | Wie `units_check`, aber alle Besitzer sind vorausgewählt |

Nicht mit `/tw_register` verknüpfte Spieler sind auswählbar; im Kanal werden sie namentlich aufgeführt („bitte manuell informieren"), per DM sind sie nicht erreichbar. DMs aus `units_*` zählen **nicht** in die Statistik.

`/tbreminder_help` zeigt alle Befehle direkt in Discord.

### `/tbreminder_resume`

Nach Neustart oder Absturz läuft die Sequenz nicht von selbst weiter, sofern beim Start kein Zeitstempel in `stats.json` steht; sonst setzt der Bot automatisch fort und meldet das in TB-Kanal und `rote-organisation`. Manuell:

```
/tbreminder_resume phase: 4
/tbreminder_resume phase: 4 hours_elapsed: 2.25
```

`phase` = Phase, deren Erinnerungsauswahl als Nächstes ansteht. `hours_elapsed` = Stunden seit dem letzten Trigger (bzw. TB-Start). Wird weggelassen, rechnet der Bot aus `phase_started_at`.

**Hinweis Offset-Änderung:** Eine bereits laufende Sequenz behält beim Fortsetzen ihren bisherigen Rhythmus (24 h Abstand zum letzten Trigger). Der Offset von 4 h gilt vollständig erst ab einem frischen TB-Start.

## Setup

### 1. Bot auf Discord erstellen

1. https://discord.com/developers/applications → **New Application**
2. **Bot** → Token kopieren
3. **Privileged Gateway Intents**: **Server Members Intent** aktivieren (Pflicht)
4. **OAuth2 → URL Generator**: Scopes `bot`, `applications.commands`; Permissions `Send Messages`, `Mention Everyone`, im Kanal `rote-organisation` zusätzlich `View Channel`, `Read Message History`
5. Link öffnen → Bot einladen

### 2. IDs

Discord **Einstellungen → Erweitert → Entwicklermodus**, dann per Rechtsklick *ID kopieren*: Kanäle, Rolle, Manager.

### 3. Umgebungsvariablen

Im Compose-Stack kommen sie aus der gemeinsamen `.env` (siehe `_env.example`, Abschnitt TB-Reminder); `docker-compose.yml` bildet sie auf die Namen unten ab.

| Variable (Compose-Name) | Beschreibung |
|---|---|
| `DISCORD_TOKEN` (`TB_DISCORD_TOKEN`) | Bot-Token |
| `GUILD_ID` | Discord-Server-ID (geteilt mit TW-Counter) |
| `TB_CHANNEL_ID` | TB-Kanal (tb-territorialschlacht): Startnachricht, generische Erinnerung, Pings. **Umbenannt, früher `TW_CHANNEL_ID`** — in `.env`/Portainer anpassen |
| `ORG_CHANNEL_ID` | `rote-organisation`: Auswahl-Dialoge, Ergebnisse, Bot-Hinweise. **Neu; ersetzt `OFFICER_ID`** |
| `MEMBER_ROLE_ID` (`TB_MEMBER_ROLE_ID`) | Rolle der getrackten Mitglieder (max. 50) |
| `MANAGER_IDS` (`TB_MANAGER_IDS`) | Komma-separierte User-IDs für die Ablaufsteuerung (optional) |
| `DATA_DIR` | Verzeichnis für `stats.json` und `requirements/` (im Compose fest `/app/data`) |
| `BOT_TIMEZONE` | Zeitzone für `/tbreminder_timer` (Standard `Europe/Vienna`) |
| `ROSTER_DB_PATH` | Pfad zu TW-Counters `counters.db` (im Compose fest `/app/roster-data/counters.db`, read-only) |

`OFFICER_ID` wird nicht mehr gelesen und kann aus der `.env` entfernt werden.

### 4. Deployment (Docker / Portainer)

Der Bot läuft als Service `tb-reminder` im gemeinsamen Stack (`docker-compose.yml`). Update: Änderungen nach GitHub pushen, in Portainer den Stack neu deployen (Image wird aus dem Repo neu gebaut).

**Zeitpunkt des Updates:** zwischen zwei TBs, nicht mitten in einer laufenden Sequenz. Views sind im Arbeitsspeicher; ein Neustart während einer offenen Auswahl verwirft sie, und die Recovery setzt erst bei der nächsten Phase wieder an.

Manuell: `pip install -r requirements.txt && python bot.py`

## Statistik

`stats.json` trackt über alle TBs hinweg, wie oft ein Spieler pro Phase erinnert wurde bzw. nicht stationiert hat (Quoten in `/tbreminder_results`). Geschrieben wird atomar. Nur Erinnerungen, die tatsächlich per DM zugestellt wurden, zählen.

## Hinweise

- Discord-Dropdowns fassen 25 Einträge; der Bot zeigt bis zu 50 Spieler über zwei Dropdowns.
- Ist `rote-organisation` nicht erreichbar (falsche ID, fehlende Rechte), fällt der Bot auf die generische Kanal-Nachricht zurück.
- Der Bot muss die gesamte TB-Dauer (~6 Tage) durchlaufen — dauerhaft laufender Host empfohlen.
