# TB-Reminder Discord Bot

Automatisiert Territory Battle Phasen-Ankündigungen mit Officer-gesteuerten Spieler-Pings und Statistik-Tracking.

## Ablauf

| Zeitpunkt | Was passiert |
|---|---|
| `/start_tb_bot` | `@everyone Ein neues Territory Battle hat gestartet!` im tw_channel |
| +22h | Officer bekommt DM mit Spielerliste zum Auswählen |
| ↳ Officer wählt & bestätigt | Ausgewählte Spieler werden per DM erinnert |
| ↳ Kein Response nach 1h | `@everyone Bitte denkt dran im Territory Battle zu stationieren!` |
| ↳ Nach dem Reminder | Officer bekommt zweite DM: wer hat **nicht** stationiert? |
| +24h (×5) | Wiederholt sich für Phase 2–6 |
| Nach Phase 6 | Officer bekommt TB-Abschlussbericht per DM (Erinnerungen + Fehlende) |

## Befehle

Alle Befehle erfordern **Administrator-Rechte** oder Eintrag in `MANAGER_IDS`.

| Befehl | Beschreibung |
|---|---|
| `/TBReminder_start` | Startet die TB-Sequenz sofort |
| `/TBReminder_timer start_time: DD.MM.YYYY HH:MM` | Plant den TB-Start zu einem bestimmten Zeitpunkt (Serverzeit) — zeigt Bestätigungsdialog |
| `/TBReminder_resume phase: <1-6> [hours_elapsed: <h>]` | Setzt eine unterbrochene Sequenz fort |
| `/TBReminder_status` | Zeigt Phasenbeginn, Officer-Erinnerungszeit und Phasenende |
| `/TBReminder_cancel` | Bricht einen laufenden Timer oder eine aktive TB-Sequenz ab |
| `/TBReminder_results` | Postet den Abschlussbericht des letzten TBs im aktuellen Kanal |
| `/TBReminder_help` | Zeigt alle Befehle mit Beschreibung direkt in Discord |

### `/TBReminder_resume` — Wann und wie verwenden

Nach einem Server-Neustart oder Container-Absturz läuft die Sequenz nicht mehr. Der Bot erkennt einen unterbrochenen TB beim Start und loggt eine Warnung:

```
⚠️  Unterbrochener TB gefunden! Phase 4 war zuletzt aktiv, ~2h 15min sind vergangen.
    Nutze /resume_tb zum Fortfahren.
```

**Automatisch** (empfohlen — ab dem zweiten Absturz, sobald `phase_started_at` im stats.json vorhanden ist):
```
/TBReminder_resume phase: 4
```

**Manuell** (bei erstem Einsatz oder wenn der Zeitstempel unbekannt ist):
```
/TBReminder_resume phase: 4 hours_elapsed: 2.25
```
`hours_elapsed` = Stunden seit Beginn der aktuellen Phasen-Wartezeit.
Berechnung: `(Phasenlänge in Stunden) - (verbleibende Stunden bis Phasenende)`
Beispiel: Phase 3 endet in 5h → `24 - 5 = 19` → `hours_elapsed: 19`

---

## Setup

### 1. Bot auf Discord erstellen

1. https://discord.com/developers/applications → **New Application**
2. **Bot** → Token kopieren
3. Unter **Privileged Gateway Intents**: **Server Members Intent** aktivieren ⚠️ (Pflicht!)
4. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Mention Everyone`
5. Generierten Link öffnen → Bot zum Server einladen

### 2. IDs herausfinden

Discord **Einstellungen → Erweitert → Entwicklermodus** aktivieren, dann:

- **Kanal-ID**: Rechtsklick auf #tw_channel → *Kanal-ID kopieren*
- **Officer-ID**: Rechtsklick auf den User → *Benutzer-ID kopieren*
- **Rollen-ID**: Servereinstellungen → Rollen → Rechtsklick auf Rolle → *Rollen-ID kopieren*
- **Manager-IDs**: Wie Officer-ID, für jeden weiteren autorisierten User

### 3. Umgebungsvariablen konfigurieren

```
cp .env.example .env
```

| Variable | Beschreibung |
|---|---|
| `DISCORD_TOKEN` | Bot-Token aus dem Discord Developer Portal |
| `TW_CHANNEL_ID` | ID des Kanals für TB-Ankündigungen |
| `OFFICER_ID` | Discord User-ID des Officers der DMs erhält |
| `MEMBER_ROLE_ID` | ID der Rolle deren Mitglieder getrackt werden |
| `MANAGER_IDS` | Komma-separierte User-IDs die Bot-Befehle ausführen dürfen (optional) |
| `DATA_DIR` | Verzeichnis für `stats.json` (Standard: aktuelles Verzeichnis) |
| `BOT_TIMEZONE` | Zeitzone für `/start_tb_timer` (Standard: `Europe/Vienna`) |

### 4. Deployment (Docker / Portainer)

Das Repo enthält ein `Dockerfile` und `tb_reminder.yml` für Portainer.

**Portainer Stack anlegen:**
1. Stacks → Add stack
2. `tb_reminder.yml` einfügen
3. Umgebungsvariablen im "Environment variables" Abschnitt eintragen
4. Deploy

**Update deployen:**
1. Änderungen auf GitHub pushen
2. Portainer → Stack → Editor → **Update the stack**

**Manuell starten:**
```bash
pip install -r requirements.txt
python bot.py
```

---

## Statistik

Der Bot trackt folgende Daten über alle TBs hinweg in `stats.json`:

- Wie oft wurde ein Spieler pro Phase erinnert
- Wie oft hat ein Spieler nicht stationiert
- Gesamtquoten über alle TBs (Erinnerungen %, Fehlzeiten %)

Der Abschlussbericht wird automatisch per DM an den Officer gesendet sobald der Officer nach Phase 6 die Fehlenden-Liste einreicht (oder die Zeit abläuft). Er kann jederzeit auch manuell mit `/start_tb_results` abgerufen werden.

**Hinweis:** Wenn der Bot während eines laufenden TBs neu gestartet wird, gehen nur die In-Memory-Daten der laufenden Phase verloren. Die `stats.json` bleibt vollständig erhalten. Die Sequenz kann mit `/TBReminder_resume` fortgesetzt werden.

---

## Hinweise

- Discord Select-Menüs zeigen max. 25 Einträge — der Bot unterstützt bis zu 50 Spieler über zwei Dropdowns
- Wenn der Officer DMs deaktiviert hat, wird automatisch die generische Kanal-Nachricht gesendet
- Zeitangaben in `/start_tb_timer` sind immer UTC
- Der Bot muss die gesamte TB-Dauer (~6 Tage) durchlaufen → Docker/Portainer auf einem dauerhaft laufenden Server empfohlen
