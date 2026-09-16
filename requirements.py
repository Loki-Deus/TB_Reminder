"""
Platoon-Anforderungen: CSV-Upload, Speicherung, und Fehlbestands-Berechnung
für /tbreminder_requirements_upload, /tbreminder_platoons_check und
/tbreminder_platoons_ping.

Eine einzige aktive Anforderungsliste (config.REQUIREMENTS_FILE) -- kein
Layout-/TB-Konzept, keine Phasen. Wir spielen ausschließlich Return of the
Empire, Phasen können pro Run variieren -- der Planet allein bestimmt die
Anforderung, siehe Chat-Verlauf. Ein Upload ersetzt den gesamten Inhalt,
mit Backup der vorherigen Version (siehe replace_requirements()).

Gespeichertes Format (config.REQUIREMENTS_FILE):
{
  "<planet_name>": [
    {"unit_id": "...", "unit_name": "...", "required_count": 2, "required_relic": 7},
    ...
  ],
  ...
}
"""

import csv
import io
import json
import os
import time

import config
import roster_read

REQUIRED_COLUMNS = ("planet", "unit_name", "required_count", "required_relic")


class RequirementsParseError(ValueError):
    """CSV fehlerhaft -- fehlende Spalten, ungültige Werte, oder eine
    Einheit, die roster_read.resolve_unit_id() nicht auflösen konnte.
    Die Nachricht ist bereits für die direkte Discord-Anzeige formatiert
    (eine Zeile pro Problem, nicht nur der erste Fehler -- siehe
    parse_csv())."""


def parse_csv(raw: bytes) -> dict[str, list[dict]]:
    """
    Parst und validiert die hochgeladene CSV vollständig, bevor irgendwas
    gespeichert wird. Sammelt ALLE Zeilenfehler und wirft sie zusammen --
    ein Upload-Versuch soll den vollständigen Fehlerbericht liefern, nicht
    nur den ersten Fehler, damit nicht pro Korrektur ein neuer Versuch
    nötig ist.
    """
    try:
        text = raw.decode("utf-8-sig")  # -sig: toleriert eine Excel-BOM
    except UnicodeDecodeError as e:
        raise RequirementsParseError(f"CSV ist nicht UTF-8-kodiert: {e}") from e

    reader = csv.DictReader(io.StringIO(text))
    header = set(reader.fieldnames or [])
    missing = set(REQUIRED_COLUMNS) - header
    if missing:
        raise RequirementsParseError(
            f"CSV-Header unvollständig. Fehlende Spalten: {', '.join(sorted(missing))}"
        )

    errors: list[str] = []
    result: dict[str, list[dict]] = {}

    for line_no, row in enumerate(reader, start=2):  # Zeile 1 ist der Header
        planet = (row.get("planet") or "").strip()
        unit_name = (row.get("unit_name") or "").strip()
        raw_count = (row.get("required_count") or "").strip()
        raw_relic = (row.get("required_relic") or "").strip()

        if not planet or not unit_name:
            errors.append(f"Zeile {line_no}: planet oder unit_name ist leer.")
            continue

        try:
            required_count = int(raw_count)
            if required_count <= 0:
                raise ValueError
        except ValueError:
            errors.append(
                f"Zeile {line_no}: required_count ungültig ({raw_count!r}, "
                f"muss eine positive Zahl sein)."
            )
            continue

        try:
            required_relic = int(raw_relic)
            if not (0 <= required_relic <= 20):
                raise ValueError
        except ValueError:
            errors.append(
                f"Zeile {line_no}: required_relic ungültig ({raw_relic!r}, "
                f"muss zwischen 0 und 20 liegen)."
            )
            continue

        unit_id = roster_read.resolve_unit_id(unit_name)
        if unit_id is None:
            errors.append(
                f"Zeile {line_no}: Einheit '{unit_name}' konnte nicht aufgelöst "
                f"werden -- besitzt sie laut letztem Roster-Refresh mindestens "
                f"ein Gildenmitglied? Tippfehler prüfen."
            )
            continue

        result.setdefault(planet, []).append(
            {
                "unit_id": unit_id,
                "unit_name": unit_name,
                "required_count": required_count,
                "required_relic": required_relic,
            }
        )

    if errors:
        raise RequirementsParseError("\n".join(errors))
    if not result:
        raise RequirementsParseError("CSV enthält keine gültigen Zeilen.")

    return result


def load_requirements() -> dict[str, list[dict]]:
    if not os.path.exists(config.REQUIREMENTS_FILE):
        return {}
    with open(config.REQUIREMENTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def replace_requirements(new_data: dict[str, list[dict]]) -> str | None:
    """
    Ersetzt die gespeicherten Anforderungen vollständig. Sichert eine
    vorhandene vorherige Version zuerst unter einem zeitgestempelten
    Dateinamen, statt sie zu überschreiben -- billige Absicherung gegen
    einen fehlerhaften Upload, der erst nach dem Ersetzen auffällt.
    Gibt den Backup-Pfad zurück, oder None, wenn es noch keine vorherige
    Version gab.

    Atomarer Schreibvorgang wie stats.py's save_stats() (temp-Datei +
    os.replace()), aus demselben Grund: kein Zwischenzustand mit
    kaputtem JSON bei einem Absturz mitten im Schreiben.
    """
    os.makedirs(config.REQUIREMENTS_DIR, exist_ok=True)

    backup_path = None
    if os.path.exists(config.REQUIREMENTS_FILE):
        backup_path = os.path.join(
            config.REQUIREMENTS_DIR, f"rote.{int(time.time())}.bak.json"
        )
        os.replace(config.REQUIREMENTS_FILE, backup_path)

    tmp_path = config.REQUIREMENTS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, config.REQUIREMENTS_FILE)

    return backup_path


def get_planets() -> list[str]:
    return sorted(load_requirements().keys())


def get_planet_requirements(planet: str) -> list[dict]:
    return load_requirements().get(planet, [])


def compute_shortfall(planet: str) -> list[dict]:
    """
    Für jede auf diesem Planeten hinterlegte Einheit: wie viele
    Gildenmitglieder besitzen sie auf dem geforderten Relic-Level, und
    reicht das. Gemeinsame Berechnungsbasis für /tbreminder_platoons_check
    (Text) und /tbreminder_platoons_ping (Text + Ping-Buttons) sowie die
    automatische Phasenend-Nachprüfung in bot.py -- alle drei dürfen nie
    unterschiedliche Zahlen zeigen, deshalb eine einzige Funktion statt
    mehrerer Implementierungen.

    Rückgabe pro Einheit (Dict, erweitert um "owners" und "shortfall"):
    unit_id, unit_name, required_count, required_relic, owners (Liste aus
    roster_read.get_owners_of_unit()), shortfall (0 wenn ausreichend
    gedeckt). Unterdeckungen zuerst sortiert, absteigend nach Schwere.
    """
    rows = get_planet_requirements(planet)
    result = []
    for row in rows:
        min_raw_relic = config.display_relic_to_raw(row["required_relic"])
        owners = roster_read.get_owners_of_unit(row["unit_id"], min_raw_relic)
        shortfall = max(0, row["required_count"] - len(owners))
        result.append({**row, "owners": owners, "shortfall": shortfall})
    result.sort(key=lambda r: -r["shortfall"])
    return result
