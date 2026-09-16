"""
Persistenz-Layer für TB-Reminder: stats.json lesen/schreiben plus die
Teilnahme-/Erinnerungs-/Fehlend-Buchhaltung. Aus bot.py herausgelöst,
inhaltlich unverändert bis auf save_stats(), das jetzt atomar schreibt.

stats.json-Struktur (unverändert gegenüber der bisherigen bot.py):
{
  "total_tbs": 3,
  "current_run": {
    "active": true,
    "tb_index": 2,
    "phase": 4,
    "phase_started_at": 1712345678,
    "channel_id": 1279533599653232739
  },
  "players": {
    "<user_id>": {
      "name": "Spielername",
      "total_reminders": 7,
      "total_failed": 2,
      "total_tbs": 3,
      "tb_history": [2, 0, 3],
      "failed_history": [1, 0, 1]
    }
  }
}
"""

import json
import os
import time

import discord

import config

STATS_FILE = config.STATS_FILE


def load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"total_tbs": 0, "players": {}}


def save_stats(stats: dict) -> None:
    """
    Atomarer Schreibvorgang: erst in eine temporäre Datei im selben
    Verzeichnis schreiben, dann per os.replace() über die Zieldatei
    verschieben. os.replace() ist auf POSIX-Systemen ein einzelner
    atomarer Rename-Syscall -- es gibt keinen Zwischenzustand, in dem die
    Datei nur halb geschrieben auf der Platte liegt.

    Ohne das: ein Absturz mitten im direkten json.dump() (OOM-Kill,
    Festplatte voll, docker stop ohne Graceful-Shutdown) hinterlässt eine
    kaputte stats.json. load_stats() hat dafür kein Fallback -- der
    nächste on_ready() wirft beim json.load() eine Exception, und der Bot
    startet gar nicht erst. Das ist der exakte Fehlerfall, den die
    Startup-Recovery in bot.py voraussetzt reparieren zu können; ohne
    atomare Schreibvorgänge wäre die Grundlage dafür selbst nicht robust.
    """
    tmp_path = STATS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STATS_FILE)


def get_tb_index(stats: dict) -> int:
    """Aktueller TB-Index = Anzahl bisher gestarteter Territory Battles."""
    return stats.get("total_tbs", 0)


def set_current_run(
    stats: dict,
    tb_index: int,
    phase: int,
    channel_id: int,
    update_timestamp: bool = True,
) -> None:
    """
    Persistiert den aktuellen Lauf-Status.
    update_timestamp=True  -> frischer Phasenübergang, neuer phase_started_at
    update_timestamp=False -> nur Resume, bestehenden phase_started_at behalten
    """
    existing = stats.get("current_run", {})
    stats["current_run"] = {
        "active": True,
        "tb_index": tb_index,
        "phase": phase,
        "phase_started_at": int(time.time())
        if update_timestamp
        else existing.get("phase_started_at", int(time.time())),
        "channel_id": channel_id,
    }
    save_stats(stats)


def clear_current_run(stats: dict) -> None:
    """Wird aufgerufen, wenn eine TB-Sequenz normal abgeschlossen wird."""
    stats["current_run"] = {"active": False}
    save_stats(stats)


def record_participation(stats: dict, members: list[discord.Member]) -> None:
    """
    Einmal beim TB-Start aufgerufen. Registriert alle aktuellen
    Rollen-Mitglieder als Teilnehmer dieses Territory Battles und
    inkrementiert den globalen TB-Zähler.
    """
    tb_index = get_tb_index(stats)

    for m in members:
        uid = str(m.id)
        if uid not in stats["players"]:
            stats["players"][uid] = {
                "name": m.display_name,
                "total_reminders": 0,
                "total_failed": 0,
                "total_tbs": 0,
                "tb_history": [],
                "failed_history": [],
            }
        player = stats["players"][uid]
        player["name"] = m.display_name

        while len(player["tb_history"]) < tb_index:
            player["tb_history"].append(0)
        while len(player.setdefault("failed_history", [])) < tb_index:
            player["failed_history"].append(0)

        player["tb_history"].append(0)
        player["failed_history"].append(0)
        player["total_tbs"] += 1

    stats["total_tbs"] = tb_index + 1
    save_stats(stats)


def record_reminders(
    stats: dict, reminded_members: list[tuple[str, str]], tb_index: int
) -> None:
    """Erhöht den Erinnerungs-Zähler für jeden vom Officer ausgewählten Spieler."""
    for uid, name in reminded_members:
        if uid not in stats["players"]:
            stats["players"][uid] = {
                "name": name,
                "total_reminders": 0,
                "total_failed": 0,
                "total_tbs": 1,
                "tb_history": [0] * (tb_index + 1),
                "failed_history": [0] * (tb_index + 1),
            }
        player = stats["players"][uid]
        player["name"] = name

        while len(player["tb_history"]) <= tb_index:
            player["tb_history"].append(0)

        player["tb_history"][tb_index] += 1
        player["total_reminders"] += 1

    save_stats(stats)


def record_failed(
    stats: dict, failed_members: list[tuple[str, str]], tb_index: int
) -> None:
    """Erhöht den Nicht-stationiert-Zähler für jeden vom Officer markierten Spieler."""
    for uid, name in failed_members:
        if uid not in stats["players"]:
            stats["players"][uid] = {
                "name": name,
                "total_reminders": 0,
                "total_failed": 0,
                "total_tbs": 1,
                "tb_history": [0] * (tb_index + 1),
                "failed_history": [0] * (tb_index + 1),
            }
        player = stats["players"][uid]
        player["name"] = name
        player.setdefault("total_failed", 0)
        player.setdefault("failed_history", [])

        while len(player["failed_history"]) <= tb_index:
            player["failed_history"].append(0)

        player["failed_history"][tb_index] += 1
        player["total_failed"] = player.get("total_failed", 0) + 1

    save_stats(stats)
