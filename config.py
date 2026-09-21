"""
Konfiguration für den TB-Reminder-Bot: Env-Parsing und Konstanten, analog
zu TW-Counters config.py aufgeteilt -- bisher lag das alles direkt in
bot.py. GUILD_ID war dort zusätzlich hartkodiert (discord.Object(id=...))
statt aus der Umgebung gelesen; das ist hier behoben.
"""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _require_int(name: str) -> int:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Pflicht-Env-Var {name} fehlt oder ist leer.")
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"Env-Var {name}={value!r} ist keine gültige ID.")


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = _require_int("GUILD_ID")
TB_CHANNEL_ID = _require_int("TB_CHANNEL_ID")
# Kanal "rote-organisation": hier landen ab jetzt ALLE Auswahl-Dialoge und
# Bot-Hinweise, die vorher als DM an OFFICER_ID gingen (OFFICER_ID entfällt).
ORG_CHANNEL_ID = _require_int("ORG_CHANNEL_ID")
MANAGER_IDS = {int(i) for i in os.getenv("MANAGER_IDS", "").split(",") if i.strip()}
MEMBER_ROLE_ID = _require_int("MEMBER_ROLE_ID")

DATA_DIR = os.getenv("DATA_DIR", ".")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
REQUIREMENTS_DIR = os.path.join(DATA_DIR, "requirements")
REQUIREMENTS_FILE = os.path.join(REQUIREMENTS_DIR, "rote.json")

# Read-only Mount von tw-counters counters.db -- siehe docker-compose.yml.
# Fehlt dieser Pfad (z.B. lokaler Testlauf ohne den Stack), fallen die
# Platoon-Befehle mit einer klaren Fehlermeldung aus, statt abzustürzen
# (siehe roster_read.py).
ROSTER_DB_PATH = os.getenv("ROSTER_DB_PATH")

BOT_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Vienna"))

HOURS = 3600
PHASE_COUNT = 6

# Zeitmodell. T = Phasenende (Phasen dauern 24 h).
#   T - PLAYER_REMINDER_OFFSET   Bot postet die Spielerauswahl in rote-organisation
#   T - GENERIC_REMINDER_OFFSET  generische @everyone-Erinnerung, ABER nur wenn
#                                bis dahin niemand bestätigt hat (Skip oder
#                                Timeout) -- nach einer Bestätigung entfällt sie
# Der Timeout der Auswahl ist die Differenz der beiden Offsets. Vorher war
# das eine einzige Konstante (OFFICER_TIMEOUT = 1 h), die gleichzeitig
# Auswahl-Timeout UND Abstand zur generischen Erinnerung war; das lässt sich
# bei einem Offset > 2 h nicht mehr sinnvoll teilen.
PHASE_LENGTH = 24 * HOURS
PLAYER_REMINDER_OFFSET = 4 * HOURS
GENERIC_REMINDER_OFFSET = 1 * HOURS
PICKER_TIMEOUT = PLAYER_REMINDER_OFFSET - GENERIC_REMINDER_OFFSET

# Abstand zwischen zwei Triggern (= Phasenlänge) und Wartezeit bis zum ersten
# Trigger nach TB-Start. Gilt überall, wo bisher 22 * HOURS / 24 * HOURS
# hartkodiert stand (run_sequence, /tbreminder_resume, /tbreminder_status).
TRIGGER_INTERVAL = PHASE_LENGTH
FIRST_TRIGGER_WAIT = PHASE_LENGTH - PLAYER_REMINDER_OFFSET

# Frist für die "Wer hat NICHT stationiert?"-Auswahl nach Phase 6 (für Phase
# 1-5 endet sie mit dem nächsten Trigger, siehe handle_phase_end). Wert wie
# bisher.
FINAL_FAILED_TIMEOUT = 22 * HOURS


def trigger_wait(phase_index: int) -> int:
    """Wartezeit in Sekunden bis zum Trigger der Phase phase_index (0-5),
    gemessen ab dem vorherigen Trigger bzw. ab TB-Start."""
    return FIRST_TRIGGER_WAIT if phase_index == 0 else TRIGGER_INTERVAL

PHASE_END_MESSAGES = [
    "Phase 1 endet bald!",
    "Phase 2 endet bald!",
    "Phase 3 endet bald!",
    "Phase 4 endet bald!",
    "Phase 5 endet bald!",
    "Phase 6 endet bald, holt nochmal alles raus!",
]

GENERIC_REMINDER = "Bitte denkt dran im Territory Battle zu stationieren!"

# Comlinks roher relic.currentTier-Wert entspricht nicht direkt der im
# Spiel angezeigten Relic-Stufe. Verifiziert in TW-Counters config.py
# (relic_tier_to_display): roher Wert - 2 = echte Stufe. Diese Konstante
# ist eine bewusste, kommentierte Duplikation -- tb-reminder und
# tw-counter sind getrennte Codebasen ohne gemeinsamen Python-Importpfad,
# auch wenn sie jetzt dieselbe Datenbank lesen. Ändert sich der Offset
# jemals (Comlink-API-Eigenschaft, nicht erwartet), muss das hier UND in
# TW-Counters config.py angepasst werden.
_RELIC_DISPLAY_OFFSET = 2
_MIN_RAW_RELIC_TIER = 3  # entspricht der niedrigsten echten Relic-Stufe, Relic 1


def display_relic_to_raw(display_tier: int) -> int:
    return display_tier + _RELIC_DISPLAY_OFFSET


def relic_tier_to_display(raw_relic_tier: int | None) -> int:
    """Umkehrung von display_relic_to_raw() -- für die Anzeige des
    tatsächlichen Relic-Levels eines Besitzers (roster_read.py). 0 für
    "kein Relic" (None oder Rohwert unter _MIN_RAW_RELIC_TIER), identisch
    zu TW-Counters config.relic_tier_to_display()."""
    if raw_relic_tier is None or raw_relic_tier < _MIN_RAW_RELIC_TIER:
        return 0
    return raw_relic_tier - _RELIC_DISPLAY_OFFSET
