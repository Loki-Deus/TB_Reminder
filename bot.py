import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import time
import traceback
from datetime import datetime

import config
import roster_read
import requirements
from stats import (
    load_stats,
    save_stats,
    get_tb_index,
    set_current_run,
    clear_current_run,
    record_participation,
    record_reminders,
    record_failed,
)

TOKEN           = config.TOKEN
GUILD_ID        = config.GUILD_ID
TW_CHANNEL_ID   = config.TW_CHANNEL_ID
OFFICER_ID      = config.OFFICER_ID
MANAGER_IDS     = config.MANAGER_IDS
MEMBER_ROLE_ID  = config.MEMBER_ROLE_ID
HOURS           = config.HOURS
OFFICER_TIMEOUT = config.OFFICER_TIMEOUT
BOT_TZ          = config.BOT_TZ
PHASE_END_MESSAGES = config.PHASE_END_MESSAGES
GENERIC_REMINDER   = config.GENERIC_REMINDER

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

is_running = False
pending_timer: asyncio.Task | None = None
running_task: asyncio.Task | None = None
_startup_recovery_done = False  # verhindert Re-Trigger bei Gateway-Reconnects
# Zwischenspeicher für die Phasenend-Nachprüfung (siehe handle_phase_end):
# der zuletzt per /tbreminder_planets_check geprüfte Planet dieser Phase,
# zurückgesetzt bei jedem neuen Phasenübergang in run_sequence.
_last_checked_planet: str | None = None

# stats.json-Struktur und alle Buchhaltungsfunktionen: siehe stats.py.


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

        # ── Platoon-Nachprüfung gegen die Erinnerungsauswahl ───────────────
        # Wenn während dieser Phase /tbreminder_planets_check gelaufen ist
        # (siehe _last_checked_planet, gesetzt dort, zurückgesetzt am Anfang
        # jeder neuen Phase in run_sequence), wird hier automatisch
        # nachgeprüft, wie viele der ursprünglich verfügbaren Besitzer auch
        # tatsächlich in der gerade getroffenen Erinnerungsauswahl stecken --
        # kein separater Befehl nötig, Byproduct des DM-Schritts oben.
        if _last_checked_planet:
            reminded_ids = {uid for uid, _ in reminded}
            try:
                refined = requirements.compute_shortfall(_last_checked_planet)
            except roster_read.RosterUnavailableError as e:
                print(f"Platoon-Nachprüfung übersprungen (Roster nicht erreichbar): {e}")
            else:
                refinement_text = format_platoon_refinement(
                    _last_checked_planet, refined, reminded_ids
                )
                for chunk in split_message(refinement_text):
                    await officer.send(chunk)
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


def split_message(text: str, limit: int = 1990) -> list[str]:
    """
    Split a potentially-oversized message into chunks that fit within Discord's
    2000-character limit. Splits on newlines so rows are never cut mid-line.
    Reopens/closes code fences across chunks when the source is a code block.
    """
    in_code_block = text.startswith("```")
    fence_open  = "```\n" if in_code_block else ""
    fence_close = "\n```" if in_code_block else ""

    inner = text[len(fence_open) : len(text) - len(fence_close)] if in_code_block else text

    lines   = inner.split("\n")
    chunks  = []
    current_lines: list[str] = []
    current_len = len(fence_open) + len(fence_close)

    for line in lines:
        added = len(line) + 1  # +1 for the \n that join() will add
        if current_len + added > limit and current_lines:
            chunks.append(fence_open + "\n".join(current_lines) + fence_close)
            current_lines = [line]
            current_len   = len(fence_open) + len(fence_close) + added
        else:
            current_lines.append(line)
            current_len += added

    if current_lines:
        chunks.append(fence_open + "\n".join(current_lines) + fence_close)

    return chunks


async def send_stats_summary(officer: discord.User, stats: dict, tb_index: int):
    """DM the officer the stats summary. Called automatically at end of TB."""
    messages = await build_stats_messages(stats, tb_index)
    for msg in messages:
        for chunk in split_message(msg):
            await officer.send(chunk)


async def run_sequence(tw_channel: discord.TextChannel, start_phase: int = 0, phase_elapsed: float = 0.0):
    """
    Main TB sequence.
    start_phase:    phase index (0-5) to start from. Used by /resume_tb.
    phase_elapsed:  seconds already elapsed in the current phase wait. Used by /resume_tb.
    """
    global is_running, _last_checked_planet
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
            # Neue Wartezeit, neue Phase -- ein zuvor gecachter
            # /tbreminder_planets_check gehört zur vorherigen Phase und
            # darf hier nicht mehr als "aktuell" gelten (siehe
            # handle_phase_end's Nachprüfung unten).
            _last_checked_planet = None

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

            if not is_running:
                print(f"TB-Sequenz wurde abgebrochen (nach Sleep Phase {i + 1}).")
                return

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
        await notify_failure(e)
        raise
    finally:
        is_running = False


# ── Zuverlässigkeit: Task-Überwachung & Crash-Benachrichtigung ─────────────────

async def notify_failure(exc: Exception):
    """
    Wird aufgerufen, wenn run_sequence mit einer unbehandelten Exception
    abbricht -- egal ob das den Bot-Prozess selbst mit reißt oder nicht.
    Ohne das: die Exception verschwindet in den Container-Logs (asyncio
    protokolliert "exception was never retrieved" höchstens beim nächsten
    Garbage-Collect), und in Discord passiert schlicht nichts mehr -- der
    Officer merkt das erst, wenn die erwartete DM ausbleibt.

    Best effort: schlägt sowohl der Kanal-Post als auch die Officer-DM
    fehl (z.B. weil der Discord-Gateway selbst die Ursache der Exception
    war), wird das geloggt, aber nichts weiter unternommen -- es gibt
    keinen zuverlässigeren Kanal mehr, über den der Bot sich melden könnte.
    """
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"TB-Sequenz abgestürzt:\n{tb_text}")

    message = (
        f"❌ Die TB-Sequenz ist mit einem Fehler abgebrochen: `{exc}`\n"
        f"Der Bot läuft weiter, die Sequenz nicht mehr. Bitte `/tbreminder_status` "
        f"prüfen und ggf. `/tbreminder_resume` nutzen."
    )
    try:
        channel = bot.get_channel(TW_CHANNEL_ID)
        if channel:
            await channel.send(message)
    except Exception as notify_exc:
        print(f"Konnte Absturz nicht im Kanal melden: {notify_exc}")

    try:
        officer = await bot.fetch_user(OFFICER_ID)
        await officer.send(message)
    except Exception as notify_exc:
        print(f"Konnte Absturz nicht per DM an Officer melden: {notify_exc}")


def launch_run_sequence(*args, **kwargs) -> asyncio.Task:
    """
    Einziger Erzeugungspunkt für die run_sequence-Task -- ersetzt die
    bisher drei separaten asyncio.create_task(run_sequence(...))-Aufrufe
    (Start, Timer, Resume). run_sequence fängt seine eigenen Exceptions
    bereits ab und meldet sie über notify_failure() (siehe oben); dieser
    zusätzliche add_done_callback ist die zweite Sicherheitsebene für den
    Fall, dass eine Exception die Sequenz VOR dem try-Block verlässt, oder
    dass die Task extern gecancelt statt regulär beendet wird (kein
    Alarmfall, wird hier bewusst ignoriert).
    """
    task = asyncio.create_task(run_sequence(*args, **kwargs))

    def _on_done(t: asyncio.Task):
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            asyncio.create_task(notify_failure(exc))

    task.add_done_callback(_on_done)
    return task


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _startup_recovery_done, is_running, running_task

    guild = discord.Object(id=GUILD_ID)
    tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Eingeloggt als {bot.user} (ID: {bot.user.id})")
    print(f"   guild_id    : {GUILD_ID}")
    print(f"   tw_channel  : {TW_CHANNEL_ID}")
    print(f"   officer     : {OFFICER_ID}")
    print(f"   manager_ids : {MANAGER_IDS or '(keine)'}")
    print(f"   rolle       : {MEMBER_ROLE_ID}")

    # on_ready kann bei Gateway-Reconnects mehrfach feuern -- die Recovery
    # darf nur beim allerersten Verbindungsaufbau laufen, sonst würde ein
    # normaler Reconnect während einer laufenden Sequenz fälschlich einen
    # zweiten run_sequence-Task neben dem bereits laufenden starten.
    if _startup_recovery_done:
        return
    _startup_recovery_done = True

    stats = load_stats()
    run = stats.get("current_run", {})
    if not run.get("active"):
        return

    phase = run.get("phase", 0)
    started_at = run.get("phase_started_at")
    channel_id = run.get("channel_id", TW_CHANNEL_ID)

    if not started_at:
        # Kein Zeitstempel vorhanden (z.B. sehr alte stats.json von vor
        # dieser Funktion) -- Recovery kann die Wartezeit nicht berechnen,
        # bleibt beim reinen Log-Hinweis wie bisher. Manueller
        # /tbreminder_resume mit explizitem hours_elapsed bleibt der Weg.
        print(
            f"⚠️  Unterbrochener TB gefunden (Phase {phase}), aber kein "
            f"Zeitstempel gespeichert -- automatische Recovery nicht möglich. "
            f"Bitte /tbreminder_resume mit explizitem hours_elapsed nutzen."
        )
        return

    elapsed = int(time.time()) - started_at
    channel = bot.get_channel(channel_id)

    print(
        f"⚠️  Unterbrochener TB gefunden! Phase {phase} war zuletzt aktiv, "
        f"~{elapsed // 3600}h {(elapsed % 3600) // 60}min sind vergangen. "
        f"Sequenz wird automatisch ab Phase {phase + 1} fortgesetzt."
    )

    if channel is None:
        print(f"Kanal {channel_id} nicht gefunden -- automatische Recovery abgebrochen.")
        try:
            officer = await bot.fetch_user(OFFICER_ID)
            await officer.send(
                f"⚠️ TB-Reminder wurde neu gestartet und hat eine unterbrochene Sequenz "
                f"gefunden (Phase {phase}), konnte den TB-Kanal (`{channel_id}`) aber "
                f"nicht finden. Bitte `/tbreminder_resume` manuell ausführen."
            )
        except Exception as notify_exc:
            print(f"Konnte Officer nicht benachrichtigen: {notify_exc}")
        return

    resume_message = (
        f"⚠️ TB-Reminder wurde neu gestartet. Letzter bekannter Stand: "
        f"Phase {phase}, ~{elapsed // 3600}h {(elapsed % 3600) // 60}min vergangen. "
        f"Sequenz wird automatisch ab Phase {phase + 1} fortgesetzt."
    )
    try:
        await channel.send(resume_message)
    except Exception as notify_exc:
        print(f"Konnte Kanal nicht benachrichtigen: {notify_exc}")

    try:
        officer = await bot.fetch_user(OFFICER_ID)
        await officer.send(resume_message)
    except Exception as notify_exc:
        print(f"Konnte Officer nicht per DM benachrichtigen: {notify_exc}")

    is_running = True
    running_task = launch_run_sequence(channel, start_phase=phase, phase_elapsed=float(elapsed))


@tree.command(name="tbreminder_start", description="Startet die Territory Battle Phasen-Ankuendigungen")
async def start(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    global is_running, running_task
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
    running_task = launch_run_sequence(channel)


@tree.command(name="tbreminder_timer", description="Startet die TB-Sequenz automatisch zu einem bestimmten Zeitpunkt")
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
        global is_running, pending_timer, running_task
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            print("TB-Timer wurde abgebrochen.")
            return
        if is_running:
            print("TB-Timer abgelaufen, aber Sequenz laeuft bereits - abgebrochen.")
            return
        is_running = True
        running_task = launch_run_sequence(channel)

    pending_timer = asyncio.create_task(delayed_start())


@tree.command(name="tbreminder_resume", description="Setzt eine unterbrochene TB-Sequenz fort")
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

    global is_running, running_task
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
    running_task = launch_run_sequence(channel, start_phase=phase_index, phase_elapsed=elapsed_seconds)


@tree.command(name="tbreminder_results", description="Zeigt den TB-Abschlussbericht in diesem Kanal an")
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
        for chunk in split_message(msg):
            await interaction.channel.send(chunk)


@tree.command(name="tbreminder_cancel", description="Bricht einen laufenden TB-Timer oder eine aktive TB-Sequenz ab")
async def cancel_tb(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    global is_running, pending_timer, running_task

    if pending_timer and not pending_timer.done():
        pending_timer.cancel()
        pending_timer = None
        await interaction.response.send_message(
            "⛔ TB-Timer wurde abgebrochen. Kein Territory Battle wird gestartet.",
            ephemeral=True,
        )
        print("TB-Timer manuell abgebrochen.")
        return

    if is_running or (running_task and not running_task.done()):
        is_running = False

        if running_task and not running_task.done():
            running_task.cancel()
            running_task = None

        stats = load_stats()
        run = stats.get("current_run", {})

        if run.get("phase", 0) == 0:
            # No officer interaction has happened yet — roll back completely
            tb_index = run.get("tb_index")
            if tb_index is not None:
                for player in stats.get("players", {}).values():
                    if len(player.get("tb_history", [])) > tb_index:
                        player["tb_history"].pop(tb_index)
                    if len(player.get("failed_history", [])) > tb_index:
                        player["failed_history"].pop(tb_index)
                    if player.get("total_tbs", 0) > 0:
                        player["total_tbs"] -= 1
                stats["total_tbs"] = max(0, stats.get("total_tbs", 1) - 1)
            clear_current_run(stats)
            await interaction.response.send_message(
                "⛔ TB-Sequenz abgebrochen. Noch keine Daten vorhanden — TB-Zaehler zurueckgesetzt.",
                ephemeral=True,
            )
            print("TB-Sequenz abgebrochen vor Phase 1 - Stats zurueckgerollt.")
        else:
            # Data exists — keep it, just stop the sequence
            clear_current_run(stats)
            await interaction.response.send_message(
                "⛔ TB-Sequenz abgebrochen. Vorhandene Daten wurden behalten. Nutze `/tbreminder_resume` zum Fortfahren.",
                ephemeral=True,
            )
            print(f"TB-Sequenz abgebrochen nach Phase {run.get('phase')} - Stats behalten.")
        return

    await interaction.response.send_message(
        "Kein aktiver Timer oder Sequenz gefunden.",
        ephemeral=True,
    )




@tree.command(name="tbreminder_status", description="Zeigt den aktuellen TB-Status: Phasenbeginn, Erinnerungszeit, Phasenende")
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

# ── Platoon-Feature: Anforderungen, Check, Ping ────────────────────────────────
# Liest read-only aus TW-Counters counters.db (roster_read.py), Anforderungen
# kommen aus einer einmal hochgeladenen CSV (requirements.py). Kein
# Phasen-/Layout-Konzept -- siehe requirements.py-Docstring.


async def planet_autocomplete(interaction: discord.Interaction, current: str):
    try:
        planets = requirements.get_planets()
    except Exception as e:
        print(f"Planet-Autocomplete fehlgeschlagen: {e}")
        return []
    return [
        app_commands.Choice(name=p, value=p)
        for p in planets
        if current.lower() in p.lower()
    ][:25]


async def unit_autocomplete(interaction: discord.Interaction, current: str):
    """Für /tbreminder_units_ping -- nicht an die Anforderungsliste
    gebunden, sondern an alles, was mindestens ein Gildenmitglied laut
    Roster tatsächlich besitzt (siehe roster_read.get_owned_unit_display_names())."""
    try:
        names = roster_read.get_owned_unit_display_names()
    except roster_read.RosterUnavailableError as e:
        print(f"Unit-Autocomplete fehlgeschlagen: {e}")
        return []
    return [
        app_commands.Choice(name=n, value=n)
        for n in names
        if current.lower() in n.lower()
    ][:25]


def _format_owner_names(row: dict) -> str:
    """Gemeinsame Besitzer-Namensformatierung für beide Platoon-Textausgaben.
    Bei Schiffen wird kein '(RX)' angehängt -- relic_tier ist dort immer
    None (siehe requirements.compute_shortfall()), und 'R0' würde
    fälschlich so lesen, als gäbe es ein Relic-Level 0."""
    if row.get("is_ship"):
        return ", ".join(o["player_name"] for o in row["owners"])
    return ", ".join(
        f"{o['player_name']} (R{config.relic_tier_to_display(o['relic_tier'])})"
        for o in row["owners"]
    )


def format_platoon_report(planet: str, rows: list[dict]) -> str:
    """Textausgabe für /tbreminder_planets_check -- diagnostisch,
    Verfügbar-/Fehlend-Aufschlüsselung pro Einheit, keine Buttons.

    Namen der Besitzer werden NUR bei tatsächlicher Unterdeckung
    aufgelistet -- bei ausreichender Deckung reicht die Zahl. Ohne das
    sprengt ein Planet mit vielen breit besessenen Einheiten (jede mit
    einer vollen Namensliste, obwohl längst gedeckt) Discords 2000-
    Zeichen-Limit über mehrere Nachrichten hinweg, ohne dass die
    zusätzliche Information irgendeinen diagnostischen Wert hätte --
    genau der Fall, der live aufgefallen ist (siehe Chat-Verlauf)."""
    lines = [f"📋 {planet} — Platoons\n"]
    for row in rows:
        marker = "⚠️" if row["shortfall"] > 0 else "✅"
        lines.append(
            f"{marker} {row['unit_name']} "
            f"(benötigt Relic {row['required_relic']}, ×{row['required_count']})"
        )
        owners = row["owners"]
        plural = "er" if len(owners) != 1 else ""
        if row.get("is_ship"):
            lines.append(f"   Verfügbar: {len(owners)} Mitglied{plural} (Schiff, kein Relic-Erfordernis)")
        else:
            lines.append(
                f"   Verfügbar: {len(owners)} Mitglied{plural} auf Relic {row['required_relic']}+"
            )
        if row["shortfall"] > 0 and owners:
            lines.append(f"   → {_format_owner_names(row)}")
        if row["shortfall"] > 0:
            lines.append(f"   Fehlend: {row['shortfall']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_platoon_refinement(planet: str, rows: list[dict], reminded_ids: set[str]) -> str:
    """
    Automatische Phasenend-Nachprüfung (siehe handle_phase_end): dieselben
    Zeilen wie format_platoon_report(), aber gefiltert auf Besitzer, deren
    discord_id in der gerade getroffenen Erinnerungsauswahl steckt --
    "wer ist von den ursprünglich verfügbaren Besitzern auch tatsächlich
    noch als aktiv markiert".
    """
    lines = [f"📋 {planet} — Nachgeprüft gegen deine Auswahl\n"]
    for row in rows:
        available = [o for o in row["owners"] if o["discord_id"] in reminded_ids]
        marker = "⚠️" if len(available) < row["required_count"] else "✅"
        if len(available) < row["required_count"]:
            lines.append(
                f"{marker} {row['unit_name']}: nur noch {len(available)} von "
                f"{len(row['owners'])} verfügbaren Besitzern in deiner Auswahl"
            )
        else:
            lines.append(f"{marker} {row['unit_name']}: weiterhin ausreichend gedeckt")
    return "\n".join(lines).rstrip()


class PlatoonPingView(discord.ui.View):
    """
    Ein Button pro Einheit -- unabhängig vom Fehlbestand, auch bei voller
    Deckung (live aufgefallen: die ursprüngliche "nur bei Unterdeckung"-
    Variante ließ bei einem vollständig gedeckten Planeten gar keine
    Einzel-Buttons mehr übrig, nur noch "Alle pingen", siehe Chat-Verlauf).
    Jeder Button ist genau einmal auslösbar, danach deaktiviert. Kein
    Officer-Timeout -- Ad-hoc-Aktion während einer laufenden Phase.

    Trägt NUR Einzel-Buttons, kein "Alle pingen" mehr -- das sitzt jetzt
    in AllUnitsPingView (siehe unten), als eigene, letzte Nachricht.
    Grund: Discord erlaubt maximal 25 Komponenten pro Nachricht (5 Reihen
    x 5), ROTE-Planeten haben aber 35-58 verschiedene Einheiten -- die
    Einzel-Buttons müssen deshalb auf mehrere Nachrichten/View-Instanzen
    verteilt werden (siehe planets_ping() unten), und "Alle pingen" muss
    dafür unabhängig von einer bestimmten Buttons-Nachricht funktionieren,
    nicht an eine einzelne von mehreren PlatoonPingView-Instanzen gebunden
    sein.
    """

    def __init__(self, planet: str, rows: list[dict]):
        super().__init__(timeout=None)
        self.planet = planet

        for row in rows:
            btn = discord.ui.Button(
                label=f"Für {row['unit_name']} pingen",
                style=discord.ButtonStyle.primary,
            )
            btn.callback = self._make_unit_callback(row, btn)
            self.add_item(btn)

    def _make_unit_callback(self, row: dict, btn: discord.ui.Button):
        async def _callback(interaction: discord.Interaction):
            mentions = [f"<@{o['discord_id']}>" for o in row["owners"] if o["discord_id"]]
            if not mentions:
                extra = "" if row.get("is_ship") else " auf ausreichendem Relic-Level"
                await interaction.response.send_message(
                    f"Niemand mit registriertem Discord-Account besitzt "
                    f"{row['unit_name']}{extra}.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"{' '.join(mentions)}\n"
                f"Bitte für **{self.planet}** bereithalten (**{row['unit_name']}**)."
            )
            btn.disabled = True
            await interaction.message.edit(view=self)

        return _callback


class AllUnitsPingView(discord.ui.View):
    """
    Eigenständige, einzelne "Alle pingen"-Schaltfläche -- pingt die
    Vereinigung aller registrierten Besitzer über ALLE Zeilen eines
    Planeten, unabhängig davon, auf wie viele PlatoonPingView-Nachrichten
    die Einzel-Buttons verteilt sind (siehe dortige Docstring). Kein
    Tracking gegen Doppel-Pings über die Einzel-Buttons hinweg (bewusst
    einfache Variante, siehe Chat-Verlauf).
    """

    def __init__(self, planet: str, all_rows: list[dict]):
        super().__init__(timeout=None)
        self.planet = planet
        self.all_rows = all_rows

    @discord.ui.button(label="Alle pingen", style=discord.ButtonStyle.success)
    async def ping_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        seen: set[str] = set()
        mentions = []
        for row in self.all_rows:
            for o in row["owners"]:
                if o["discord_id"] and o["discord_id"] not in seen:
                    seen.add(o["discord_id"])
                    mentions.append(f"<@{o['discord_id']}>")

        if not mentions:
            await interaction.response.send_message(
                "Niemand mit registriertem Discord-Account gefunden.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"{' '.join(mentions)}\nBitte für **{self.planet}** bereithalten."
        )
        button.disabled = True
        await interaction.message.edit(view=self)


class UnitPingView(discord.ui.View):
    """
    Einzelner Ping-Button für /tbreminder_units_ping -- ad-hoc, eine
    Einheit, ein Ergebnis. Einfacher als PlatoonPingView (kein
    "Alle pingen" nötig, da es nur eine Einheit gibt), gleiches
    Prinzip: einmalig auslösbar, kein Officer-Timeout.
    """

    def __init__(self, unit_name: str, owners: list, is_ship: bool):
        super().__init__(timeout=None)
        self.unit_name = unit_name
        self.owners = owners
        self.is_ship = is_ship

    @discord.ui.button(label="Pingen", style=discord.ButtonStyle.primary)
    async def ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        mentions = [f"<@{o['discord_id']}>" for o in self.owners if o["discord_id"]]
        if not mentions:
            await interaction.response.send_message(
                f"Niemand mit registriertem Discord-Account besitzt "
                f"{self.unit_name}" + ("" if self.is_ship else " auf ausreichendem Relic-Level") + ".",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"{' '.join(mentions)}\nBitte für **{self.unit_name}** bereithalten."
        )
        button.disabled = True
        await interaction.message.edit(view=self)


class ConfirmReplaceView(discord.ui.View):
    """Bestätigungsdialog für /tbreminder_requirements_upload, wenn
    bereits eine Anforderungsliste existiert -- analog zu ConfirmTimerView
    oben. Destruktive Aktion (vollständiges Ersetzen), daher explizite
    Bestätigung statt stillem Überschreiben."""

    def __init__(self):
        super().__init__(timeout=60)
        self.confirmed = False

    @discord.ui.button(label="Ersetzen", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(content="Wird ersetzt...", view=None)
        self.stop()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await interaction.response.edit_message(content="Abgebrochen.", view=None)
        self.stop()

    async def on_timeout(self):
        self.confirmed = False
        try:
            await self.message.edit(content="Keine Bestaetigung - Upload abgebrochen.", view=None)
        except Exception:
            pass


@tree.command(
    name="tbreminder_requirements_upload",
    description="Lädt die Platoon-Anforderungsliste hoch (ersetzt eine bestehende vollständig)",
)
@app_commands.describe(
    file="CSV mit Spalten: planet, unit_name, required_count, required_relic"
)
async def requirements_upload(interaction: discord.Interaction, file: discord.Attachment):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        raw = await file.read()
    except Exception as e:
        await interaction.followup.send(f"Konnte Datei nicht lesen: {e}", ephemeral=True)
        return

    try:
        new_data = requirements.parse_csv(raw)
    except requirements.RequirementsParseError as e:
        await interaction.followup.send(f"CSV fehlerhaft:\n```\n{e}\n```", ephemeral=True)
        return
    except roster_read.RosterUnavailableError as e:
        await interaction.followup.send(f"Rosterdaten nicht erreichbar: {e}", ephemeral=True)
        return

    total_new_rows = sum(len(v) for v in new_data.values())
    existing = requirements.load_requirements()

    if existing:
        total_old_rows = sum(len(v) for v in existing.values())
        view = ConfirmReplaceView()
        await interaction.followup.send(
            f"Es existiert bereits eine Anforderungsliste "
            f"({len(existing)} Planeten, {total_old_rows} Zeilen). "
            f"Mit der neuen Datei ersetzen ({len(new_data)} Planeten, "
            f"{total_new_rows} Zeilen)?",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()
        await view.wait()
        if not view.confirmed:
            return

    backup_path = requirements.replace_requirements(new_data)
    msg = f"✅ Anforderungsliste gespeichert: {len(new_data)} Planeten, {total_new_rows} Zeilen."
    if backup_path:
        msg += f"\nVorherige Version gesichert unter `{os.path.basename(backup_path)}`."
    await interaction.followup.send(msg, ephemeral=True)


@tree.command(
    name="tbreminder_planets_check",
    description="Zeigt Fehlbestand pro Einheit für einen Planeten (diagnostisch, keine Ping-Buttons)",
)
@app_commands.describe(planet="Planet, wie in der Anforderungsliste hinterlegt")
@app_commands.autocomplete(planet=planet_autocomplete)
async def planets_check(interaction: discord.Interaction, planet: str):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    rows = requirements.get_planet_requirements(planet)
    if not rows:
        await interaction.response.send_message(
            f"Keine Anforderungen für Planet '{planet}' hinterlegt. "
            f"Erst `/tbreminder_requirements_upload` ausführen.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        shortfall_rows = requirements.compute_shortfall(planet)
    except roster_read.RosterUnavailableError as e:
        await interaction.followup.send(f"Rosterdaten nicht erreichbar: {e}")
        return

    global _last_checked_planet
    _last_checked_planet = planet

    text = format_platoon_report(planet, shortfall_rows)
    for chunk in split_message(text):
        await interaction.followup.send(chunk)


@tree.command(
    name="tbreminder_planets_ping",
    description="Ruft alle Besitzer der benötigten Einheiten eines Planeten zur Teilnahme auf",
)
@app_commands.describe(planet="Planet, wie in der Anforderungsliste hinterlegt")
@app_commands.autocomplete(planet=planet_autocomplete)
async def planets_ping(interaction: discord.Interaction, planet: str):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    rows = requirements.get_planet_requirements(planet)
    if not rows:
        await interaction.response.send_message(
            f"Keine Anforderungen für Planet '{planet}' hinterlegt. "
            f"Erst `/tbreminder_requirements_upload` ausführen.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        shortfall_rows = requirements.compute_shortfall(planet)
    except roster_read.RosterUnavailableError as e:
        await interaction.followup.send(f"Rosterdaten nicht erreichbar: {e}")
        return

    # Gleicher Text wie /tbreminder_planets_check -- der ausschlaggebende
    # Unterschied zu vorher, siehe Chat-Verlauf: der Officer wollte die
    # vertraute Verfügbar-/Fehlend-Ansicht, nicht die kompakte
    # (N fehlt)/(ausreichend)-Variante, nur eben zusätzlich mit
    # Ping-Buttons.
    text = format_platoon_report(planet, shortfall_rows)
    for chunk in split_message(text):
        await interaction.followup.send(chunk)

    # Ping-Buttons getrennt von der Textausgabe verschickt, in Gruppen zu
    # maximal 25 (Discords Hartlimit pro Nachricht) -- ein Planet mit
    # vielen Einheiten (ROTE: 35-58) sprengt das in einer einzigen
    # Nachricht locker. "Alle pingen" kommt am Ende als eigene, letzte
    # Nachricht, unabhängig davon, wie viele Einzel-Buttons-Nachrichten
    # davor stehen -- siehe AllUnitsPingView-Docstring.
    DISCORD_MAX_COMPONENTS = 25
    total = len(shortfall_rows)
    for i in range(0, total, DISCORD_MAX_COMPONENTS):
        chunk_rows = shortfall_rows[i:i + DISCORD_MAX_COMPONENTS]
        view = PlatoonPingView(planet, chunk_rows)
        label = f"Ping-Buttons ({i + 1}–{i + len(chunk_rows)} von {total}):"
        await interaction.followup.send(label, view=view)

    await interaction.followup.send(
        "Oder alle Besitzer aller gelisteten Einheiten auf einmal pingen:",
        view=AllUnitsPingView(planet, shortfall_rows),
    )


async def _lookup_unit(unit: str, relic: int):
    """
    Gemeinsame Auflösungs-/Abfragelogik für /tbreminder_units_check und
    /tbreminder_units_ping -- beide brauchen exakt dasselbe (Einheit
    auflösen, Schiff erkennen, Besitzer abfragen, Anzeige-Zeilen bauen),
    nur der Ping-Button unterscheidet sie. Eine Funktion statt zweier
    Kopien, damit sich das Verhalten nicht auseinanderentwickeln kann.

    Rückgabe: (lines, owners, is_ship) bei Erfolg, oder (error_text, None,
    None) wenn die Einheit nicht aufgelöst werden konnte oder die
    Rosterdaten nicht erreichbar sind -- der Aufrufer unterscheidet das
    am zweiten Rückgabewert (owners is None -> error_text direkt senden).
    """
    try:
        unit_id = roster_read.resolve_unit_id(unit)
    except roster_read.RosterUnavailableError as e:
        return f"Rosterdaten nicht erreichbar: {e}", None, None

    if unit_id is None:
        return (
            f"'{unit}' konnte nicht aufgelöst werden -- besitzt sie laut "
            f"letztem Roster-Refresh mindestens ein Gildenmitglied?"
        ), None, None

    is_ship = unit in requirements.SHIP_UNIT_NAMES
    if is_ship:
        owners = roster_read.get_owners_of_unit_ignore_relic(unit_id)
    else:
        owners = roster_read.get_owners_of_unit(unit_id, config.display_relic_to_raw(relic))

    lines = [f"🔍 {unit}"]
    plural = "er" if len(owners) != 1 else ""
    if is_ship:
        if relic:
            lines.append("(Relic-Angabe ignoriert -- Schiffe haben kein Relic-System)")
        lines.append(f"Verfügbar: {len(owners)} Mitglied{plural}")
    else:
        lines.append(f"Verfügbar: {len(owners)} Mitglied{plural} auf Relic {relic}+")
    if owners:
        fake_row = {"is_ship": is_ship, "owners": owners}
        lines.append(f"→ {_format_owner_names(fake_row)}")

    return lines, owners, is_ship


@tree.command(
    name="tbreminder_units_check",
    description="Zeigt Besitzer einer Einheit ab einem Relic-Level (diagnostisch, keine Ping-Buttons)",
)
@app_commands.describe(
    unit="Einheit (nur was mindestens ein Gildenmitglied laut Roster besitzt)",
    relic="Mindest-Relic-Level (0-20). Wird für Schiffe ignoriert, da diese kein Relic-System haben.",
)
@app_commands.autocomplete(unit=unit_autocomplete)
async def units_check(
    interaction: discord.Interaction,
    unit: str,
    relic: app_commands.Range[int, 0, 20] = 0,
):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    lines, owners, is_ship = await _lookup_unit(unit, relic)
    if owners is None:
        await interaction.followup.send(lines)  # lines is the error text here
        return

    await interaction.followup.send("\n".join(lines))


@tree.command(
    name="tbreminder_units_ping",
    description="Zeigt und pingt Besitzer einer Einheit ab einem Relic-Level (unabhängig von der Anforderungsliste)",
)
@app_commands.describe(
    unit="Einheit (nur was mindestens ein Gildenmitglied laut Roster besitzt)",
    relic="Mindest-Relic-Level (0-20). Wird für Schiffe ignoriert, da diese kein Relic-System haben.",
)
@app_commands.autocomplete(unit=unit_autocomplete)
async def units_ping(
    interaction: discord.Interaction,
    unit: str,
    relic: app_commands.Range[int, 0, 20] = 0,
):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoatigst Administrator-Rechte oder Officer-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    lines, owners, is_ship = await _lookup_unit(unit, relic)
    if owners is None:
        await interaction.followup.send(lines)  # lines is the error text here
        return

    view = UnitPingView(unit, owners, is_ship)
    await interaction.followup.send("\n".join(lines), view=view)


@tree.command(name="tbreminder_help", description="Zeigt alle verfuegbaren Bot-Befehle und ihre Verwendung")
async def help_command(interaction: discord.Interaction):
    help_text = (
        "## TB-Reminder Bot — Befehlsuebersicht\n\n"

        "### 🟢 TB starten\n"
        "**`/tbreminder_start`**\n"
        "Startet die TB-Sequenz sofort. Der Bot kuendigt den TB-Start im konfigurierten Kanal an "
        "und kontaktiert den Officer automatisch am Ende jeder Phase.\n\n"

        "**`/tbreminder_timer start_time: DD.MM.YYYY HH:MM`**\n"
        "Plant den TB-Start zu einem bestimmten Zeitpunkt (Serverzeit).\n"
        "Der Bot zeigt die geplante Zeit zur Bestaetigung an bevor der Timer gesetzt wird.\n"
        "Beispiel: `/tbreminder_timer start_time: 20.04.2026 18:00`\n\n"

        "### 🔄 TB fortsetzen & Status\n"
        "**`/tbreminder_resume phase: <1-6> [hours_elapsed: <Stunden>]`**\n"
        "Setzt eine unterbrochene TB-Sequenz fort (z.B. nach Server-Neustart).\n"
        "`phase` = welche Phase als naechstes endet.\n"
        "`hours_elapsed` = wie viele Stunden der aktuellen Wartezeit bereits vergangen sind. "
        "Wird automatisch aus dem gespeicherten Status berechnet, falls vorhanden.\n"
        "Beispiel: `/tbreminder_resume phase: 6 hours_elapsed: 19.5`\n\n"

        "**`/tbreminder_status`**\n"
        "Zeigt den aktuellen TB-Status: wann die Phase gestartet ist, wann der Officer erinnert wird, wann die Phase endet.\n\n"

        "### 📊 Ergebnisse\n"
        "**`/tbreminder_results`**\n"
        "Postet den Abschlussbericht des letzten TBs in diesen Kanal. "
        "Zeigt Erinnerungen und nicht-stationierte Spieler mit Gesamtquoten.\n\n"

        "### ⛔ Abbrechen\n"
        "**`/tbreminder_cancel`**\n"
        "Bricht einen laufenden Timer oder eine aktive TB-Sequenz ab.\n\n"

        "### 🪖 Platoons\n"
        "**`/tbreminder_requirements_upload file`**\n"
        "Lädt die Platoon-Anforderungsliste hoch (CSV: planet, unit_name, "
        "required_count, required_relic). Ersetzt eine bestehende Liste "
        "vollständig, mit Bestätigung und automatischem Backup.\n\n"

        "**`/tbreminder_planets_check planet`**\n"
        "Zeigt Fehlbestand pro Einheit für einen Planeten -- diagnostisch, "
        "keine Ping-Buttons.\n\n"

        "**`/tbreminder_planets_ping planet`**\n"
        "Ruft alle Besitzer der benötigten Einheiten eines Planeten zur "
        "Teilnahme auf, mit einem Ping-Button pro Einheit plus 'Alle pingen'.\n\n"

        "**`/tbreminder_units_check unit relic`**\n"
        "Zeigt alle Besitzer einer bestimmten Einheit ab einem Relic-Level -- "
        "diagnostisch, unabhängig von der Anforderungsliste. Relic-Angabe "
        "wird bei Schiffen ignoriert.\n\n"

        "**`/tbreminder_units_ping unit relic`**\n"
        "Wie `/tbreminder_units_check`, zusätzlich mit Ping-Button für die "
        "gefundenen Besitzer.\n\n"

        "### ℹ️ Sonstiges\n"
        "**`/tbreminder_help`**\n"
        "Zeigt diese Uebersicht.\n\n"

        "-# Alle Befehle erfordern Administrator-Rechte oder Officer-Status."
    )
    await interaction.response.send_message(help_text, ephemeral=True)


bot.run(TOKEN)