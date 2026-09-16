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
TW_CHANNEL_ID = _require_int("TW_CHANNEL_ID")
OFFICER_ID = _require_int("OFFICER_ID")
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
OFFICER_TIMEOUT = 1 * HOURS
PHASE_WAIT_FIRST = 22 * HOURS
PHASE_WAIT_LATER = 24 * HOURS
PHASE_COUNT = 6

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
