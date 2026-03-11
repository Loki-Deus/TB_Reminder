# TB-Phasen Discord Bot

Automatisiert TB-Phasen-Ankündigungen mit Officer-gesteuerten Spieler-Pings.

## Ablauf

| Zeitpunkt | Was passiert |
|-----------|-------------|
| `/start`  | `@everyone Ein neuer TB hat gestartet!` im tw_channel |
| +22h      | Officer bekommt DM mit Spielerliste zum Auswählen |
| ↳ Officer wählt & bestätigt | Ausgewählte Spieler werden im tw_channel gepingt |
| ↳ Kein Response nach 1h | `@everyone Bitte denkt dran im TB zu stationieren!` |
| +24h (x5) | Wiederholt sich für Phase 2–6 |

## Setup

### 1. Abhängigkeiten installieren
```bash
pip install -r requirements.txt
```

### 2. Bot auf Discord erstellen
1. https://discord.com/developers/applications → **New Application**
2. **Bot** → **Add Bot** → Token kopieren
3. Unter **Privileged Gateway Intents**: **Server Members Intent** aktivieren ⚠️ (Pflicht!)
4. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Mention Everyone`
5. Generierten Link öffnen → Bot zum Server einladen

### 3. IDs herausfinden
Discord **Einstellungen → Erweitert → Entwicklermodus** aktivieren, dann:
- **Kanal-ID**: Rechtsklick auf #tw_channel → *Kanal-ID kopieren*
- **Officer-ID**: Rechtsklick auf den User → *Benutzer-ID kopieren*
- **Rollen-ID**: Servereinstellungen → Rollen → Rechtsklick auf Rolle → *Rollen-ID kopieren*

### 4. `.env` Datei erstellen
```bash
cp .env.example .env
```
Alle vier Werte in `.env` eintragen.

### 5. Bot starten
```bash
python bot.py
```

## Verwendung

`/start` im Discord eingeben (nur Admins).

## Hinweise
- Der Bot muss die gesamte TB-Dauer (~6 Tage) durchlaufen → empfohlen: VPS oder Raspberry Pi
- Discord Select-Menüs zeigen max. 25 Spieler — bei größeren Gilden ggf. anpassen
- Wenn der Officer DMs deaktiviert hat, wird automatisch die generische Nachricht gesendet
