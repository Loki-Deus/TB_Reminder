"""
Read-only Zugriff auf TW-Counters counters.db für die Platoon-Befehle
(/tbreminder_requirements_upload, /tbreminder_platoons_check,
/tbreminder_platoons_ping). Gemountet unter config.ROSTER_DB_PATH,
siehe docker-compose.yml -- :ro, tb-reminder schreibt dort nie hin.

Duplikation gegenüber TW-Counters db.py ist hier bewusst in Kauf
genommen: getrennte Codebasen ohne gemeinsamen Python-Importpfad (siehe
config.py's Kommentar zu display_relic_to_raw()). Die Query-Logik selbst
(resolve_unit_id, get_owners_of_unit) ist bewusst so nah wie möglich an
TW-Counters Original (db.get_display_name_to_unit_id_map(),
db.get_owners_of_unit()) gehalten, damit sich das Verhalten hier nicht
unbemerkt von dort entfernt.
"""

import sqlite3
from contextlib import contextmanager

import config


class RosterUnavailableError(RuntimeError):
    """ROSTER_DB_PATH fehlt oder die Datenbank ist nicht erreichbar --
    z.B. lokaler Testlauf ohne den gemeinsamen Stack, der Mount fehlt,
    oder tw-counter hat noch keinen ersten Roster-Refresh abgeschlossen
    (Tabellen existieren dann zwar, sind aber leer -- das ist kein Fehler
    hier, sondern ergibt einfach leere Ergebnislisten)."""


@contextmanager
def get_connection():
    if not config.ROSTER_DB_PATH:
        raise RosterUnavailableError(
            "ROSTER_DB_PATH ist nicht gesetzt -- kein Zugriff auf die "
            "Rosterdaten möglich. Läuft der Bot im gemeinsamen Stack mit "
            "dem read-only Mount (siehe docker-compose.yml)?"
        )
    try:
        # Read-only URI-Verbindung -- zusätzliche Absicherung auf
        # SQLite-Ebene selbst, unabhängig vom :ro-Docker-Mount. Ein Bug
        # hier kann tw-counters Daten damit auf zwei unabhängigen Ebenen
        # nicht verändern, nicht nur einer.
        conn = sqlite3.connect(f"file:{config.ROSTER_DB_PATH}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        raise RosterUnavailableError(
            f"Konnte Rosterdatenbank nicht öffnen ({config.ROSTER_DB_PATH}): {e}"
        ) from e
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def resolve_unit_id(display_name: str) -> str | None:
    """
    Löst einen Anzeigenamen (aus der hochgeladenen Anforderungs-CSV) auf
    die unit_id auf, die in roster_units tatsächlich von den meisten
    Gildenmitgliedern besessen wird -- exakt dieselbe Logik wie
    TW-Counters db.get_display_name_to_unit_id_map() (siehe dortiger
    Docstring zu Nicht-Spieler-Varianten mit identischem Anzeigenamen wie
    Raid-Boss-Formen oder Encounter-Previews, die über den INNER JOIN auf
    roster_units automatisch herausfallen, weil sie nie echte
    Roster-Einträge haben).

    Gibt None zurück, wenn kein Gildenmitglied comlinks Katalog zufolge
    diesen Namen besitzt -- /tbreminder_requirements_upload kann den
    Namen dann nicht auflösen und lehnt die Zeile ab.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT r.unit_id, COUNT(DISTINCT r.ally_code) AS owner_count
            FROM unit_names n
            JOIN roster_units r ON r.unit_id = n.unit_id
            WHERE n.display_name = ?
            GROUP BY r.unit_id
            ORDER BY owner_count DESC
            LIMIT 1
            """,
            (display_name,),
        ).fetchone()
        return row["unit_id"] if row else None


def get_owned_unit_display_names() -> list[str]:
    """
    Alle Anzeigenamen, die mindestens ein Gildenmitglied laut letztem
    Roster-Refresh tatsächlich besitzt -- Quelle für die Autocomplete von
    /tbreminder_units_ping. Exakt dasselbe Prinzip wie TW-Counters eigene
    Charakter-Katalog-Autocomplete (db.get_owned_unit_display_names(),
    siehe dessen README-Abschnitt "Charakter-Katalog"): kein
    ungefilterter ~11.000-Einheiten-Katalog, nur was die Gilde wirklich
    hat.
    """
    with get_connection() as conn:
        return [
            row["display_name"]
            for row in conn.execute(
                """
                SELECT DISTINCT n.display_name
                FROM unit_names n
                JOIN roster_units r ON r.unit_id = n.unit_id
                ORDER BY n.display_name
                """
            ).fetchall()
        ]


def get_owners_of_unit(unit_id: str, min_raw_relic_tier: int) -> list[sqlite3.Row]:
    """
    Gildenmitglieder, die eine bestimmte Einheit auf mindestens
    min_raw_relic_tier besitzen -- comlinks ROHER Wert, nicht die im
    Spiel angezeigte Stufe (siehe config.display_relic_to_raw(), vom
    Aufrufer bereits umgerechnet). MIT und OHNE verknüpfte Discord-ID
    zurückgegeben; der Aufrufer (bot.py) unterscheidet das für
    Ping-Fähigkeit -- identisch zu TW-Counters db.get_owners_of_unit().

    relic_tier IS NULL (keine Relic-Angabe, z.B. unterhalb Gear 13) wird
    immer ausgeschlossen, unabhängig vom Schwellwert. Sortiert nach
    relic_tier/gear_tier absteigend.

    NICHT für Schiffe geeignet -- Schiffe haben im Spiel überhaupt kein
    Relic-System, weshalb roster_units.relic_tier für JEDEN Schiffs-
    Besitzer IMMER NULL ist. Diese Funktion würde also für jede Schiffs-
    unit_id unabhängig vom Schwellwert 0 Zeilen liefern, selbst wenn die
    Gilde das Schiff breit besitzt -- siehe get_owners_of_unit_ignore_relic()
    für den richtigen Zugriffsweg bei Schiffen.
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT p.player_name, p.discord_id, r.gear_tier, r.relic_tier
            FROM roster_units r
            JOIN players p ON p.ally_code = r.ally_code
            WHERE r.unit_id = ? AND r.relic_tier IS NOT NULL AND r.relic_tier >= ?
            ORDER BY r.relic_tier DESC, r.gear_tier DESC
            """,
            (unit_id, min_raw_relic_tier),
        ).fetchall()


def get_owners_of_unit_ignore_relic(unit_id: str) -> list[sqlite3.Row]:
    """
    Gildenmitglieder, die eine bestimmte Einheit besitzen -- OHNE
    Relic-Filter. Für Einheiten, die im Spiel gar kein Relic-System
    haben (aktuell: Schiffe) ist ein Filter auf relic_tier sinnlos,
    da diese Spalte für solche Einheiten immer NULL ist -- ein Filter
    "relic_tier IS NOT NULL AND relic_tier >= X" würde dann JEDEN
    Besitzer ausschließen, unabhängig davon, wie viele die Gilde
    tatsächlich besitzt (siehe Chat-Verlauf, live aufgefallen bei
    /tbreminder_platoons_check).

    Interimslösung (siehe requirements.py's SHIP_UNIT_NAMES): der
    korrekte, dauerhafte Fix wäre, comlinks combatType-Feld beim
    Roster-Refresh mit zu erfassen (TW-Counters roster.py/db.py) und
    hier danach zu unterscheiden, statt einer manuell gepflegten
    Namensliste auf tb-reminder-Seite. Diese Funktion ist der schnelle,
    korrekte Zwischenschritt, der ohne einen weiteren Roster-Refresh-
    Zyklus oder ein TW-Counter-Schema-Update auskommt.

    "Besitz" heißt hier: irgendein Eintrag in roster_units für diese
    unit_id, unabhängig von rarity/gear_tier -- ein Schiff hat kein
    einsatzfähiges/nicht-einsatzfähiges Relic-Level, das gefiltert
    werden könnte, also zählt reiner Besitz.
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT p.player_name, p.discord_id, r.gear_tier, r.relic_tier
            FROM roster_units r
            JOIN players p ON p.ally_code = r.ally_code
            WHERE r.unit_id = ?
            ORDER BY r.gear_tier DESC
            """,
            (unit_id,),
        ).fetchall()
