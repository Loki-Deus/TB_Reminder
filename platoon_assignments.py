"""
Platoon-Zuweisungen: Manager tragen ein, welcher Spieler in welcher Phase
welche Einheit auf welchem Planeten als Platoon stationieren soll
(/tbreminder_platoon_register, _remove, _list). Zu Beginn der jeweiligen
Phase bekommt jeder betroffene Spieler eine DM mit all seinen Einträgen
für diese Phase (Versand: dispatch_platoon_dms() in bot.py).

Gespeichertes Format (config.PLATOON_FILE):
{
  "assignments": [
    {
      "id": "a1b2c3d4",
      "phase": 3,                 # 1-6
      "planet": "...",
      "unit_id": "...", "unit_name": "...",
      "ally_code": "...", "player_name": "...",
      "discord_id": "...",        # Stand bei Registrierung, nur Fallback
      "created_by": "...", "created_at": 1712345678,
      "tb_index": null,           # null = gilt für den nächsten TB
      "sent": false,
      "result": null              # nach Versand: "ok" oder Fehlergrund
    }
  ]
}

Bindung an einen TB (tb_index):
  - Eintrag während eines laufenden TBs -> sofort an dessen tb_index.
  - Eintrag ohne laufenden TB -> null, wird beim nächsten TB-Start
    gebunden (bind_unbound()). So kann man vorab planen.
  - Nach Phase 6 werden alle Einträge des beendeten TBs gelöscht
    (clear_tb()). Einträge eines abgebrochenen und nie fortgesetzten TBs
    räumt prune_stale() beim nächsten TB weg.

Phasenbeginn: siehe current_open_phase().
"""

import json
import os
import time
import uuid

import config

PLATOON_FILE = config.PLATOON_FILE


def load() -> list[dict]:
    if os.path.exists(PLATOON_FILE):
        with open(PLATOON_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("assignments", [])
    return []


def save(assignments: list[dict]) -> None:
    """Atomar wie stats.save_stats()."""
    tmp_path = PLATOON_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"assignments": assignments}, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, PLATOON_FILE)


def current_open_phase(run: dict, now: float | None = None) -> int | None:
    """
    Aktuell offene Phase (1-6) aus stats["current_run"], None wenn kein TB
    läuft oder Phase 6 vorbei ist.

    current_run["phase"] = k ist der Index der als nächstes feuernden
    Erinnerungsauswahl; phase_started_at ist der Zeitpunkt der letzten
    Auswahl (bzw. TB-Start bei k == 0), siehe run_sequence/set_current_run.
      k == 0      -> Phase 1 offen seit TB-Start.
      k >= 1      -> Auswahl für Phase k lief um phase_started_at; Phase k
                     endet PLAYER_REMINDER_OFFSET später, dann öffnet k+1.
    """
    if not run.get("active"):
        return None
    k = run.get("phase", 0)
    started_at = run.get("phase_started_at")
    if started_at is None:
        return None
    if now is None:
        now = time.time()
    if k == 0:
        return 1
    if now < started_at + config.PLAYER_REMINDER_OFFSET:
        return k
    return k + 1 if k < config.PHASE_COUNT else None


def phase_open_ts(run: dict, phase: int) -> int | None:
    """Zeitpunkt (Unix), zu dem `phase` öffnet bzw. geöffnet hat -- nur für
    die gerade offene oder die direkt folgende Phase bestimmbar, sonst None."""
    if not run.get("active") or run.get("phase_started_at") is None:
        return None
    k = run.get("phase", 0)
    s = run["phase_started_at"]
    if k == 0 and phase == 1:
        return s
    if k >= 1 and phase == k + 1:
        return s + config.PLAYER_REMINDER_OFFSET
    return None


def new_assignment(
    *, phase: int, planet: str, unit_id: str, unit_name: str,
    ally_code: str, player_name: str, discord_id: str | None,
    created_by: str, tb_index: int | None,
) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "phase": phase,
        "planet": planet,
        "unit_id": unit_id,
        "unit_name": unit_name,
        "ally_code": ally_code,
        "player_name": player_name,
        "discord_id": discord_id,
        "created_by": created_by,
        "created_at": int(time.time()),
        "tb_index": tb_index,
        "sent": False,
        "result": None,
    }


def find_duplicate(assignments: list[dict], phase: int, planet: str, unit_id: str, ally_code: str) -> dict | None:
    for a in assignments:
        if (a["phase"], a["planet"].lower(), a["unit_id"], a["ally_code"]) == (
            phase, planet.lower(), unit_id, ally_code
        ):
            return a
    return None


def bind_unbound(assignments: list[dict], tb_index: int) -> bool:
    changed = False
    for a in assignments:
        if a["tb_index"] is None:
            a["tb_index"] = tb_index
            changed = True
    return changed


def prune_stale(assignments: list[dict], tb_index: int) -> list[dict]:
    """Einträge älterer TBs (abgebrochen, nie fortgesetzt) entfernen."""
    return [a for a in assignments if a["tb_index"] is None or a["tb_index"] >= tb_index]


def clear_tb(tb_index: int) -> int:
    """Nach Phase 6: alle Einträge dieses TBs löschen. Rückgabe: Anzahl."""
    assignments = load()
    keep = [a for a in assignments if a["tb_index"] != tb_index]
    if len(keep) != len(assignments):
        save(keep)
    return len(assignments) - len(keep)


def reset_tb(tb_index: int) -> None:
    """TB vor Phase 1 abgebrochen und zurückgerollt: Einträge wieder als
    ungebunden und ungesendet markieren, damit der Neustart sie erneut
    verschickt."""
    assignments = load()
    changed = False
    for a in assignments:
        if a["tb_index"] == tb_index:
            a["tb_index"] = None
            a["sent"] = False
            a["result"] = None
            changed = True
    if changed:
        save(assignments)


def label(a: dict) -> str:
    return f"P{a['phase']} · {a['planet']} · {a['unit_name']} · {a['player_name']}"
