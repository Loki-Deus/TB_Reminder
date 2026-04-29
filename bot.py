import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

TOKEN          = os.getenv("DISCORD_TOKEN")
TW_CHANNEL_ID  = int(os.getenv("TW_CHANNEL_ID"))
OFFICER_ID     = int(os.getenv("OFFICER_ID"))
MANAGER_IDS    = set(int(i) for i in os.getenv("MANAGER_IDS", "").split(",") if i.strip())
MEMBER_ROLE_ID = int(os.getenv("MEMBER_ROLE_ID"))

HOURS = 3600
OFFICER_TIMEOUT = 1 * HOURS
STATS_FILE = os.path.join(os.getenv("DATA_DIR", "."), "stats.json")
BOT_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Vienna"))

PHASE_END_MESSAGES = [
    "Phase 1 endet bald!",
    "Phase 2 endet bald!",
    "Phase 3 endet bald!",
    "Phase 4 endet bald!",
    "Phase 5 endet bald!",
    "Phase 6 endet bald, holt nochmal alles raus!",
]

GENERIC_REMINDER = "Bitte denkt dran im Territory Battle zu stationieren!"

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

is_running = False
pending_timer: asyncio.Task | None = None


# stats.json structure:
# {
#   "total_tbs": 3,
#   "current_run": {                    <- written at TB start, updated each phase, cleared on finish
#     "active": true,
#     "tb_index": 2,
#     "phase": 4,                       <- last phase that completed (0 = none yet)
#     "phase_started_at": 1712345678,   <- unix timestamp when current phase wait began
#     "channel_id": 1279533599653232739
#   },
#   "players": {
#     "<user_id>": {
#       "name": "Spielername",
#       "total_reminders": 7,
#       "total_failed": 2,
#       "total_tbs": 3,
#       "tb_history": [2, 0, 3],
#       "failed_history": [1, 0, 1]
#     }
#   }
# }

def load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"total_tbs": 0, "players": {}}


def save_stats(stats: dict):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def get_tb_index(stats: dict) -> int:
    """Current TB index = number of Territory Battles started so far."""
    return stats.get("total_tbs", 0)


def set_current_run(stats: dict, tb_index: int, phase: int, channel_id: int, update_timestamp: bool = True):
    """
    Persist the current run state to disk.
    update_timestamp=True  -> fresh phase transition, write new phase_started_at
    update_timestamp=False -> resume only, preserve existing phase_started_at
    """
    existing = stats.get("current_run", {})
    stats["current_run"] = {
        "active": True,
        "tb_index": tb_index,
        "phase": phase,
        "phase_started_at": int(time.time()) if update_timestamp else existing.get("phase_started_at", int(time.time())),
        "channel_id": channel_id,
    }
    save_stats(stats)


def clear_current_run(stats: dict):
    """Called when a TB sequence completes normally."""
    stats["current_run"] = {"active": False}
    save_stats(stats)


def record_participation(stats: dict, members: list[discord.Member]):
    """
    Called once at TB start. Registers all current role members as
    participating in this Territory Battle and increments the global TB counter.
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


def record_reminders(stats: dict, reminded_members: list[tuple[str, str]], tb_index: int):
    """Increment reminder count for each player the officer picked."""
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


def record_failed(stats: dict, failed_members: list[tuple[str, str]], tb_index: int):
    """Increment failed-to-set count for each player the officer flagged."""
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


# ── Permission helper ─────────────────────────────────────────────────────────

def is_authorized(interaction: discord.Interaction) -> bool:
    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.id in MANAGER_IDS
    )


# ── Player selection UI ───────────────────────────────────────────────────────

class PlayerSelectView(discord.ui.View):
    """Officer picks players to send a personal reminder to."""
    def __init__(self, members: list[discord.Member]):
        super().__init__(timeout=OFFICER_TIMEOUT)
        self.selected_ids: set[str] = set()
        self.confirmed = False
        self._skipped = False

        chunk1 = members[:25]
        chunk2 = members[25:50]

        self._add_select(chunk1, "Spieler 1-25 auswaehlen...", "select_1")
        if chunk2:
            self._add_select(chunk2, "Spieler 26-50 auswaehlen...", "select_2")

        confirm_btn = discord.ui.Button(
            label="Bestaetigen & senden",
            style=discord.ButtonStyle.green,
            row=2,
        )
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

        skip_btn = discord.ui.Button(
            label="Ueberspringen (generische Nachricht)",
            style=discord.ButtonStyle.grey,
            row=2,
        )
        skip_btn.callback = self._on_skip
        self.add_item(skip_btn)

    def _add_select(self, members: list[discord.Member], placeholder: str, custom_id: str):
        options = [
            discord.SelectOption(label=m.display_name, value=str(m.id))
            for m in members
        ]
        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=custom_id,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        chosen = set(interaction.data["values"])
        custom_id = interaction.data["custom_id"]

        if custom_id == "select_1":
            pool = {o.value for item in self.children
                    if isinstance(item, discord.ui.Select) and item.custom_id == "select_1"
                    for o in item.options}
        else:
            pool = {o.value for item in self.children
                    if isinstance(item, discord.ui.Select) and item.custom_id == "select_2"
                    for o in item.options}

        self.selected_ids -= pool
        self.selected_ids |= chosen

        await interaction.response.send_message(
            f"Aktuell ausgewaehlt: **{len(self.selected_ids)} Spieler**\n"
            "Druecke *Bestaetigen* wenn du fertig bist.",
            ephemeral=True,
        )

    async def _on_confirm(self, interaction: discord.Interaction):
        if not self.selected_ids:
            await interaction.response.send_message(
                "Du hast noch niemanden ausgewaehlt!", ephemeral=True
            )
            return
        self.confirmed = True
        await interaction.response.edit_message(
            content=f"Bestaetigt! {len(self.selected_ids)} Spieler erhalten eine persoenliche Nachricht.",
            view=None,
        )
        self.stop()

    async def _on_skip(self, interaction: discord.Interaction):
        self.confirmed = False
        self._skipped = True
        await interaction.response.edit_message(
            content="Uebersprungen. Eine generische Nachricht wird gesendet.",
            view=None,
        )
        self.stop()

    async def on_timeout(self):
        self.confirmed = False
        self._skipped = False
        try:
            await self.message.edit(
                content="Zeit abgelaufen! Keine Auswahl getroffen - eine generische Nachricht wurde gesendet.",
                view=None,
            )
        except Exception:
            pass


class FailedSetView(discord.ui.View):
    """Officer picks players who failed to set their troops this phase."""
    def __init__(self, members: list[discord.Member], timeout: float = OFFICER_TIMEOUT):
        super().__init__(timeout=timeout)
        self.selected_ids: set[str] = set()
        self.confirmed = False
        self._skipped = False

        chunk1 = members[:25]
        chunk2 = members[25:50]

        self._add_select(chunk1, "Spieler 1-25 auswaehlen...", "fselect_1")
        if chunk2:
            self._add_select(chunk2, "Spieler 26-50 auswaehlen...", "fselect_2")

        confirm_btn = discord.ui.Button(
            label="Bestaetigen (nicht stationiert)",
            style=discord.ButtonStyle.red,
            row=2,
        )
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

        skip_btn = discord.ui.Button(
            label="Alle haben stationiert",
            style=discord.ButtonStyle.grey,
            row=2,
        )
        skip_btn.callback = self._on_skip
        self.add_item(skip_btn)

    def _add_select(self, members: list[discord.Member], placeholder: str, custom_id: str):
        options = [
            discord.SelectOption(label=m.display_name, value=str(m.id))
            for m in members
        ]
        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=custom_id,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        chosen = set(interaction.data["values"])
        custom_id = interaction.data["custom_id"]

        if custom_id == "fselect_1":
            pool = {o.value for item in self.children
                    if isinstance(item, discord.ui.Select) and item.custom_id == "fselect_1"
                    for o in item.options}
        else:
            pool = {o.value for item in self.children
                    if isinstance(item, discord.ui.Select) and item.custom_id == "fselect_2"
                    for o in item.options}

        self.selected_ids -= pool
        self.selected_ids |= chosen

        await interaction.response.send_message(
            f"Aktuell ausgewaehlt: **{len(self.selected_ids)} Spieler**\n"
            "Druecke *Bestaetigen* wenn du fertig bist.",
            ephemeral=True,
        )

    async def _on_confirm(self, interaction: discord.Interaction):
        if not self.selected_ids:
            await interaction.response.send_message(
                "Du hast noch niemanden ausgewaehlt!", ephemeral=True
            )
            return
        self.confirmed = True
        await interaction.response.edit_message(
            content=f"Bestaetigt! {len(self.selected_ids)} Spieler als nicht stationiert markiert.",
            view=None,
        )
        self.stop()

    async def _on_skip(self, interaction: discord.Interaction):
        self.confirmed = False
        self._skipped = True
        await interaction.response.edit_message(
            content="Alle haben stationiert - keine Eintrage.",
            view=None,
        )
        self.stop()

    async def on_timeout(self):
        self.confirmed = False
        self._skipped = False
        try:
            await self.message.edit(
                content="Zeit abgelaufen - keine Fehlenden eingetragen.",
                view=None,
            )
        except Exception:
            pass


# ── Core logic ────────────────────────────────────────────────────────────────

async def handle_phase_end(
    phase_index: int,
    tw_channel: discord.TextChannel,
    stats: dict,
    tb_index: int,
    next_phase_wait: float = 0,
    is_last_phase: bool = False,
):
    phase_num = phase_index + 1
    print(f"Phase {phase_num} endet bald - Officer wird kontaktiert...")

    async def send_generic():
        await tw_channel.send(f"@everyone {GENERIC_REMINDER}")

    try:
        officer = await bot.fetch_user(OFFICER_ID)
    except discord.NotFound:
        print(f"Officer (ID {OFFICER_ID}) nicht gefunden - generische Nachricht wird gesendet")
        await send_generic()
        return
    except discord.HTTPException as e:
        print(f"Netzwerkfehler beim Abrufen des Officers ({e}) - generische Nachricht wird gesendet")
        await send_generic()
        return

    guild  = tw_channel.guild
    role   = guild.get_role(MEMBER_ROLE_ID)

    if role is None:
        print(f"Rolle (ID {MEMBER_ROLE_ID}) nicht gefunden - generische Nachricht wird gesendet")
        await send_generic()
        return

    members = sorted(
        [m for m in role.members if not m.bot],
        key=lambda m: m.display_name.lower()
    )[:50]

    if not members:
        print("Keine Mitglieder gefunden - generische Nachricht wird gesendet")
        await send_generic()
        return

    # ── Step 1: Reminder picker ───────────────────────────────────────────────
    reminder_view = PlayerSelectView(members)

    try:
        msg = await officer.send(
            f"**Phase {phase_num} endet bald! (Territory Battle)**\n"
            f"Waehle die Spieler aus, die eine persoenliche Erinnerung erhalten sollen.\n"
            f"Du hast **1 Stunde** Zeit. Danach wird automatisch eine generische Nachricht gesendet.\n\n"
            f"*(Spieler in den Dropdowns auswaehlen, dann auf Bestaetigen klicken)*",
            view=reminder_view,
        )
        reminder_view.message = msg
    except discord.Forbidden:
        print("Officer hat DMs deaktiviert - generische Nachricht wird gesendet")
        await send_generic()
        return

    await reminder_view.wait()

    phase_msg = PHASE_END_MESSAGES[phase_index]

    if reminder_view.confirmed and reminder_view.selected_ids:
        member_map = {str(m.id): m for m in members}
        sent, failed_dm = 0, 0
        reminded = []

        for uid in reminder_view.selected_ids:
            member = member_map.get(uid)
            if member:
                for attempt in range(3):
                    try:
                        await member.send(f"{phase_msg}")
                        sent += 1
                        reminded.append((uid, member.display_name))
                        break
                    except discord.Forbidden:
                        print(f"Konnte {member.display_name} keine DM senden (DMs deaktiviert)")
                        failed_dm += 1
                        break
                    except discord.DiscordServerError as e:
                        if attempt < 2:
                            print(f"Discord 503 beim Senden an {member.display_name}, Versuch {attempt + 1}/3 - warte 5s...")
                            await asyncio.sleep(5)
                        else:
                            print(f"Discord 503 beim Senden an {member.display_name} nach 3 Versuchen - uebersprungen.")
                            failed_dm += 1
                    except Exception as e:
                        print(f"Unerwarteter Fehler beim Senden an {member.display_name}: {e} - uebersprungen.")
                        failed_dm += 1
                        break

        record_reminders(stats, reminded, tb_index)

        print(f"Phase {phase_num}: {sent} DMs gesendet, {failed_dm} fehlgeschlagen.")
        await officer.send(
            f"Erledigt! **{sent}** Spieler wurden per DM benachrichtigt" +
            (f", **{failed_dm}** konnten nicht erreicht werden (DMs deaktiviert)." if failed_dm else ".")
        )
    else:
        reason = "Uebersprungen" if reminder_view._skipped else "Timeout"
        print(f"Phase {phase_num}: {reason} - generische Nachricht wird gesendet.")
        await send_generic()

    # ── Step 2: Failed-to-set picker (fire and forget) ────────────────────────
    async def send_failed_picker():
        if is_last_phase:
            failed_timeout = 22 * HOURS
        elif next_phase_wait > 0:
            failed_timeout = max(OFFICER_TIMEOUT, next_phase_wait - OFFICER_TIMEOUT)
        else:
            failed_timeout = OFFICER_TIMEOUT
        deadline_ts = int(time.time()) + int(failed_timeout)
        failed_view = FailedSetView(members, timeout=failed_timeout)
        try:
            msg = await officer.send(
                f"**Phase {phase_num} - Wer hat NICHT stationiert?**\n"
                f"Waehle die Spieler aus, die diese Phase nicht stationiert haben.\n"
                f"Du hast Zeit bis <t:{deadline_ts}:F> (<t:{deadline_ts}:R>).\n\n"
                f"*(Falls alle stationiert haben, auf 'Alle haben stationiert' klicken)*",
                view=failed_view,
            )
            failed_view.message = msg
        except discord.Forbidden:
            print("Officer hat DMs deaktiviert - Failed-to-set wird nicht erfasst")
            if is_last_phase:
                await send_stats_summary(officer, stats, tb_index)
            return

        await failed_view.wait()

        if failed_view.confirmed and failed_view.selected_ids:
            member_map = {str(m.id): m for m in members}
            failed_list = [
                (uid, member_map[uid].display_name)
                for uid in failed_view.selected_ids
                if uid in member_map
            ]
            record_failed(stats, failed_list, tb_index)
            print(f"Phase {phase_num}: {len(failed_list)} Spieler als nicht stationiert markiert.")
        else:
            reason = "Alle haben stationiert" if failed_view._skipped else "Timeout"
            print(f"Phase {phase_num}: {reason} - keine Fehlenden eingetragen.")

        if is_last_phase:
            await send_stats_summary(officer, stats, tb_index)

    asyncio.create_task(send_failed_picker())


async def build_stats_messages(stats: dict, tb_index: int) -> list[str]:
    """
    Build the stats summary message strings for a given TB index.
    Returns a list of message strings (up to 2: reminders + failed-to-set).
    Shared by the automatic end-of-TB summary and /start_tb_results.
    """
    players = stats.get("players", {})
    messages = []

    rows = []
    for data in players.values():
        if len(data["tb_history"]) <= tb_index:
            continue
        reminders_this_tb = data["tb_history"][tb_index]
        failed_this_tb = (
            data["failed_history"][tb_index]
            if len(data.get("failed_history", [])) > tb_index
            else 0
        )
        total_tbs       = data.get("total_tbs", 1)
        total_reminders = data.get("total_reminders", 0)
        total_failed    = data.get("total_failed", 0)
        max_possible    = total_tbs * 6
        reminder_quote  = round((total_reminders / max_possible) * 100) if max_possible > 0 else 0
        failed_quote    = round((total_failed    / max_possible) * 100) if max_possible > 0 else 0
        rows.append((
            data["name"],
            reminders_this_tb,
            failed_this_tb,
            total_tbs,
            total_reminders,
            total_failed,
            max_possible,
            reminder_quote,
            failed_quote,
        ))

    if not rows:
        return [f"**TB-Abschlussbericht #{tb_index + 1}**\nKeine Teilnehmerdaten fuer diesen TB gefunden."]

    # ── Reminder summary ──
    reminder_rows = sorted(rows, key=lambda x: (x[1], x[7]), reverse=True)
    r_lines = [f"**TB-Abschlussbericht #{tb_index + 1} - Erinnerungen**\n"]
    r_lines.append(f"{'Spieler':<20} {'Dieser TB':>10} {'TBs dabei':>10} {'Quote':>14}")
    r_lines.append("-" * 58)
    has_reminder_data = False
    for name, rem_tb, _, total_tbs, total_reminders, _, max_possible, reminder_quote, _ in reminder_rows:
        if total_reminders == 0:
            continue
        fraction = f"({total_reminders}/{max_possible})"
        r_lines.append(f"{name:<20} {rem_tb:>10} {total_tbs:>10} {f'{reminder_quote}% {fraction}':>14}")
        has_reminder_data = True

    if has_reminder_data:
        messages.append("```\n" + "\n".join(r_lines) + "\n```")
    else:
        messages.append(
            f"**TB-Abschlussbericht #{tb_index + 1} - Erinnerungen**\n"
            "In diesem TB wurde niemand persoenlich erinnert."
        )

    # ── Failed-to-set summary ──
    failed_rows = sorted(rows, key=lambda x: (x[2], x[8]), reverse=True)
    f_lines = [f"**TB-Abschlussbericht #{tb_index + 1} - Nicht stationiert**\n"]
    f_lines.append(f"{'Spieler':<20} {'Dieser TB':>10} {'TBs dabei':>10} {'Quote':>14}")
    f_lines.append("-" * 58)
    has_failed_data = False
    for name, _, fail_tb, total_tbs, _, total_failed, max_possible, _, failed_quote in failed_rows:
        if total_failed == 0:
            continue
        fraction = f"({total_failed}/{max_possible})"
        f_lines.append(f"{name:<20} {fail_tb:>10} {total_tbs:>10} {f'{failed_quote}% {fraction}':>14}")
        has_failed_data = True

    if has_failed_data:
        messages.append("```\n" + "\n".join(f_lines) + "\n```")
    else:
        messages.append(
            f"**TB-Abschlussbericht #{tb_index + 1} - Nicht stationiert**\n"
            "In diesem TB hat niemand das Stationieren verpasst - gut gemacht!"
        )

    return messages


async def send_stats_summary(officer: discord.User, stats: dict, tb_index: int):
    """DM the officer the stats summary. Called automatically at end of TB."""
    messages = await build_stats_messages(stats, tb_index)
    for msg in messages:
        await officer.send(msg)


async def run_sequence(tw_channel: discord.TextChannel, start_phase: int = 0, phase_elapsed: float = 0.0):
    """
    Main TB sequence.
    start_phase:    phase index (0-5) to start from. Used by /resume_tb.
    phase_elapsed:  seconds already elapsed in the current phase wait. Used by /resume_tb.
    """
    global is_running
    stats = load_stats()

    # On resume, reuse the persisted tb_index. On fresh start, derive from record_participation.
    if start_phase > 0:
        tb_index = stats.get("current_run", {}).get("tb_index", get_tb_index(stats) - 1)
    else:
        tb_index = get_tb_index(stats)  # will be updated after record_participation

    try:
        guild = tw_channel.guild
        role  = guild.get_role(MEMBER_ROLE_ID)
        members = []
        if role:
            members = sorted(
                [m for m in role.members if not m.bot],
                key=lambda m: m.display_name.lower()
            )[:50]

        if start_phase == 0:
            # Fresh start: record participation and announce
            if members:
                record_participation(stats, members)
                tb_index = get_tb_index(stats) - 1  # updated by record_participation
                print(f"{len(members)} Spieler als TB-Teilnehmer registriert.")
            else:
                print("Rolle nicht gefunden oder keine Mitglieder - Teilnahme wird nicht getrackt.")

            await tw_channel.send("@everyone Ein neues Territory Battle hat gestartet!")
            print(f"TB-Startnachricht gesendet. (TB #{tb_index + 1} in den Stats)")
        else:
            print(f"TB-Sequenz wird ab Phase {start_phase + 1} fortgesetzt. (TB #{tb_index + 1})")

        # Persist run state to disk
        # On resume (start_phase > 0), preserve existing phase_started_at
        set_current_run(stats, tb_index, start_phase, tw_channel.id, update_timestamp=(start_phase == 0))

        carry_over = 0.0
        for i in range(start_phase, 6):
            if i == start_phase and phase_elapsed > 0:
                # Resume: subtract already-elapsed time from this phase's wait
                base_wait = 22 * HOURS if i == 0 else 24 * HOURS
                wait_seconds = max(0, base_wait - phase_elapsed)
                print(f"Phase {i + 1}: noch {wait_seconds / HOURS:.2f}h verbleibend (Resumption).")
            else:
                wait_seconds = (22 * HOURS if i == 0 else 24 * HOURS) - carry_over

            next_phase_wait = 24 * HOURS if i < 5 else 0
            last_phase = (i == 5)

            print(f"Warte {wait_seconds / HOURS:.2f}h bis Phase {i + 1} endet...")
            await asyncio.sleep(max(0, wait_seconds))

            # Update persisted phase state before handling
            set_current_run(stats, tb_index, i + 1, tw_channel.id)

            t0 = time.monotonic()
            await handle_phase_end(i, tw_channel, stats, tb_index, next_phase_wait, last_phase)
            carry_over = time.monotonic() - t0
            print(f"Phase {i + 1} Interaktion dauerte {carry_over / 60:.1f} min - wird von naechster Phase abgezogen.")

        print("Alle Phasen abgeschlossen. Territory Battle Sequenz beendet.")
        clear_current_run(stats)

    except Exception as e:
        print(f"Unerwarteter Fehler: {e}")
        raise
    finally:
        is_running = False


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    guild = discord.Object(id=1269591429227745332)
    tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Eingeloggt als {bot.user} (ID: {bot.user.id})")
    print(f"   tw_channel  : {TW_CHANNEL_ID}")
    print(f"   officer     : {OFFICER_ID}")
    print(f"   manager_ids : {MANAGER_IDS or '(keine)'}")
    print(f"   rolle       : {MEMBER_ROLE_ID}")

    # Warn if an interrupted run is detected in stats.json
    stats = load_stats()
    run = stats.get("current_run", {})
    if run.get("active"):
        phase = run.get("phase", 0)
        elapsed = int(time.time()) - run.get("phase_started_at", int(time.time()))
        print(
            f"⚠️  Unterbrochener TB gefunden! "
            f"Phase {phase} war zuletzt aktiv, ~{elapsed // 3600}h {(elapsed % 3600) // 60}min sind vergangen. "
            f"Nutze /resume_tb zum Fortfahren."
        )


@tree.command(name="TBReminder_start", description="Startet die Territory Battle Phasen-Ankuendigungen")
async def start(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    global is_running
    if is_running:
        await interaction.response.send_message(
            "Eine Territory Battle Sequenz laeuft bereits! Warte bis sie abgeschlossen ist.",
            ephemeral=True,
        )
        return

    channel = bot.get_channel(TW_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"Kanal mit ID `{TW_CHANNEL_ID}` nicht gefunden. Bitte `.env` pruefen.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Territory Battle Sequenz gestartet! Nachrichten gehen in {channel.mention}.",
        ephemeral=True,
    )

    is_running = True
    asyncio.create_task(run_sequence(channel))


@tree.command(name="TBReminder_timer", description="Startet die TB-Sequenz automatisch zu einem bestimmten Zeitpunkt")
@app_commands.describe(start_time="Startzeit im Format: DD.MM.YYYY HH:MM (Serverzeit)")
async def start_tb_timer(interaction: discord.Interaction, start_time: str):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    global is_running, pending_timer
    if is_running:
        await interaction.response.send_message(
            "Eine Territory Battle Sequenz laeuft bereits!",
            ephemeral=True,
        )
        return

    if pending_timer and not pending_timer.done():
        await interaction.response.send_message(
            "Es laeuft bereits ein Timer! Nutze `/cancel_tb` um ihn abzubrechen.",
            ephemeral=True,
        )
        return

    try:
        # Parse as local server time (BOT_TZ), not UTC
        target_dt = datetime.strptime(start_time.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=BOT_TZ)
    except ValueError:
        await interaction.response.send_message(
            "Ungültiges Zeitformat. Bitte verwende: `DD.MM.YYYY HH:MM` (z.B. `27.04.2026 18:00`)",
            ephemeral=True,
        )
        return

    now = datetime.now(BOT_TZ)
    wait_seconds = (target_dt - now).total_seconds()

    if wait_seconds <= 0:
        await interaction.response.send_message(
            "Der angegebene Zeitpunkt liegt in der Vergangenheit!",
            ephemeral=True,
        )
        return

    channel = bot.get_channel(TW_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"Kanal mit ID `{TW_CHANNEL_ID}` nicht gefunden. Bitte `.env` pruefen.",
            ephemeral=True,
        )
        return

    target_ts = int(target_dt.timestamp())
    tz_name = target_dt.strftime("%Z")

    # Send confirmation view before setting the timer
    class ConfirmTimerView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.confirmed = False

        @discord.ui.button(label="Bestaetigen", style=discord.ButtonStyle.green)
        async def confirm(self, confirm_interaction: discord.Interaction, button: discord.ui.Button):
            self.confirmed = True
            await confirm_interaction.response.edit_message(
                content=f"✅ Timer gesetzt! TB startet <t:{target_ts}:F> (<t:{target_ts}:R>) in {channel.mention}.",
                view=None,
            )
            self.stop()

        @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.red)
        async def cancel(self, cancel_interaction: discord.Interaction, button: discord.ui.Button):
            self.confirmed = False
            await cancel_interaction.response.edit_message(
                content="Timer abgebrochen.",
                view=None,
            )
            self.stop()

        async def on_timeout(self):
            self.confirmed = False
            try:
                await self.message.edit(content="Keine Bestaetigung - Timer nicht gesetzt.", view=None)
            except Exception:
                pass

    confirm_view = ConfirmTimerView()
    await interaction.response.send_message(
        f"⏰ TB-Timer bestätigen:\n"
        f"Startzeit: **{target_dt.strftime('%d.%m.%Y %H:%M')} {tz_name}** (<t:{target_ts}:R>)\n"
        f"Kanal: {channel.mention}\n\n"
        f"Ist das korrekt?",
        view=confirm_view,
        ephemeral=True,
    )
    confirm_view.message = await interaction.original_response()
    await confirm_view.wait()

    if not confirm_view.confirmed:
        return

    print(f"TB-Timer gesetzt: Start in {wait_seconds / 3600:.2f}h um {target_dt.strftime('%d.%m.%Y %H:%M')} {tz_name}")

    async def delayed_start():
        global is_running, pending_timer
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            print("TB-Timer wurde abgebrochen.")
            return
        if is_running:
            print("TB-Timer abgelaufen, aber Sequenz laeuft bereits - abgebrochen.")
            return
        is_running = True
        await run_sequence(channel)

    pending_timer = asyncio.create_task(delayed_start())


@tree.command(name="TBReminder_resume", description="Setzt eine unterbrochene TB-Sequenz fort")
@app_commands.describe(
    phase="Phase bei der fortgesetzt wird (1-6, welche Phase als naechstes endet)",
    hours_elapsed="Wie viele Stunden der aktuellen Wartezeit bereits vergangen sind (optional, wird aus gespeichertem Status berechnet)"
)
async def resume_tb(interaction: discord.Interaction, phase: int, hours_elapsed: float = 0.0):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    global is_running
    if is_running:
        await interaction.response.send_message(
            "Eine Territory Battle Sequenz laeuft bereits!",
            ephemeral=True,
        )
        return

    if not 1 <= phase <= 6:
        await interaction.response.send_message(
            "Phase muss zwischen 1 und 6 liegen.",
            ephemeral=True,
        )
        return

    stats = load_stats()
    run = stats.get("current_run", {})

    # Auto-calculate elapsed time from persisted timestamp if not manually provided
    if hours_elapsed == 0.0 and run.get("active") and run.get("phase_started_at"):
        hours_elapsed = (int(time.time()) - run["phase_started_at"]) / HOURS
        print(f"Elapsed time aus gespeichertem Status: {hours_elapsed:.2f}h")

    channel = bot.get_channel(TW_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"Kanal mit ID `{TW_CHANNEL_ID}` nicht gefunden.",
            ephemeral=True,
        )
        return

    phase_index = phase - 1
    elapsed_seconds = hours_elapsed * HOURS
    base_wait = 22 * HOURS if phase_index == 0 else 24 * HOURS
    remaining = max(0, base_wait - elapsed_seconds)

    await interaction.response.send_message(
        f"TB-Sequenz wird ab Phase {phase} fortgesetzt.\n"
        f"Verbleibende Wartezeit fuer diese Phase: **{remaining / HOURS:.1f}h**\n"
        f"Nachrichten gehen in {channel.mention}.",
        ephemeral=True,
    )
    print(f"TB resume: Phase {phase}, {hours_elapsed:.2f}h vergangen, {remaining / HOURS:.2f}h verbleibend.")

    is_running = True
    asyncio.create_task(run_sequence(channel, start_phase=phase_index, phase_elapsed=elapsed_seconds))


@tree.command(name="TBReminder_results", description="Zeigt den TB-Abschlussbericht in diesem Kanal an")
async def start_tb_results(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    stats = load_stats()
    total_tbs = stats.get("total_tbs", 0)

    if total_tbs == 0:
        await interaction.response.send_message(
            "Noch keine TB-Daten vorhanden.",
            ephemeral=True,
        )
        return

    tb_index = total_tbs - 1
    await interaction.response.send_message(
        f"Lade TB-Abschlussbericht #{tb_index + 1}...",
        ephemeral=True,
    )

    messages = await build_stats_messages(stats, tb_index)
    for msg in messages:
        await interaction.channel.send(msg)


@tree.command(name="TBReminder_cancel", description="Bricht einen laufenden TB-Timer oder eine aktive TB-Sequenz ab")
async def cancel_tb(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    global is_running, pending_timer

    if pending_timer and not pending_timer.done():
        pending_timer.cancel()
        pending_timer = None
        await interaction.response.send_message(
            "⛔ TB-Timer wurde abgebrochen. Kein Territory Battle wird gestartet.",
            ephemeral=True,
        )
        print("TB-Timer manuell abgebrochen.")
        return

    if is_running:
        is_running = False
        stats = load_stats()
        clear_current_run(stats)
        await interaction.response.send_message(
            "⛔ TB-Sequenz wurde abgebrochen. Stats wurden gespeichert.",
            ephemeral=True,
        )
        print("TB-Sequenz manuell abgebrochen.")
        return

    await interaction.response.send_message(
        "Kein aktiver Timer oder Sequenz gefunden.",
        ephemeral=True,
    )




@tree.command(name="TBReminder_status", description="Zeigt den aktuellen TB-Status: Phasenbeginn, Erinnerungszeit, Phasenende")
async def tb_status(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    stats = load_stats()
    run = stats.get("current_run", {})

    if not run.get("active"):
        await interaction.response.send_message(
            "Kein aktiver Territory Battle.",
            ephemeral=True,
        )
        return

    tb_index   = run.get("tb_index", 0)
    phase      = run.get("phase", 0)
    started_at = run.get("phase_started_at")

    if not started_at:
        await interaction.response.send_message(
            "Status nicht verfuegbar - keine Zeitinformation gespeichert.",
            ephemeral=True,
        )
        return

    phase_duration = 22 * HOURS if phase == 0 else 24 * HOURS
    phase_end_ts   = started_at + int(phase_duration)
    reminder_ts    = phase_end_ts - int(OFFICER_TIMEOUT)  # officer DM fires 1h before phase end

    phase_num = phase + 1  # phase in current_run is the last completed phase, so next is phase+1

    await interaction.response.send_message(
        f"**TB #{tb_index + 1} - Phase {phase_num} laeuft**\n\n"
        f"Phase gestartet:        <t:{started_at}:F>\n"
        f"Officer wird erinnert:  <t:{reminder_ts}:F> (<t:{reminder_ts}:R>)\n"
        f"Phase endet:            <t:{phase_end_ts}:F> (<t:{phase_end_ts}:R>)",
        ephemeral=True,
    )

@tree.command(name="TBReminder_help", description="Zeigt alle verfuegbaren Bot-Befehle und ihre Verwendung")
async def help_command(interaction: discord.Interaction):
    help_text = (
        "## TB-Reminder Bot — Befehlsuebersicht\n\n"

        "### 🟢 TB starten\n"
        "**`/TBReminder_start`**\n"
        "Startet die TB-Sequenz sofort. Der Bot kuendigt den TB-Start im konfigurierten Kanal an "
        "und kontaktiert den Officer automatisch am Ende jeder Phase.\n\n"

        "**`/TBReminder_timer start_time: DD.MM.YYYY HH:MM`**\n"
        "Plant den TB-Start zu einem bestimmten Zeitpunkt (Serverzeit).\n"
        "Der Bot zeigt die geplante Zeit zur Bestaetigung an bevor der Timer gesetzt wird.\n"
        "Beispiel: `/TBReminder_timer start_time: 20.04.2026 18:00`\n\n"

        "### 🔄 TB fortsetzen & Status\n"
        "**`/TBReminder_resume phase: <1-6> [hours_elapsed: <Stunden>]`**\n"
        "Setzt eine unterbrochene TB-Sequenz fort (z.B. nach Server-Neustart).\n"
        "`phase` = welche Phase als naechstes endet.\n"
        "`hours_elapsed` = wie viele Stunden der aktuellen Wartezeit bereits vergangen sind. "
        "Wird automatisch aus dem gespeicherten Status berechnet, falls vorhanden.\n"
        "Beispiel: `/TBReminder_resume phase: 6 hours_elapsed: 19.5`\n\n"

        "**`/TBReminder_status`**\n"
        "Zeigt den aktuellen TB-Status: wann die Phase gestartet ist, wann der Officer erinnert wird, wann die Phase endet.\n\n"

        "### 📊 Ergebnisse\n"
        "**`/TBReminder_results`**\n"
        "Postet den Abschlussbericht des letzten TBs in diesen Kanal. "
        "Zeigt Erinnerungen und nicht-stationierte Spieler mit Gesamtquoten.\n\n"

        "### ⛔ Abbrechen\n"
        "**`/TBReminder_cancel`**\n"
        "Bricht einen laufenden Timer oder eine aktive TB-Sequenz ab.\n\n"

        "### ℹ️ Sonstiges\n"
        "**`/TBReminder_help`**\n"
        "Zeigt diese Uebersicht.\n\n"

        "-# Alle Befehle erfordern Administrator-Rechte oder Officer-Status."
    )
    await interaction.response.send_message(help_text, ephemeral=True)


bot.run(TOKEN)