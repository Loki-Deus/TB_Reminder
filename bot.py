import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import json
import time
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

PHASE_END_MESSAGES = [
    "Phase 1 endet bald!",
    "Phase 2 endet bald!",
    "Phase 3 endet bald!",
    "Phase 4 endet bald!",
    "Phase 5 endet bald!",
    "Phase 6 endet bald, holt nochmal alles raus!",
]

# CHANGE 1: Territory Battle (not Territorialkrieg)
GENERIC_REMINDER = "Bitte denkt dran im Territory Battle zu stationieren!"

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

is_running = False


# ── Stats persistence ─────────────────────────────────────────────────────────
# stats.json structure:
# {
#   "total_tbs": 3,          <- how many Territory Battles have been started total
#   "players": {
#     "<user_id>": {
#       "name": "Spielername",
#       "total_reminders": 7,
#       "total_failed": 2,     <- total phases this player failed to set across all TBs
#       "total_tbs": 3,        <- how many TBs this player was present for
#       "tb_history": [2, 0, 3],    <- reminders per TB (0 = present but not reminded)
#       "failed_history": [1, 0, 1] <- failed-to-set count per TB
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
        player["name"] = m.display_name  # keep name current

        # Pad both histories up to current TB index with 0s
        while len(player["tb_history"]) < tb_index:
            player["tb_history"].append(0)
        while len(player.setdefault("failed_history", [])) < tb_index:
            player["failed_history"].append(0)

        # Add a 0 slot for this TB (will be incremented later as needed)
        player["tb_history"].append(0)
        player["failed_history"].append(0)
        player["total_tbs"] += 1

    # Advance the global TB counter
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


# ── Player selection UI ───────────────────────────────────────────────────────

class PlayerSelectView(discord.ui.View):
    """Officer picks players to send a personal reminder to."""
    def __init__(self, members: list[discord.Member]):
        super().__init__(timeout=OFFICER_TIMEOUT)
        self.selected_ids: set[str] = set()
        self.confirmed = False
        self._skipped = False

        # CHANGE 2: members are passed in already sorted alphabetically
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


# CHANGE 3: New view for logging players who failed to set their troops
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

    # CHANGE 2: sort alphabetically before slicing to 50
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
        reminded = []  # (uid, name) pairs for stats

        for uid in reminder_view.selected_ids:
            member = member_map.get(uid)
            if member:
                try:
                    await member.send(f"{phase_msg}")
                    sent += 1
                    reminded.append((uid, member.display_name))
                except discord.Forbidden:
                    print(f"Konnte {member.display_name} keine DM senden (DMs deaktiviert)")
                    failed_dm += 1

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

    # Step 2 - Failed-to-set picker: fire and forget, does NOT block the phase loop
    async def send_failed_picker():
        # Last phase gets 22h; others get next_phase_wait minus 1h buffer
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

        # Last phase: fire summary immediately after officer responds (or times out)
        if is_last_phase:
            await send_stats_summary(officer, stats, tb_index)

    asyncio.create_task(send_failed_picker())


async def send_stats_summary(officer: discord.User, stats: dict, tb_index: int):
    """DM the officer a ranked summary of reminders AND failed-to-set for the TB that just finished."""
    players = stats.get("players", {})

    rows = []
    for data in players.values():
        if len(data["tb_history"]) <= tb_index:
            continue  # wasn't present this TB
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
        await officer.send(
            "**Territory Battle abgeschlossen!**\n"
            "Keine Teilnehmerdaten fuer diesen TB gefunden."
        )
        return

    # ── Reminder summary (sorted: most reminded this TB first, then by overall quote) ──
    reminder_rows = sorted(rows, key=lambda x: (x[1], x[7]), reverse=True)
    r_lines = ["**TB-Abschlussbericht - Erinnerungen**\n"]
    r_lines.append(f"{'Spieler':<20} {'Dieser TB':>10} {'TBs dabei':>10} {'Quote':>14}")
    r_lines.append("-" * 58)
    for name, rem_tb, _, total_tbs, total_reminders, _, max_possible, reminder_quote, _ in reminder_rows:
        if total_reminders == 0:
            continue
        fraction = f"({total_reminders}/{max_possible})"
        r_lines.append(f"{name:<20} {rem_tb:>10} {total_tbs:>10} {f'{reminder_quote}% {fraction}':>14}")

    if len(r_lines) > 3:
        await officer.send("```\n" + "\n".join(r_lines) + "\n```")
    else:
        await officer.send(
            "**Territory Battle abgeschlossen!**\n"
            "In diesem TB wurde niemand persoenlich erinnert."
        )

    # ── Failed-to-set summary (sorted: most failures this TB first, then by overall quote) ──
    failed_rows = sorted(rows, key=lambda x: (x[2], x[8]), reverse=True)
    f_lines = ["**TB-Abschlussbericht - Nicht stationiert**\n"]
    f_lines.append(f"{'Spieler':<20} {'Dieser TB':>10} {'TBs dabei':>10} {'Quote':>14}")
    f_lines.append("-" * 58)
    for name, _, fail_tb, total_tbs, _, total_failed, max_possible, _, failed_quote in failed_rows:
        if total_failed == 0:
            continue
        fraction = f"({total_failed}/{max_possible})"
        f_lines.append(f"{name:<20} {fail_tb:>10} {total_tbs:>10} {f'{failed_quote}% {fraction}':>14}")

    if len(f_lines) > 3:
        await officer.send("```\n" + "\n".join(f_lines) + "\n```")
    else:
        await officer.send(
            "**Territory Battle abgeschlossen!**\n"
            "In diesem TB hat niemand das Stationieren verpasst - gut gemacht!"
        )


async def run_sequence(tw_channel: discord.TextChannel):
    global is_running
    stats = load_stats()
    tb_index = get_tb_index(stats)

    try:
        guild = tw_channel.guild
        role  = guild.get_role(MEMBER_ROLE_ID)
        if role:
            # CHANGE 2: sort alphabetically
            members = sorted(
                [m for m in role.members if not m.bot],
                key=lambda m: m.display_name.lower()
            )[:50]
            record_participation(stats, members)
            print(f"{len(members)} Spieler als TB-Teilnehmer registriert.")
        else:
            print("Rolle nicht gefunden - Teilnahme wird nicht getrackt.")

        # CHANGE 1: Territory Battle
        await tw_channel.send("@everyone Ein neues Territory Battle hat gestartet!")
        print(f"TB-Startnachricht gesendet. (TB #{tb_index + 1} in den Stats)")

        carry_over = 0.0
        for i in range(6):
            wait_seconds = (22 * HOURS if i == 0 else 24 * HOURS) - carry_over
            next_phase_wait = 24 * HOURS if i < 5 else 0
            last_phase = (i == 5)
            print(f"Warte {wait_seconds / HOURS:.2f}h bis Phase {i + 1} endet...")
            await asyncio.sleep(max(0, wait_seconds))
            t0 = time.monotonic()
            await handle_phase_end(i, tw_channel, stats, tb_index, next_phase_wait, last_phase)
            carry_over = time.monotonic() - t0
            print(f"Phase {i + 1} Interaktion dauerte {carry_over / 60:.1f} min - wird von naechster Phase abgezogen.")

        # Summary is now triggered from within the phase 6 failed picker background task
        print("Alle 6 Phasen abgeschlossen. Territory Battle Sequenz beendet.")

    except Exception as e:
        print(f"Unerwarteter Fehler: {e}")
        raise
    finally:
        is_running = False


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.event  # fixed: duplicate @bot.event decorator removed
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


@tree.command(name="start_tb_bot", description="Startet die Territory Battle Phasen-Ankuendigungen")
async def start(interaction: discord.Interaction):
    is_admin   = interaction.user.guild_permissions.administrator
    is_manager = interaction.user.id in MANAGER_IDS

    if not is_admin and not is_manager:
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


bot.run(TOKEN)