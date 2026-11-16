import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
import time
import traceback
from datetime import datetime

import config
import roster_read
import requirements
import platoon_assignments as platoons
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
TB_CHANNEL_ID   = config.TB_CHANNEL_ID
ORG_CHANNEL_ID  = config.ORG_CHANNEL_ID
MANAGER_IDS     = config.MANAGER_IDS
MEMBER_ROLE_ID  = config.MEMBER_ROLE_ID
HOURS           = config.HOURS
PICKER_TIMEOUT  = config.PICKER_TIMEOUT
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

# stats.json-Struktur und alle Buchhaltungsfunktionen: siehe stats.py.


# ── Permission helper ─────────────────────────────────────────────────────────

def is_authorized(interaction: discord.Interaction) -> bool:
    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.id in MANAGER_IDS
    )


def can_use_org_commands(interaction: discord.Interaction) -> bool:
    """
    Platoon-/Unit-Befehle (planets_check/-ping, units_check/-ping): offen für
    jeden, der sie in rote-organisation ausführt -- keine Rollen- oder
    Manager-Prüfung darüber hinaus, der Zugriff auf den Kanal ist die
    Hürde. Admins/Manager dürfen sie zusätzlich in jedem Kanal nutzen (z.B.
    zum Testen). Ohne die Kanalbindung könnte jeder mit Serverzugriff die
    Ping-Buttons aus beliebigen Kanälen auslösen.

    Die Ablaufsteuerung (start, timer, resume, cancel, status, results,
    requirements_upload) bleibt bei is_authorized().
    """
    return interaction.channel_id == ORG_CHANNEL_ID or is_authorized(interaction)


async def deny_org_only(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"Dieser Befehl ist in <#{ORG_CHANNEL_ID}> verfügbar.", ephemeral=True
    )


# ── Auswahl-UI (gemeinsame Basis) ─────────────────────────────────────────────

SELECT_MAX_OPTIONS = 25  # Discord-Hartlimit pro Dropdown
NO_MENTIONS = discord.AllowedMentions.none()


class MultiSelectView(discord.ui.View):
    """
    Gemeinsame Basis für alle Mehrfachauswahl-Dialoge (Erinnerungsauswahl,
    "Wer hat NICHT stationiert?", Besitzerauswahl bei /tbreminder_units_*).
    Ersetzt die zwei früheren, bis auf Labels identischen Kopien
    PlayerSelectView/FailedSetView.

    Was sich gegenüber den DM-Versionen ändert, weil die Dialoge jetzt in
    einem Kanal stehen und JEDER dort klicken darf:

    * Der Auswahlzustand ist EIN gemeinsamer Satz (selected_ids) und wird
      nach jeder Änderung als Nachrichtentext (Namen) UND als
      SelectOption.default in die Dropdowns zurückgeschrieben. Alle
      Betrachter sehen dadurch denselben Stand, und ein Klient sendet beim
      nächsten Klick "gemeinsamer Stand +/- eigene Änderung" statt eines
      veralteten Eigenstands. Vorher gab es nur eine ephemere "N
      ausgewählt"-Antwort -- bei mehreren Bedienern unsichtbar überschreibbar.
      Restrisiko: zwei Klicks im selben Sekundenbruchteil, Last-write-wins.
    * timeout=None + harte Frist über wait_until(). discord.py misst
      View.timeout ab der LETZTEN Interaktion (View._refresh_timeout); in
      einem Kanal, in dem mehrere Leute klicken, würde ein Klick kurz vor
      Fristende die Frist verlängern und die generische Erinnerung nach
      hinten schieben.
    """

    def __init__(
        self,
        items: list[tuple[str, str, str | None]],
        *,
        header: str,
        noun: str = "Spieler",
        show_names: bool = True,
        preselected: set[str] | None = None,
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        self.header = header
        self.show_names = show_names
        self.selected_ids: set[str] = set(preselected or ())
        self.status_lines: list[str] = []
        self.confirmed = False
        self.skipped = False
        self.timed_out = False
        self.closed = False
        self.actor_name: str | None = None
        self.message: discord.Message | None = None
        self._labels = {value: label for value, label, _ in items}
        self._selects: list[discord.ui.Select] = []

        # Discord: max. 5 Zeilen pro Nachricht, ein Dropdown belegt eine
        # Zeile, die Buttons der Unterklassen brauchen eine -> höchstens 4
        # Dropdowns = 100 Einträge (SWGOH-Gilden haben max. 50 Mitglieder).
        items = items[: 4 * SELECT_MAX_OPTIONS]
        for start in range(0, len(items), SELECT_MAX_OPTIONS):
            chunk = items[start : start + SELECT_MAX_OPTIONS]
            options = [
                discord.SelectOption(
                    label=label[:100],
                    value=value,
                    description=(description[:100] if description else None),
                    default=value in self.selected_ids,
                )
                for value, label, description in chunk
            ]
            select = discord.ui.Select(
                placeholder=f"{noun} {start + 1}-{start + len(chunk)} auswählen...",
                min_values=0,
                max_values=len(options),
                options=options,
            )
            select.callback = self._make_select_callback(select)
            self._selects.append(select)
            self.add_item(select)

    # ── Darstellung ───────────────────────────────────────────────────────

    def _selection_line(self) -> str:
        if not self.selected_ids:
            return "*Noch niemand ausgewählt.*"
        if self.show_names:
            names = ", ".join(
                sorted((self._labels[v] for v in self.selected_ids), key=str.lower)
            )
            return f"**Ausgewählt ({len(self.selected_ids)}):** {names}"
        return f"**Ausgewählt:** {len(self.selected_ids)}"

    def _content(self) -> str:
        parts = [self.header]
        if self.status_lines:
            parts.append("\n".join(self.status_lines))
        parts.append(self._selection_line())
        text = "\n\n".join(parts)
        if len(text) > 1990 and self.show_names:
            # Notbremse gegen Discords 2000-Zeichen-Limit: nur Anzahl statt Namen
            parts[-1] = f"**Ausgewählt:** {len(self.selected_ids)}"
            text = "\n\n".join(parts)
        return text[:1990]

    def _sync_defaults(self) -> None:
        """Gemeinsamen Stand in die Dropdowns zurückschreiben, siehe Docstring."""
        for select in self._selects:
            for option in select.options:
                option.default = option.value in self.selected_ids

    # ── Callbacks ─────────────────────────────────────────────────────────

    async def _reject_if_closed(self, interaction: discord.Interaction) -> bool:
        if self.closed:
            await interaction.response.send_message(
                "Diese Auswahl ist bereits abgeschlossen.", ephemeral=True
            )
            return True
        return False

    def _make_select_callback(self, select: discord.ui.Select):
        async def _callback(interaction: discord.Interaction):
            if await self._reject_if_closed(interaction):
                return
            pool = {option.value for option in select.options}
            self.selected_ids -= pool
            self.selected_ids |= set(select.values)
            self._sync_defaults()
            await interaction.response.edit_message(content=self._content(), view=self)

        return _callback

    # ── Abschluss ─────────────────────────────────────────────────────────

    async def edit_message(self, **kwargs) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(**kwargs)
        except discord.HTTPException as e:
            print(f"Konnte Auswahl-Nachricht nicht bearbeiten: {e}")

    async def finalize(self, text: str) -> None:
        """Nachricht in ihr Endergebnis umschreiben (statt eine weitere zu
        posten -- weniger Rauschen im Kanal)."""
        await self.edit_message(content=text[:1990], view=None)

    async def wait_until(self, seconds: float, timeout_text: str) -> None:
        """Wartet bis Bestätigen/Überspringen ODER harte Frist. Bei
        Fristablauf: Dialog schließen (danach lehnen alle Callbacks ab) und
        die Nachricht auf timeout_text umschreiben. shield(), damit der
        wait_for-Abbruch nicht das interne Stopped-Future der View cancelt."""
        try:
            await asyncio.wait_for(asyncio.shield(self.wait()), timeout=seconds)
        except asyncio.TimeoutError:
            if self.closed:  # in genau diesem Moment bestätigt/übersprungen
                return
            self.closed = True
            self.timed_out = True
            self.stop()
            await self.edit_message(content=timeout_text, view=None)


class PhasePickerView(MultiSelectView):
    """Auswahl mit "Bestätigen" und "Überspringen" -- Basis für die beiden
    Phasenend-Dialoge. Unterklassen setzen nur Beschriftungen und Texte."""

    def __init__(
        self,
        members: list[discord.Member],
        *,
        header: str,
        confirm_label: str,
        confirm_style: discord.ButtonStyle,
        skip_label: str,
        confirm_text: str,
        skip_text: str,
    ):
        items = [(str(m.id), m.display_name, None) for m in members]
        super().__init__(items, header=header)
        self._confirm_text = confirm_text
        self._skip_text = skip_text

        confirm_btn = discord.ui.Button(label=confirm_label, style=confirm_style)
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

        skip_btn = discord.ui.Button(label=skip_label, style=discord.ButtonStyle.grey)
        skip_btn.callback = self._on_skip
        self.add_item(skip_btn)

    async def _on_confirm(self, interaction: discord.Interaction):
        if await self._reject_if_closed(interaction):
            return
        if not self.selected_ids:
            await interaction.response.send_message(
                "Es ist noch niemand ausgewählt!", ephemeral=True
            )
            return
        self.closed = True
        self.confirmed = True
        self.actor_name = interaction.user.display_name
        await interaction.response.edit_message(
            content=self._confirm_text.format(
                n=len(self.selected_ids), user=self.actor_name
            ),
            view=None,
        )
        self.stop()

    async def _on_skip(self, interaction: discord.Interaction):
        if await self._reject_if_closed(interaction):
            return
        self.closed = True
        self.skipped = True
        self.actor_name = interaction.user.display_name
        await interaction.response.edit_message(
            content=self._skip_text.format(user=self.actor_name), view=None
        )
        self.stop()


class PlayerSelectView(PhasePickerView):
    """Erinnerungsauswahl: wer bekommt eine persönliche DM."""

    def __init__(self, members: list[discord.Member], header: str):
        super().__init__(
            members,
            header=header,
            confirm_label="Bestätigen & senden",
            confirm_style=discord.ButtonStyle.green,
            skip_label="Überspringen (generische Nachricht)",
            confirm_text="✅ Bestätigt von {user}: {n} Spieler erhalten eine persönliche DM...",
            skip_text="⏭️ Übersprungen von {user}. Die generische Nachricht wird gesendet.",
        )


class FailedSetView(PhasePickerView):
    """Wer hat in dieser Phase NICHT stationiert."""

    def __init__(self, members: list[discord.Member], header: str):
        super().__init__(
            members,
            header=header,
            confirm_label="Bestätigen (nicht stationiert)",
            confirm_style=discord.ButtonStyle.red,
            skip_label="Alle haben stationiert",
            confirm_text="✅ {user}: {n} Spieler als nicht stationiert markiert.",
            skip_text="✅ {user}: Alle haben stationiert - keine Einträge.",
        )


# ── DM-Versand (gemeinsam für Phasenend-Erinnerung und Unit-DMs) ──────────────

# (key, anzeigename, ziel) -- key ist beliebig (Discord-ID, Ally-Code) und
# kommt unverändert im Ergebnis zurück; ziel=None heißt "nicht auffindbar".
DmRecipient = tuple[str, str, "discord.abc.Messageable | None"]


async def send_dms(
    recipients: list[DmRecipient], text: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """
    Sendet text an jeden Empfänger. Rückgabe: (sent, failed) mit
    sent = [(key, name)], failed = [(key, name, grund)].

    Der Grund unterscheidet jetzt, was früher alles unter "DMs deaktiviert"
    lief: Forbidden (wirklich deaktiviert), 503 nach 3 Versuchen,
    unerwarteter Fehler, Empfänger nicht auffindbar.
    """
    sent: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []

    for key, name, target in recipients:
        if target is None:
            failed.append((key, name, "nicht auf dem Server gefunden"))
            continue

        reason: str | None = None
        for attempt in range(3):
            try:
                await target.send(text)
                reason = None
                break
            except discord.Forbidden:
                print(f"Konnte {name} keine DM senden (DMs deaktiviert)")
                reason = "DMs deaktiviert"
                break
            except discord.DiscordServerError:
                reason = "Discord-Serverfehler (3 Versuche)"
                if attempt < 2:
                    print(f"Discord 503 beim Senden an {name}, Versuch {attempt + 1}/3 - warte 5s...")
                    await asyncio.sleep(5)
                else:
                    print(f"Discord 503 beim Senden an {name} nach 3 Versuchen - uebersprungen.")
            except Exception as e:
                print(f"Unerwarteter Fehler beim Senden an {name}: {e} - uebersprungen.")
                reason = f"Fehler ({type(e).__name__})"
                break

        if reason is None:
            sent.append((key, name))
        else:
            failed.append((key, name, reason))

    return sent, failed


def _format_failed(failed: list[tuple[str, str, str]]) -> str:
    return ", ".join(f"{name} ({reason})" for _, name, reason in sorted(failed, key=lambda f: f[1].lower()))


# ── Core logic ────────────────────────────────────────────────────────────────

async def handle_phase_end(
    phase_index: int,
    tb_channel: discord.TextChannel,
    stats: dict,
    tb_index: int,
    next_phase_wait: float = 0,
    is_last_phase: bool = False,
):
    """
    Läuft PLAYER_REMINDER_OFFSET vor Phasenende (siehe config.py):
      1. Auswahl der zu erinnernden Spieler in rote-organisation posten;
         bis GENERIC_REMINDER_OFFSET vor Phasenende kann dort jeder
         auswählen und bestätigen.
      2. Bestätigt  -> persönliche DMs, KEINE generische Nachricht.
         Übersprungen oder Frist abgelaufen -> generische Nachricht im
         TB-Kanal.
      3. "Wer hat NICHT stationiert?"-Auswahl (Hintergrundtask).
    """
    phase_num = phase_index + 1
    started = time.monotonic()
    print(f"Phase {phase_num} endet in {config.PLAYER_REMINDER_OFFSET / HOURS:.0f}h - Auswahl wird in rote-organisation gepostet...")

    async def send_generic():
        await tb_channel.send(f"@everyone {GENERIC_REMINDER}")

    org_channel = bot.get_channel(ORG_CHANNEL_ID)
    if org_channel is None:
        print(f"Kanal rote-organisation (ID {ORG_CHANNEL_ID}) nicht gefunden - generische Nachricht wird gesendet")
        await send_generic()
        return

    guild  = tb_channel.guild
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
    picker_deadline = int(time.time()) + int(PICKER_TIMEOUT)
    reminder_view = PlayerSelectView(
        members,
        header=(
            f"**Phase {phase_num} endet in ca. {config.PLAYER_REMINDER_OFFSET // HOURS} Stunden! (Territory Battle)**\n"
            f"Wählt die Spieler aus, die eine persönliche DM-Erinnerung erhalten sollen, "
            f"und klickt dann auf *Bestätigen & senden*. Jeder darf auswählen.\n"
            f"Frist: <t:{picker_deadline}:t> (<t:{picker_deadline}:R>). Ohne Bestätigung bis dahin "
            f"geht die generische Erinnerung an alle in {tb_channel.mention} raus."
        ),
    )

    try:
        msg = await org_channel.send(
            reminder_view._content(), view=reminder_view, allowed_mentions=NO_MENTIONS
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Konnte Auswahl nicht in rote-organisation posten ({e}) - generische Nachricht wird gesendet")
        await send_generic()
        return
    reminder_view.message = msg

    await reminder_view.wait_until(
        PICKER_TIMEOUT,
        "⏰ Zeit abgelaufen - keine Auswahl getroffen. Die generische Nachricht wurde gesendet.",
    )

    phase_msg = PHASE_END_MESSAGES[phase_index]

    if reminder_view.confirmed and reminder_view.selected_ids:
        member_map = {str(m.id): m for m in members}
        recipients = [
            (uid, member_map[uid].display_name, member_map[uid])
            for uid in reminder_view.selected_ids
            if uid in member_map
        ]
        sent, failed = await send_dms(recipients, phase_msg)
        record_reminders(stats, sent, tb_index)

        print(f"Phase {phase_num}: {len(sent)} DMs gesendet, {len(failed)} fehlgeschlagen.")
        lines = [
            f"✅ **Phase {phase_num}**: {len(sent)} Spieler per DM erinnert "
            f"(bestätigt von {reminder_view.actor_name})."
        ]
        if sent:
            lines.append("Erinnert: " + ", ".join(sorted((n for _, n in sent), key=str.lower)))
        if failed:
            lines.append(f"⚠️ Nicht erreicht ({len(failed)}): {_format_failed(failed)}")
        await reminder_view.finalize("\n".join(lines))
    else:
        reason = "Uebersprungen" if reminder_view.skipped else "Timeout"
        print(f"Phase {phase_num}: {reason} - generische Nachricht wird gesendet.")
        await send_generic()

    # ── Step 2: Failed-to-set picker (fire and forget) ────────────────────────
    # Kandidaten: nur die in Step 1 ausgewählten Spieler (unabhängig davon, ob
    # die DM zugestellt wurde -- ausgewählt heißt "stand auf der Liste").
    # Ohne Bestätigung (Skip/Timeout) gibt es keine Auswahl, die generische
    # Nachricht ging an alle -> dann bleibt die volle Mitgliederliste.
    if reminder_view.confirmed and reminder_view.selected_ids:
        failed_candidates = [m for m in members if str(m.id) in reminder_view.selected_ids]
        candidates_note = "Zur Auswahl stehen nur die in der Erinnerungsauswahl gewählten Spieler.\n"
    else:
        failed_candidates = members
        candidates_note = ""

    async def send_failed_picker():
        if is_last_phase:
            failed_timeout = config.FINAL_FAILED_TIMEOUT
        else:
            # Bis zum nächsten Trigger (dann erscheint die nächste
            # Erinnerungsauswahl), mindestens 1 h. next_phase_wait ist die
            # Wartezeit ab Trigger dieser Phase, abzüglich der seitdem
            # vergangenen Zeit.
            failed_timeout = max(HOURS, next_phase_wait - (time.monotonic() - started))
        deadline_ts = int(time.time()) + int(failed_timeout)
        failed_view = FailedSetView(
            failed_candidates,
            header=(
                f"**Phase {phase_num} - Wer hat NICHT stationiert?**\n"
                f"{candidates_note}"
                f"Wählt die Spieler aus, die diese Phase nicht stationiert haben, und klickt auf "
                f"*Bestätigen*. Falls alle stationiert haben: *Alle haben stationiert*.\n"
                f"Frist: <t:{deadline_ts}:F> (<t:{deadline_ts}:R>)."
            ),
        )
        try:
            fmsg = await org_channel.send(
                failed_view._content(), view=failed_view, allowed_mentions=NO_MENTIONS
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"Konnte Failed-to-set-Auswahl nicht posten ({e}) - wird nicht erfasst")
            if is_last_phase:
                await send_stats_summary(org_channel, stats, tb_index)
            return
        failed_view.message = fmsg

        await failed_view.wait_until(
            failed_timeout, "⏰ Zeit abgelaufen - keine Fehlenden eingetragen."
        )

        if failed_view.confirmed and failed_view.selected_ids:
            member_map = {str(m.id): m for m in failed_candidates}
            failed_list = [
                (uid, member_map[uid].display_name)
                for uid in failed_view.selected_ids
                if uid in member_map
            ]
            record_failed(stats, failed_list, tb_index)
            print(f"Phase {phase_num}: {len(failed_list)} Spieler als nicht stationiert markiert.")
            names = ", ".join(sorted((n for _, n in failed_list), key=str.lower))
            await failed_view.finalize(
                f"✅ **Phase {phase_num}**: {len(failed_list)} Spieler als nicht stationiert "
                f"eingetragen (von {failed_view.actor_name}): {names}"
            )
        else:
            reason = "Alle haben stationiert" if failed_view.skipped else "Timeout"
            print(f"Phase {phase_num}: {reason} - keine Fehlenden eingetragen.")

        if is_last_phase:
            await send_stats_summary(org_channel, stats, tb_index)

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


async def send_stats_summary(destination: discord.abc.Messageable, stats: dict, tb_index: int):
    """Post the stats summary (rote-organisation). Called automatically at end of TB."""
    messages = await build_stats_messages(stats, tb_index)
    for msg in messages:
        for chunk in split_message(msg):
            await destination.send(chunk)


async def run_sequence(tb_channel: discord.TextChannel, start_phase: int = 0, phase_elapsed: float = 0.0):
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
        guild = tb_channel.guild
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

            await tb_channel.send("@everyone Ein neues Territory Battle hat gestartet!")
            print(f"TB-Startnachricht gesendet. (TB #{tb_index + 1} in den Stats)")
        else:
            print(f"TB-Sequenz wird ab Phase {start_phase + 1} fortgesetzt. (TB #{tb_index + 1})")

        # Persist run state to disk
        # On resume (start_phase > 0), preserve existing phase_started_at
        set_current_run(stats, tb_index, start_phase, tb_channel.id, update_timestamp=(start_phase == 0))
        # Phase 1 öffnet mit dem TB-Start: Platoon-DMs nicht erst beim
        # nächsten Schleifendurchlauf (bis 60 s) verschicken.
        asyncio.create_task(dispatch_platoon_dms())

        carry_over = 0.0
        for i in range(start_phase, 6):
            if i == start_phase and phase_elapsed > 0:
                # Resume: subtract already-elapsed time from this phase's wait
                base_wait = config.trigger_wait(i)
                wait_seconds = max(0, base_wait - phase_elapsed)
                print(f"Phase {i + 1}: noch {wait_seconds / HOURS:.2f}h bis zur Erinnerungsauswahl (Resumption).")
            else:
                wait_seconds = config.trigger_wait(i) - carry_over

            next_phase_wait = config.TRIGGER_INTERVAL if i < 5 else 0
            last_phase = (i == 5)

            print(f"Warte {wait_seconds / HOURS:.2f}h bis zur Erinnerungsauswahl fuer Phase {i + 1}...")
            await asyncio.sleep(max(0, wait_seconds))

            if not is_running:
                print(f"TB-Sequenz wurde abgebrochen (nach Sleep Phase {i + 1}).")
                return

            # Update persisted phase state before handling
            set_current_run(stats, tb_index, i + 1, tb_channel.id)

            t0 = time.monotonic()
            await handle_phase_end(i, tb_channel, stats, tb_index, next_phase_wait, last_phase)
            carry_over = time.monotonic() - t0
            print(f"Phase {i + 1} Interaktion dauerte {carry_over / 60:.1f} min - wird von naechster Phase abgezogen.")

        print("Alle Phasen abgeschlossen. Territory Battle Sequenz beendet.")
        removed = platoons.clear_tb(tb_index)
        if removed:
            print(f"{removed} Platoon-Zuweisungen von TB #{tb_index + 1} entfernt.")
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
    Garbage-Collect), und in Discord passiert schlicht nichts mehr -- in
    rote-organisation merkt das erst jemand, wenn die erwartete
    Auswahl ausbleibt.

    Best effort: schlagen beide Kanal-Posts (TB-Kanal und
    rote-organisation) fehl (z.B. weil der Discord-Gateway selbst die
    Ursache der Exception war), wird das geloggt, aber nichts weiter
    unternommen -- es gibt keinen zuverlässigeren Kanal mehr, über den der
    Bot sich melden könnte.
    """
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"TB-Sequenz abgestürzt:\n{tb_text}")

    message = (
        f"❌ Die TB-Sequenz ist mit einem Fehler abgebrochen: `{exc}`\n"
        f"Der Bot läuft weiter, die Sequenz nicht mehr. Bitte `/tbreminder_status` "
        f"prüfen und ggf. `/tbreminder_resume` nutzen."
    )
    try:
        channel = bot.get_channel(TB_CHANNEL_ID)
        if channel:
            await channel.send(message)
    except Exception as notify_exc:
        print(f"Konnte Absturz nicht im Kanal melden: {notify_exc}")

    try:
        org_channel = bot.get_channel(ORG_CHANNEL_ID)
        if org_channel:
            await org_channel.send(message)
    except Exception as notify_exc:
        print(f"Konnte Absturz nicht in rote-organisation melden: {notify_exc}")


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


# ── Platoon-Zuweisungen: DM zu Phasenbeginn ───────────────────────────────────
# Unabhängig von run_sequence: eine Schleife prüft jede Minute anhand von
# stats["current_run"], welche Phase gerade offen ist, und verschickt
# fällige DMs. Damit funktioniert der Versand auch nach Neustart,
# Auto-Recovery, /tbreminder_resume und für Einträge, die erst während der
# laufenden Phase registriert werden -- ohne eigene Timer, die verloren
# gehen können. Versendet-Status steht in platoon_assignments.json, also
# keine Doppel-DMs nach einem Neustart.

PLATOON_DISPATCH_INTERVAL = 60  # Sekunden

# Schützt platoon_assignments.json gegen gleichzeitiges Lesen-Ändern-
# Schreiben von Dispatcher und register/remove.
_platoon_lock = asyncio.Lock()


def _platoon_dm_text(phase: int, entries: list[dict]) -> str:
    lines = [
        f"**Territory Battle - Phase {phase} hat begonnen**",
        "Bitte stationiere in dieser Phase folgende Platoons:",
    ]
    for a in sorted(entries, key=lambda a: (a["planet"].lower(), a["unit_name"].lower())):
        lines.append(f"• **{a['unit_name']}** auf **{a['planet']}**")
    return "\n".join(lines)


async def _resolve_dm_target(guild: discord.Guild | None, discord_id: str | None):
    if not discord_id or guild is None:
        return None
    try:
        uid = int(discord_id)
    except ValueError:
        return None
    member = guild.get_member(uid)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(uid)
    except (discord.NotFound, discord.HTTPException):
        return None


async def dispatch_platoon_dms() -> None:
    async with _platoon_lock:
        run = load_stats().get("current_run", {})
        phase = platoons.current_open_phase(run)
        if phase is None:
            return
        tb_index = run.get("tb_index")
        if tb_index is None:
            return

        assignments = platoons.load()
        count_before = len(assignments)
        assignments = platoons.prune_stale(assignments, tb_index)
        bound = platoons.bind_unbound(assignments, tb_index)
        if bound or len(assignments) != count_before:
            platoons.save(assignments)

        due = [
            a for a in assignments
            if a["tb_index"] == tb_index and a["phase"] == phase and not a["sent"]
        ]
        if not due:
            return

        by_player: dict[str, list[dict]] = {}
        for a in due:
            by_player.setdefault(a["ally_code"], []).append(a)

        guild = bot.get_guild(GUILD_ID)
        text_ok: list[str] = []
        text_failed: list[str] = []

        for ally_code, entries in by_player.items():
            name = entries[0]["player_name"]
            discord_id = entries[0].get("discord_id")
            # Aktuelle Verknüpfung aus dem Roster bevorzugen (falls neu
            # verknüpft), gespeicherte ID aus der Registrierung als Fallback.
            try:
                row = roster_read.get_player(ally_code)
                if row is not None:
                    name = row["player_name"] or name
                    if row["discord_id"]:
                        discord_id = row["discord_id"]
            except roster_read.RosterUnavailableError as e:
                print(f"Platoon-DM: Roster nicht erreichbar ({e}) - nutze gespeicherte Discord-ID.")

            if not discord_id:
                result = "keine Discord-Verknüpfung"
            else:
                target = await _resolve_dm_target(guild, discord_id)
                _, failed = await send_dms([(ally_code, name, target)], _platoon_dm_text(phase, entries))
                result = failed[0][2] if failed else "ok"

            for a in entries:
                a["sent"] = True
                a["result"] = result
            # Nach jedem Spieler speichern: ein Absturz mitten im Versand
            # führt höchstens für den gerade bearbeiteten Spieler zu einer
            # doppelten DM, nicht für alle bereits angeschriebenen.
            platoons.save(assignments)

            units = ", ".join(f"{a['unit_name']} ({a['planet']})" for a in entries)
            if result == "ok":
                text_ok.append(f"{name}: {units}")
            else:
                text_failed.append(f"{name} ({result}): {units}")

        print(f"Phase {phase}: Platoon-DMs an {len(text_ok)} Spieler, {len(text_failed)} nicht erreicht.")

        org_channel = bot.get_channel(ORG_CHANNEL_ID)
        if org_channel is None:
            return
        lines = [f"📨 **Phase {phase} - Platoon-DMs** an {len(text_ok)} Spieler verschickt."]
        lines += [f"• {t}" for t in sorted(text_ok, key=str.lower)]
        if text_failed:
            lines.append(f"⚠️ **Nicht erreicht ({len(text_failed)}) - bitte manuell informieren:**")
            lines += [f"• {t}" for t in sorted(text_failed, key=str.lower)]
        try:
            for chunk in split_message("\n".join(lines)):
                await org_channel.send(chunk, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            print(f"Konnte Platoon-Zusammenfassung nicht posten: {e}")


@tasks.loop(seconds=PLATOON_DISPATCH_INTERVAL)
async def platoon_dispatch_loop():
    # Ausnahmen hier abfangen: eine unbehandelte Exception beendet
    # tasks.loop dauerhaft.
    try:
        await dispatch_platoon_dms()
    except Exception:
        print(f"Platoon-Dispatcher fehlgeschlagen:\n{traceback.format_exc()}")


@platoon_dispatch_loop.before_loop
async def _platoon_loop_wait_ready():
    await bot.wait_until_ready()


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
    print(f"   tb_channel  : {TB_CHANNEL_ID}")
    print(f"   org_channel : {ORG_CHANNEL_ID}")
    print(f"   manager_ids : {MANAGER_IDS or '(keine)'}")
    print(f"   rolle       : {MEMBER_ROLE_ID}")

    # on_ready kann bei Gateway-Reconnects mehrfach feuern -- die Recovery
    # darf nur beim allerersten Verbindungsaufbau laufen, sonst würde ein
    # normaler Reconnect während einer laufenden Sequenz fälschlich einen
    # zweiten run_sequence-Task neben dem bereits laufenden starten.
    if not platoon_dispatch_loop.is_running():
        platoon_dispatch_loop.start()

    if _startup_recovery_done:
        return
    _startup_recovery_done = True

    stats = load_stats()
    run = stats.get("current_run", {})
    if not run.get("active"):
        return

    phase = run.get("phase", 0)
    started_at = run.get("phase_started_at")
    channel_id = run.get("channel_id", TB_CHANNEL_ID)

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
            org_channel = bot.get_channel(ORG_CHANNEL_ID)
            if org_channel:
                await org_channel.send(
                    f"⚠️ TB-Reminder wurde neu gestartet und hat eine unterbrochene Sequenz "
                    f"gefunden (Phase {phase}), konnte den TB-Kanal (`{channel_id}`) aber "
                    f"nicht finden. Bitte `/tbreminder_resume` manuell ausführen."
                )
        except Exception as notify_exc:
            print(f"Konnte rote-organisation nicht benachrichtigen: {notify_exc}")
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
        org_channel = bot.get_channel(ORG_CHANNEL_ID)
        if org_channel:
            await org_channel.send(resume_message)
    except Exception as notify_exc:
        print(f"Konnte rote-organisation nicht benachrichtigen: {notify_exc}")

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

    channel = bot.get_channel(TB_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"Kanal mit ID `{TB_CHANNEL_ID}` nicht gefunden. Bitte `.env` pruefen.",
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
            "Es laeuft bereits ein Timer! Nutze `/tbreminder_cancel` um ihn abzubrechen.",
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

    channel = bot.get_channel(TB_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"Kanal mit ID `{TB_CHANNEL_ID}` nicht gefunden. Bitte `.env` pruefen.",
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
    hours_elapsed="Stunden seit dem letzten Trigger bzw. TB-Start (optional, wird aus gespeichertem Status berechnet)"
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

    channel = bot.get_channel(TB_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"Kanal mit ID `{TB_CHANNEL_ID}` nicht gefunden.",
            ephemeral=True,
        )
        return

    phase_index = phase - 1
    elapsed_seconds = hours_elapsed * HOURS
    base_wait = config.trigger_wait(phase_index)
    remaining = max(0, base_wait - elapsed_seconds)

    await interaction.response.send_message(
        f"TB-Sequenz wird ab Phase {phase} fortgesetzt.\n"
        f"Verbleibende Wartezeit bis zur Erinnerungsauswahl: **{remaining / HOURS:.1f}h**\n"
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
            if tb_index is not None:
                platoons.reset_tb(tb_index)
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




@tree.command(name="tbreminder_status", description="Zeigt den aktuellen TB-Status: Phasenbeginn, Erinnerungsauswahl, Phasenende")
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

    # current_run["phase"] ist der Index (0-5) der Phase, deren Trigger als
    # nächstes feuert; phase_started_at ist der Zeitpunkt des vorherigen
    # Triggers bzw. des TB-Starts (siehe run_sequence / set_current_run).
    if phase >= config.PHASE_COUNT:
        await interaction.response.send_message(
            f"**TB #{tb_index + 1}** - alle Phasen sind durch, die letzte Fehlend-Auswahl "
            f"bzw. der Abschlussbericht laeuft noch.",
            ephemeral=True,
        )
        return

    trigger_ts   = started_at + config.trigger_wait(phase)
    phase_end_ts = trigger_ts + config.PLAYER_REMINDER_OFFSET
    generic_ts   = phase_end_ts - config.GENERIC_REMINDER_OFFSET
    # Phase 1 beginnt mit dem TB-Start; jede spätere Phase beginnt zum Ende
    # der vorherigen = vorheriger Trigger + PLAYER_REMINDER_OFFSET.
    phase_start_ts = started_at if phase == 0 else started_at + config.PLAYER_REMINDER_OFFSET

    platoon_line = ""
    try:
        pending = [
            a for a in platoons.load()
            if a["tb_index"] in (tb_index, None) and not a["sent"]
        ]
        open_phase = platoons.current_open_phase(run) or 0
        pending = [a for a in pending if a["phase"] >= open_phase]
        upcoming = sorted({a["phase"] for a in pending})
        if upcoming:
            next_ts = platoons.phase_open_ts(run, upcoming[0])
            when = f" (<t:{next_ts}:R>)" if next_ts else ""
            platoon_line = (
                f"Platoon-DMs offen:          {len(pending)} Eintraege, "
                f"naechste zu Beginn von Phase {upcoming[0]}{when}\n"
            )
    except Exception as e:
        print(f"Platoon-Status nicht lesbar: {e}")

    await interaction.response.send_message(
        f"**TB #{tb_index + 1} - Phase {phase + 1} laeuft**\n\n"
        f"Phase gestartet:            <t:{phase_start_ts}:F>\n"
        f"Spielerauswahl (rote-org.): <t:{trigger_ts}:F> (<t:{trigger_ts}:R>)\n"
        f"Generische Erinnerung*:     <t:{generic_ts}:F>\n"
        f"Phase endet:                <t:{phase_end_ts}:F> (<t:{phase_end_ts}:R>)\n"
        f"{platoon_line}\n"
        f"-# * nur, wenn bis dahin niemand die Auswahl bestaetigt hat",
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


def _build_ping_content(context_line: str, owners: list) -> str | None:
    """
    Gemeinsamer Ping-Text-Baustein für alle drei Ping-Views. Registrierte
    Besitzer (discord_id vorhanden, via /tw_register) werden als
    @mentions gepingt; nicht-registrierte werden als Klartext-Namen
    aufgeführt, statt stillschweigend aus der Ping-Nachricht zu
    verschwinden -- vorher wurden sie komplett ausgeblendet, wenn
    mindestens ein anderer Besitzer registriert war (live aufgefallen,
    siehe Chat-Verlauf: nicht jedes Gildenmitglied hat /tw_register schon
    ausgeführt).

    Gibt None zurück, wenn owners komplett leer ist -- der Aufrufer zeigt
    dann seine eigene "niemand besitzt"-Meldung statt eines leeren Pings.
    Ein Ergebnis mit nur nicht-registrierten Namen (keine @mentions) ist
    dagegen ein gültiger, nicht-leerer Ping-Text.
    """
    if not owners:
        return None

    mentions = [f"<@{o['discord_id']}>" for o in owners if o["discord_id"]]
    # Namensbasierte Deduplizierung (roster_read liefert inzwischen auch
    # ally_code, aber die Anzeige hier ist ohnehin der Name). Restrisiko:
    # zwei Gildenmitglieder mit identischem Ingame-Namen würden hier
    # fälschlich zusammengelegt -- in der Praxis selten genug, um für
    # diesen Zweck (wer muss manuell informiert werden) hinnehmbar zu sein.
    unregistered = sorted({o["player_name"] for o in owners if not o["discord_id"]})

    parts = []
    if mentions:
        parts.append(" ".join(mentions))
    if unregistered:
        parts.append(f"Nicht registriert, bitte manuell informieren: {', '.join(unregistered)}")
    parts.append(context_line)
    return "\n".join(parts)


# ── Ping-Ziel: TB-Kanal ────────────────────────────────────────────────────
# Auswahl und Bedienung passieren in rote-organisation, der eigentliche Ping
# landet aber im TB-Kanal (TB_CHANNEL_ID), wo die Spieler ihn sehen.

async def post_ping(interaction: discord.Interaction, content: str) -> bool:
    """Postet content in den TB-Kanal und quittiert dem Klickenden ephemer.
    True bei Erfolg, sonst False (Fehlermeldung ist dann schon gesendet)."""
    channel = bot.get_channel(TB_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"TB-Kanal (`{TB_CHANNEL_ID}`) nicht gefunden.", ephemeral=True
        )
        return False
    try:
        await channel.send(content)
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"Ping konnte nicht gesendet werden: {e}", ephemeral=True
        )
        return False
    await interaction.response.send_message(
        f"📣 Gepingt in {channel.mention}.", ephemeral=True
    )
    return True


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

    Der Ping selbst geht in den TB-Kanal (siehe post_ping()), der Klickende
    bekommt nur eine ephemere Bestätigung.
    """

    def __init__(self, planet: str, rows: list[dict]):
        super().__init__(timeout=None)
        self.planet = planet

        for row in rows:
            btn = discord.ui.Button(
                label=f"{row['unit_name']} pingen",
                style=discord.ButtonStyle.primary,
            )
            btn.callback = self._make_unit_callback(row, btn)
            self.add_item(btn)

    def _make_unit_callback(self, row: dict, btn: discord.ui.Button):
        async def _callback(interaction: discord.Interaction):
            context_line = f"Bitte für **{self.planet}** bereithalten (**{row['unit_name']}**)."
            content = _build_ping_content(context_line, row["owners"])
            if content is None:
                await interaction.response.send_message(
                    f"Niemand besitzt {row['unit_name']}.", ephemeral=True
                )
                return
            if not await post_ping(interaction, content):
                return
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
    einfache Variante, siehe Chat-Verlauf). Ping geht in den TB-Kanal.
    """

    def __init__(self, planet: str, all_rows: list[dict]):
        super().__init__(timeout=None)
        self.planet = planet
        self.all_rows = all_rows

    @discord.ui.button(label="Alle pingen", style=discord.ButtonStyle.success)
    async def ping_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        deduped_owners = []
        for row in self.all_rows:
            for o in row["owners"]:
                if o["discord_id"]:
                    if o["discord_id"] not in seen_ids:
                        seen_ids.add(o["discord_id"])
                        deduped_owners.append(o)
                elif o["player_name"] not in seen_names:
                    seen_names.add(o["player_name"])
                    deduped_owners.append(o)

        context_line = f"Bitte für **{self.planet}** bereithalten."
        content = _build_ping_content(context_line, deduped_owners)
        if content is None:
            await interaction.response.send_message(
                "Niemand besitzt eine der gelisteten Einheiten.", ephemeral=True
            )
            return

        if not await post_ping(interaction, content):
            return
        button.disabled = True
        await interaction.message.edit(view=self)


UNIT_VIEW_TIMEOUT = 14 * 60  # ephemere Nachricht: Interaction-Token gilt 15 min


class UnitSelectView(MultiSelectView):
    """
    Besitzer-Auswahl für /tbreminder_units_check und /tbreminder_units_ping:
    aus den Besitzern einer Einheit gezielt die auswählen, die noch
    stationieren sollen (wer schon stationiert hat, sieht man nur im Spiel),
    und dann entweder im TB-Kanal pingen oder per DM anschreiben.

    Ephemer, nur für den Aufrufer -- deshalb kein Mehrbediener-Problem und
    kein Rauschen in rote-organisation. Option-Wert ist der Ally-Code, nicht
    die Discord-ID: auch nicht registrierte Besitzer (discord_id leer) sind
    auswählbar; _build_ping_content() nennt sie dann als Klartext-Name.

    DMs werden bewusst NICHT in stats.json gezählt -- die Erinnerungsquote
    dort setzt maximal 6 Erinnerungen pro TB voraus (build_stats_messages).

    Nach einem DM-Versand schrumpft die Auswahl auf die NICHT erreichten
    Spieler, damit "Im Kanal pingen" direkt der Fallback für genau diese ist.
    """

    def __init__(
        self,
        unit: str,
        relic: int,
        owners: list,
        is_ship: bool,
        lookup_text: str,
        *,
        preselect_all: bool,
    ):
        items = []
        for o in owners:
            label = o["player_name"] or o["ally_code"]
            if not is_ship:
                label = f"{label} (R{config.relic_tier_to_display(o['relic_tier'])})"
            description = None if o["discord_id"] else "nicht registriert (kein Discord-Link)"
            items.append((o["ally_code"], label, description))

        super().__init__(
            items,
            header=lookup_text + "\n\n*Besitzer auswählen, dann im TB-Kanal pingen oder per DM anschreiben.*",
            noun="Besitzer",
            show_names=False,
            preselected={o["ally_code"] for o in owners} if preselect_all else None,
            timeout=UNIT_VIEW_TIMEOUT,
        )
        self.unit = unit
        self.relic = relic
        self.is_ship = is_ship
        self.owners = {o["ally_code"]: o for o in owners}
        self.origin: discord.Interaction | None = None

        self.ping_btn = discord.ui.Button(label="Im Kanal pingen", style=discord.ButtonStyle.primary)
        self.ping_btn.callback = self._on_ping
        self.add_item(self.ping_btn)

        self.dm_btn = discord.ui.Button(label="Per DM senden", style=discord.ButtonStyle.secondary)
        self.dm_btn.callback = self._on_dm
        self.add_item(self.dm_btn)

    def _selected_owners(self) -> list:
        return [self.owners[k] for k in self.selected_ids if k in self.owners]

    @staticmethod
    def _name(owner) -> str:
        return owner["player_name"] or owner["ally_code"]

    def _dm_text(self) -> str:
        if self.is_ship or not self.relic:
            return f"Bitte stationiere **{self.unit}** im Territory Battle - wird noch gebraucht."
        return (
            f"Bitte stationiere **{self.unit}** (Relic {self.relic}+) im Territory Battle "
            f"- wird noch gebraucht."
        )

    async def _on_ping(self, interaction: discord.Interaction):
        if not self.selected_ids:
            await interaction.response.send_message(
                "Erst mindestens einen Besitzer auswählen.", ephemeral=True
            )
            return
        owners = self._selected_owners()
        content = _build_ping_content(f"Bitte für **{self.unit}** bereithalten.", owners)
        channel = bot.get_channel(TB_CHANNEL_ID)
        if content is None or channel is None:
            await interaction.response.send_message(
                f"TB-Kanal (`{TB_CHANNEL_ID}`) nicht gefunden.", ephemeral=True
            )
            return
        try:
            await channel.send(content)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Ping konnte nicht gesendet werden: {e}", ephemeral=True
            )
            return

        self.ping_btn.disabled = True
        names = ", ".join(sorted((self._name(o) for o in owners), key=str.lower))
        self.status_lines.append(f"📣 Gepingt in {channel.mention}: {names}")
        await interaction.response.edit_message(content=self._content(), view=self)

    async def _on_dm(self, interaction: discord.Interaction):
        if not self.selected_ids:
            await interaction.response.send_message(
                "Erst mindestens einen Besitzer auswählen.", ephemeral=True
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "DMs lassen sich nur vom Server aus versenden.", ephemeral=True
            )
            return

        await interaction.response.defer()

        recipients = []
        failed: list[tuple[str, str, str]] = []
        for o in self._selected_owners():
            if not o["discord_id"]:
                failed.append((o["ally_code"], self._name(o), "nicht registriert"))
                continue
            member = interaction.guild.get_member(int(o["discord_id"]))
            recipients.append((o["ally_code"], self._name(o), member))

        sent, send_failed = await send_dms(recipients, self._dm_text())
        failed += send_failed

        self.dm_btn.disabled = True
        if sent:
            self.status_lines.append(
                "✉️ DM gesendet an: " + ", ".join(sorted((n for _, n in sent), key=str.lower))
            )
        if failed:
            self.status_lines.append(
                f"⚠️ Nicht erreicht: {_format_failed(failed)} - Auswahl auf diese "
                f"reduziert, *Im Kanal pingen* erreicht sie im TB-Kanal."
            )
        self.selected_ids = {key for key, _, _ in failed}
        self._sync_defaults()
        await interaction.edit_original_response(content=self._content(), view=self)

    async def on_timeout(self):
        if self.origin is None:
            return
        try:
            await self.origin.edit_original_response(view=None)
        except Exception:
            pass


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
    if not can_use_org_commands(interaction):
        await deny_org_only(interaction)
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
    if not can_use_org_commands(interaction):
        await deny_org_only(interaction)
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


async def _run_unit_command(
    interaction: discord.Interaction, unit: str, relic: int, *, preselect_all: bool
):
    """Gemeinsamer Ablauf für /tbreminder_units_check und _ping: Besitzer
    nachschlagen und als ephemere Auswahl (UnitSelectView) anzeigen. Der
    Unterschied der beiden Befehle ist nur, ob zu Beginn niemand oder alle
    Besitzer vorausgewählt sind."""
    if not can_use_org_commands(interaction):
        await deny_org_only(interaction)
        return

    await interaction.response.defer(ephemeral=True)

    lines, owners, is_ship = await _lookup_unit(unit, relic)
    if owners is None:
        await interaction.followup.send(lines, ephemeral=True)  # lines is the error text here
        return
    if not owners:
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    view = UnitSelectView(
        unit, relic, owners, is_ship, "\n".join(lines), preselect_all=preselect_all
    )
    await interaction.followup.send(view._content(), view=view, ephemeral=True)
    view.origin = interaction


@tree.command(
    name="tbreminder_units_check",
    description="Besitzer einer Einheit ab einem Relic-Level nachschlagen, gezielt auswählen und pingen/anschreiben",
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
    await _run_unit_command(interaction, unit, relic, preselect_all=False)


@tree.command(
    name="tbreminder_units_ping",
    description="Wie units_check, aber alle Besitzer sind vorausgewählt (Auswahl lässt sich anpassen)",
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
    await _run_unit_command(interaction, unit, relic, preselect_all=True)


# ── Platoon-Zuweisungen: register / remove / list ─────────────────────────────

async def platoon_player_autocomplete(interaction: discord.Interaction, current: str):
    """Spieler aus der players-Tabelle. Ist die Einheit schon gewählt, nur
    deren Besitzer (nach Relic absteigend), sonst alle. Wert = Ally-Code."""
    unit = getattr(interaction.namespace, "unit", None)
    try:
        rows = None
        if unit:
            unit_id = roster_read.resolve_unit_id(unit)
            if unit_id:
                rows = roster_read.get_owners_of_unit_ignore_relic(unit_id)
                rows = sorted(rows, key=lambda r: (r["relic_tier"] or 0, r["gear_tier"] or 0), reverse=True)
        if rows is None:
            rows = roster_read.get_all_players()
    except roster_read.RosterUnavailableError as e:
        print(f"Spieler-Autocomplete fehlgeschlagen: {e}")
        return []

    choices = []
    for r in rows:
        name = r["player_name"] or r["ally_code"]
        if current.lower() not in name.lower():
            continue
        relic = config.relic_tier_to_display(r["relic_tier"]) if "relic_tier" in r.keys() else 0
        label = f"{name} (R{relic})" if relic else name
        choices.append(app_commands.Choice(name=label[:100], value=r["ally_code"]))
        if len(choices) == 25:
            break
    return choices


async def platoon_entry_autocomplete(interaction: discord.Interaction, current: str):
    try:
        assignments = platoons.load()
    except Exception as e:
        print(f"Platoon-Autocomplete fehlgeschlagen: {e}")
        return []
    assignments.sort(key=lambda a: (a["phase"], a["planet"].lower(), a["unit_name"].lower(), a["player_name"].lower()))
    return [
        app_commands.Choice(name=platoons.label(a)[:100], value=a["id"])
        for a in assignments
        if current.lower() in platoons.label(a).lower()
    ][:25]


async def _send_chunked_ephemeral(interaction: discord.Interaction, text: str) -> None:
    """Für bereits deferred Interactions: alle Teile als Followup."""
    for chunk in split_message(text):
        await interaction.followup.send(chunk, ephemeral=True)


@tree.command(
    name="tbreminder_platoon_register",
    description="Platoon zuweisen: Spieler bekommt zu Beginn der Phase eine DM",
)
@app_commands.describe(
    phase="Phase (1-6), in der der Platoon gesetzt werden soll",
    planet="Planet, auf dem der Platoon gesetzt werden soll",
    unit="Einheit (Charakter oder Schiff)",
    player="Spieler (nach Wahl der Einheit: nur deren Besitzer)",
)
@app_commands.autocomplete(
    planet=planet_autocomplete, unit=unit_autocomplete, player=platoon_player_autocomplete
)
async def platoon_register(
    interaction: discord.Interaction,
    phase: app_commands.Range[int, 1, 6],
    planet: str,
    unit: str,
    player: str,
):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoetigst Administrator-Rechte oder Manager-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    # Defer vor allem anderen: der Lock unten kann warten, solange der
    # Dispatcher gerade DMs verschickt (3-s-Antwortfrist von Discord).
    await interaction.response.defer(ephemeral=True)

    planet = planet.strip()
    if not planet:
        await interaction.followup.send("Planet darf nicht leer sein.", ephemeral=True)
        return

    try:
        unit_id = roster_read.resolve_unit_id(unit)
        row = roster_read.get_player(player) or roster_read.find_player_by_name(player)
        owners = roster_read.get_owners_of_unit_ignore_relic(unit_id) if unit_id else []
    except roster_read.RosterUnavailableError as e:
        await interaction.followup.send(f"Rosterdaten nicht erreichbar: {e}", ephemeral=True)
        return

    if unit_id is None:
        await interaction.followup.send(
            f"'{unit}' konnte nicht aufgeloest werden -- besitzt sie laut letztem "
            f"Roster-Refresh mindestens ein Gildenmitglied?",
            ephemeral=True,
        )
        return
    if row is None:
        await interaction.followup.send(
            f"Spieler '{player}' nicht gefunden. Bitte einen Vorschlag aus der Liste waehlen.",
            ephemeral=True,
        )
        return

    ally_code = row["ally_code"]
    player_name = row["player_name"] or ally_code
    warnings: list[str] = []

    if not any(o["ally_code"] == ally_code for o in owners):
        warnings.append(f"⚠️ Laut Roster besitzt {player_name} **{unit}** nicht.")
    if not row["discord_id"]:
        warnings.append(f"⚠️ {player_name} hat keine Discord-Verknuepfung -- die DM wird nicht zustellbar sein.")

    try:
        known_planets = requirements.get_planets()
    except Exception:
        known_planets = []
    match = next((p for p in known_planets if p.lower() == planet.lower()), None)
    if match:
        planet = match
    elif known_planets:
        warnings.append(f"⚠️ Planet '{planet}' steht nicht in der Anforderungsliste.")

    async with _platoon_lock:
        run = load_stats().get("current_run", {})
        open_phase = platoons.current_open_phase(run)
        tb_index = None
        if open_phase is not None:
            if phase < open_phase:
                await interaction.followup.send(
                    f"Phase {phase} ist im laufenden TB schon vorbei (aktuell Phase {open_phase}). "
                    f"Eintraege fuer den naechsten TB sind erst nach Ende dieses TBs moeglich.",
                    ephemeral=True,
                )
                return
            tb_index = run.get("tb_index")

        assignments = platoons.load()
        if platoons.find_duplicate(assignments, phase, planet, unit_id, ally_code):
            await interaction.followup.send(
                f"Diesen Eintrag gibt es schon: Phase {phase} · {planet} · {unit} · {player_name}.",
                ephemeral=True,
            )
            return

        entry = platoons.new_assignment(
            phase=phase, planet=planet, unit_id=unit_id, unit_name=unit,
            ally_code=ally_code, player_name=player_name, discord_id=row["discord_id"],
            created_by=interaction.user.display_name, tb_index=tb_index,
        )
        assignments.append(entry)
        platoons.save(assignments)

    if tb_index is None:
        when = f"DM zu Beginn von Phase {phase} des naechsten TBs."
    elif phase == open_phase:
        when = f"Phase {phase} laeuft bereits -- die DM geht jetzt raus."
    else:
        ts = platoons.phase_open_ts(run, phase)
        when = f"DM zu Beginn von Phase {phase}" + (f": <t:{ts}:F> (<t:{ts}:R>)." if ts else ".")

    msg = [f"✅ Eingetragen: **Phase {phase}** · {planet} · **{unit}** → {player_name}", when] + warnings
    await interaction.followup.send("\n".join(msg), ephemeral=True)
    print(f"Platoon-Zuweisung von {interaction.user.display_name}: {platoons.label(entry)}")

    if tb_index is not None and phase == open_phase:
        await dispatch_platoon_dms()


@tree.command(name="tbreminder_platoon_remove", description="Platoon-Zuweisung entfernen")
@app_commands.describe(entry="Zuweisung (Phase · Planet · Einheit · Spieler)")
@app_commands.autocomplete(entry=platoon_entry_autocomplete)
async def platoon_remove(interaction: discord.Interaction, entry: str):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "Du benoetigst Administrator-Rechte oder Manager-Status fuer diesen Befehl.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    async with _platoon_lock:
        assignments = platoons.load()
        target = next((a for a in assignments if a["id"] == entry), None)
        if target is None:
            await interaction.followup.send(
                "Eintrag nicht gefunden. Bitte einen Vorschlag aus der Liste waehlen.",
                ephemeral=True,
            )
            return
        assignments.remove(target)
        platoons.save(assignments)

    note = "\n-# Die DM wurde bereits verschickt." if target["sent"] else ""
    await interaction.followup.send(f"🗑️ Entfernt: {platoons.label(target)}{note}", ephemeral=True)
    print(f"Platoon-Zuweisung entfernt von {interaction.user.display_name}: {platoons.label(target)}")


@tree.command(name="tbreminder_platoon_list", description="Zeigt alle Platoon-Zuweisungen nach Phase")
async def platoon_list(interaction: discord.Interaction):
    if not can_use_org_commands(interaction):
        await deny_org_only(interaction)
        return

    await interaction.response.defer(ephemeral=True)
    assignments = platoons.load()
    if not assignments:
        await interaction.followup.send("Keine Platoon-Zuweisungen vorhanden.", ephemeral=True)
        return

    run = load_stats().get("current_run", {})
    open_phase = platoons.current_open_phase(run)
    lines = [f"**Platoon-Zuweisungen** ({len(assignments)})"]
    if any(a["tb_index"] is None for a in assignments):
        lines.append("-# (naechster TB) = gilt fuer den naechsten TB-Start")
    for phase in range(1, config.PHASE_COUNT + 1):
        entries = sorted(
            (a for a in assignments if a["phase"] == phase),
            key=lambda a: (a["planet"].lower(), a["unit_name"].lower(), a["player_name"].lower()),
        )
        if not entries:
            continue
        lines.append(f"\n**Phase {phase}**")
        for a in entries:
            if not a["sent"] and a["tb_index"] is not None and open_phase and a["phase"] < open_phase:
                status = "⏭️ (Phase verpasst, keine DM)"
            elif not a["sent"]:
                status = "⏳"
            elif a["result"] == "ok":
                status = "✅"
            else:
                status = f"⚠️ ({a['result']})"
            suffix = " (naechster TB)" if a["tb_index"] is None else ""
            lines.append(f"{status} {a['planet']} · {a['unit_name']} → {a['player_name']}{suffix}")

    await _send_chunked_ephemeral(interaction, "\n".join(lines))


@tree.command(name="tbreminder_help", description="Zeigt alle verfuegbaren Bot-Befehle und ihre Verwendung")
async def help_command(interaction: discord.Interaction):
    help_text = (
        "## TB-Reminder Bot — Befehlsuebersicht\n\n"

        "### 🟢 TB starten\n"
        "**`/tbreminder_start`**\n"
        "Startet die TB-Sequenz sofort. Der Bot kuendigt den TB-Start im TB-Kanal an. "
        "4 Stunden vor jedem Phasenende postet er in rote-organisation die "
        "Spielerauswahl: dort kann jeder die Spieler auswaehlen, die eine persoenliche "
        "DM-Erinnerung bekommen, und bestaetigen. Ohne Bestaetigung geht 1 Stunde vor "
        "Phasenende die generische Erinnerung an alle in den TB-Kanal. Danach folgt "
        "die Auswahl 'Wer hat NICHT stationiert?'.\n\n"

        "**`/tbreminder_timer start_time: DD.MM.YYYY HH:MM`**\n"
        "Plant den TB-Start zu einem bestimmten Zeitpunkt (Serverzeit).\n"
        "Der Bot zeigt die geplante Zeit zur Bestaetigung an bevor der Timer gesetzt wird.\n"
        "Beispiel: `/tbreminder_timer start_time: 20.04.2026 18:00`\n\n"

        "### 🔄 TB fortsetzen & Status\n"
        "**`/tbreminder_resume phase: <1-6> [hours_elapsed: <Stunden>]`**\n"
        "Setzt eine unterbrochene TB-Sequenz fort (z.B. nach Server-Neustart).\n"
        "`phase` = welche Phase als naechstes endet.\n"
        "`hours_elapsed` = Stunden seit dem letzten Trigger bzw. TB-Start. "
        "Wird automatisch aus dem gespeicherten Status berechnet, falls vorhanden.\n"
        "Beispiel: `/tbreminder_resume phase: 6 hours_elapsed: 19.5`\n\n"

        "**`/tbreminder_status`**\n"
        "Zeigt den aktuellen TB-Status: Phasenbeginn, Zeitpunkt der Spielerauswahl, "
        "generische Erinnerung, Phasenende.\n\n"

        "### 📊 Ergebnisse\n"
        "**`/tbreminder_results`**\n"
        "Postet den Abschlussbericht des letzten TBs in diesen Kanal. "
        "Zeigt Erinnerungen und nicht-stationierte Spieler mit Gesamtquoten.\n\n"

        "### ⛔ Abbrechen\n"
        "**`/tbreminder_cancel`**\n"
        "Bricht einen laufenden Timer oder eine aktive TB-Sequenz ab.\n\n"

        "### 🪖 Platoons (in rote-organisation fuer alle nutzbar)\n"
        "**`/tbreminder_requirements_upload file`** *(Admin/Manager)*\n"
        "Laedt die Platoon-Anforderungsliste hoch (CSV: planet, unit_name, "
        "required_count, required_relic). Ersetzt eine bestehende Liste "
        "vollstaendig, mit Bestaetigung und automatischem Backup.\n\n"

        "**`/tbreminder_planets_check planet`**\n"
        "Zeigt Fehlbestand pro Einheit fuer einen Planeten -- diagnostisch, "
        "keine Ping-Buttons.\n\n"

        "**`/tbreminder_planets_ping planet`**\n"
        "Wie planets_check, mit Ping-Button pro Einheit plus 'Alle pingen'. "
        "Der Ping erscheint im TB-Kanal.\n\n"

        "**`/tbreminder_units_check unit relic`**\n"
        "Zeigt alle Besitzer einer Einheit ab einem Relic-Level. Aus der Liste "
        "koennen gezielt Spieler ausgewaehlt und dann im TB-Kanal gepingt oder per "
        "DM ('Bitte stationiere ...') angeschrieben werden. Nur fuer dich sichtbar. "
        "Relic-Angabe wird bei Schiffen ignoriert.\n\n"

        "**`/tbreminder_units_ping unit relic`**\n"
        "Wie `/tbreminder_units_check`, aber alle Besitzer sind vorausgewaehlt.\n\n"

        "### 📌 Platoon-Zuweisungen\n"
        "**`/tbreminder_platoon_register phase planet unit player`** *(Admin/Manager)*\n"
        "Weist einem Spieler eine Einheit fuer einen Planeten in einer Phase zu. "
        "Zu Beginn dieser Phase bekommt der Spieler eine DM mit allen seinen "
        "Zuweisungen fuer die Phase; rote-organisation erhaelt eine Zusammenfassung. "
        "Ohne laufenden TB gilt der Eintrag fuer den naechsten TB.\n\n"

        "**`/tbreminder_platoon_remove entry`** *(Admin/Manager)*\n"
        "Entfernt eine Zuweisung.\n\n"

        "**`/tbreminder_platoon_list`**\n"
        "Zeigt alle Zuweisungen nach Phase (⏳ offen, ✅ zugestellt, ⚠️ nicht erreicht, "
        "⏭️ Phase verpasst). "
        "Nach Phase 6 werden die Zuweisungen des TBs geloescht.\n\n"

        "### ℹ️ Sonstiges\n"
        "**`/tbreminder_help`**\n"
        "Zeigt diese Uebersicht.\n\n"

        "-# TB-Ablaufsteuerung (start, timer, resume, status, results, cancel, "
        "requirements_upload) erfordert Administrator-Rechte oder Manager-Status. "
        "platoon_register/-remove: Admin/Manager. Weitere Platoon-/Unit-Befehle: "
        "alle in rote-organisation, Admins/Manager ueberall."
    )
    # Discord-Limit 2000 Zeichen: abschnittsweise (### ...) zu Nachrichten packen,
    # damit kein Befehl von seiner Beschreibung getrennt wird. Ein einzelner zu
    # langer Abschnitt wird zeilenweise ueber split_message() geteilt.
    parts = help_text.split("\n\n### ")
    sections = [parts[0]] + ["### " + p for p in parts[1:]]
    chunks: list[str] = []
    current = ""
    for section in sections:
        candidate = f"{current}\n\n{section}" if current else section
        if len(candidate) <= 1990:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = section
    if current:
        chunks.append(current)
    chunks = [piece for chunk in chunks for piece in split_message(chunk)]

    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


bot.run(TOKEN)