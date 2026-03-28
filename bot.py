import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import json
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

GENERIC_REMINDER = "Bitte denkt dran im TB zu stationieren!"

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

is_running = False


# ── Stats persistence ─────────────────────────────────────────────────────────
# stats.json structure:
# {
#   "total_tbs": 3,          ← how many TBs have been started total
#   "players": {
#     "<user_id>": {
#       "name": "Spielername",
#       "total_reminders": 7,
#       "total_tbs": 3,        ← how many TBs this player was present for
#       "tb_history": [2, 0, 3] ← reminders per TB (0 = present but not reminded)
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
    """Current TB index = number of TBs started so far."""
    return stats.get("total_tbs", 0)


def record_participation(stats: dict, members: list[discord.Member]):
    """
    Called once at TB start. Registers all current role members as
    participating in this TB and increments the global TB counter.
    """
    tb_index = get_tb_index(stats)

    for m in members:
        uid = str(m.id)
        if uid not in stats["players"]:
            stats["players"][uid] = {
                "name": m.display_name,
                "total_reminders": 0,
                "total_tbs": 0,
                "tb_history": [],
            }
        player = stats["players"][uid]
        player["name"] = m.display_name  # keep name current

        # Pad tb_history up to current TB index with 0s (handles players who
        # joined mid-way through previous TBs)
        while len(player["tb_history"]) < tb_index:
            player["tb_history"].append(0)

        # Add a 0 slot for this TB (will be incremented later if reminded)
        player["tb_history"].append(0)
        player["total_tbs"] += 1

    # Advance the global TB counter
    stats["total_tbs"] = tb_index + 1
    save_stats(stats)


def record_reminders(stats: dict, reminded_members: list[tuple[str, str]], tb_index: int):
    """Increment reminder count for each player the officer picked."""
    for uid, name in reminded_members:
        if uid not in stats["players"]:
            # Shouldn't happen (record_participation runs first), but handle it
            stats["players"][uid] = {
                "name": name,
                "total_reminders": 0,
                "total_tbs": 1,
                "tb_history": [0] * (tb_index + 1),
            }
        player = stats["players"][uid]
        player["name"] = name

        # Ensure tb_history is long enough
        while len(player["tb_history"]) <= tb_index:
            player["tb_history"].append(0)

        player["tb_history"][tb_index] += 1
        player["total_reminders"] += 1

    save_stats(stats)


# ── Player selection UI ───────────────────────────────────────────────────────

class PlayerSelectView(discord.ui.View):
    def __init__(self, members: list[discord.Member]):
        super().__init__(timeout=OFFICER_TIMEOUT)
        self.selected_ids: set[str] = set()
        self.confirmed = False
        self._skipped = False

        chunk1 = members[:25]
        chunk2 = members[25:50]

        self._add_select(chunk1, "Spieler 1–25 auswählen...", "select_1")
        if chunk2:
            self._add_select(chunk2, "Spieler 26–50 auswählen...", "select_2")

        confirm_btn = discord.ui.Button(
            label="✅ Bestätigen & senden",
            style=discord.ButtonStyle.green,
            row=2,
        )
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

        skip_btn = discord.ui.Button(
            label="⏭️ Überspringen (generische Nachricht)",
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
            f"Aktuell ausgewählt: **{len(self.selected_ids)} Spieler**\n"
            "Drücke *Bestätigen* wenn du fertig bist.",
            ephemeral=True,
        )

    async def _on_confirm(self, interaction: discord.Interaction):
        if not self.selected_ids:
            await interaction.response.send_message(
                "⚠️ Du hast noch niemanden ausgewählt!", ephemeral=True
            )
            return
        self.confirmed = True
        await interaction.response.edit_message(
            content=f"✅ Bestätigt! {len(self.selected_ids)} Spieler erhalten eine persönliche Nachricht.",
            view=None,
        )
        self.stop()

    async def _on_skip(self, interaction: discord.Interaction):
        self.confirmed = False
        self._skipped = True
        await interaction.response.edit_message(
            content="⏭️ Übersprungen. Eine generische Nachricht wird gesendet.",
            view=None,
        )
        self.stop()

    async def on_timeout(self):
        self.confirmed = False
        self._skipped = False
        try:
            await self.message.edit(
                content="⏰ Zeit abgelaufen! Keine Auswahl getroffen — eine generische Nachricht wurde gesendet.",
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
):
    phase_num = phase_index + 1
    print(f"⏰ Phase {phase_num} endet bald — Officer wird kontaktiert...")

    async def send_generic():
        await tw_channel.send(f"@everyone {GENERIC_REMINDER}")

    try:
        officer = await bot.fetch_user(OFFICER_ID)
    except discord.NotFound:
        print(f"❌ Officer (ID {OFFICER_ID}) nicht gefunden — generische Nachricht wird gesendet")
        await send_generic()
        return
    except discord.HTTPException as e:
        print(f"❌ Netzwerkfehler beim Abrufen des Officers ({e}) — generische Nachricht wird gesendet")
        await send_generic()
        return

    guild  = tw_channel.guild
    role   = guild.get_role(MEMBER_ROLE_ID)

    if role is None:
        print(f"❌ Rolle (ID {MEMBER_ROLE_ID}) nicht gefunden — generische Nachricht wird gesendet")
        await send_generic()
        return

    members = [m for m in role.members if not m.bot][:50]

    if not members:
        print("⚠️ Keine Mitglieder gefunden — generische Nachricht wird gesendet")
        await send_generic()
        return

    view = PlayerSelectView(members)

    try:
        await officer.send(
            f"⚔️ **Phase {phase_num} endet bald!**\n"
            f"Wähle die Spieler aus, die eine persönliche Nachricht erhalten sollen.\n"
            f"Du hast **1 Stunde** Zeit. Danach wird automatisch eine generische Nachricht in #tw-territorialkrieg gesendet.\n\n"
            f"*(Spieler in den Dropdowns auswählen, dann auf Bestätigen klicken)*",
            view=view,
        )
    except discord.Forbidden:
        print("❌ Officer hat DMs deaktiviert — generische Nachricht wird gesendet")
        await send_generic()
        return

    await view.wait()

    phase_msg = PHASE_END_MESSAGES[phase_index]

    if view.confirmed and view.selected_ids:
        member_map = {str(m.id): m for m in members}
        sent, failed = 0, 0
        reminded = []  # (uid, name) pairs for stats

        for uid in view.selected_ids:
            member = member_map.get(uid)
            if member:
                try:
                    await member.send(f"⚔️ {phase_msg}")
                    sent += 1
                    reminded.append((uid, member.display_name))
                except discord.Forbidden:
                    print(f"⚠️ Konnte {member.display_name} keine DM senden (DMs deaktiviert)")
                    failed += 1

        # Persist reminder counts
        record_reminders(stats, reminded, tb_index)

        print(f"✅ Phase {phase_num}: {sent} DMs gesendet, {failed} fehlgeschlagen.")
        await officer.send(
            f"✅ Erledigt! **{sent}** Spieler wurden per DM benachrichtigt" +
            (f", **{failed}** konnten nicht erreicht werden (DMs deaktiviert)." if failed else ".")
        )
    else:
        reason = "Übersprungen" if view._skipped else "Timeout"
        print(f"⚠️ Phase {phase_num}: {reason} — generische Nachricht wird gesendet.")
        await send_generic()


async def send_stats_summary(officer: discord.User, stats: dict, tb_index: int):
    """DM the officer a ranked summary of reminders for the TB that just finished."""
    players = stats.get("players", {})

    # Include all players who participated in this TB
    rows = []
    for data in players.values():
        if len(data["tb_history"]) <= tb_index:
            continue  # wasn't present this TB
        reminders_this_tb = data["tb_history"][tb_index]
        total_tbs = data.get("total_tbs", 1)
        total_reminders = data.get("total_reminders", 0)
        max_possible = total_tbs * 6
        quote = round((total_reminders / max_possible) * 100) if max_possible > 0 else 0
        rows.append((data["name"], reminders_this_tb, total_tbs, total_reminders, max_possible, quote))

    if not rows:
        await officer.send(
            "📊 **TB abgeschlossen!**\n"
            "Keine Teilnehmerdaten für diesen TB gefunden."
        )
        return

    # Sort: most reminded this TB first, then by overall quote
    rows.sort(key=lambda x: (x[1], x[5]), reverse=True)

    lines = ["📊 **TB-Abschlussbericht — Erinnerungen**\n"]
    lines.append(f"{'Spieler':<20} {'Dieser TB':>10} {'TBs dabei':>10} {'Quote':>14}")
    lines.append("─" * 58)
    for name, this_tb, total_tbs, total_reminders, max_possible, quote in rows:
        if total_reminders == 0:
            continue
        fraction = f"({total_reminders}/{max_possible})"
        quote_str = f"{quote}% {fraction}"
        lines.append(f"{name:<20} {this_tb:>10} {total_tbs:>10} {quote_str:>14}")

    if len(lines) == 3:  # only header + divider, no data rows
        await officer.send(
            "📊 **TB abgeschlossen!**\n"
            "In diesem TB wurde niemand persönlich erinnert."
        )
        return

    await officer.send("```\n" + "\n".join(lines) + "\n```")


async def run_sequence(tw_channel: discord.TextChannel):
    global is_running
    stats = load_stats()
    tb_index = get_tb_index(stats)

    try:
        # Record all current role members as participants before starting
        guild = tw_channel.guild
        role  = guild.get_role(MEMBER_ROLE_ID)
        if role:
            members = [m for m in role.members if not m.bot][:50]
            record_participation(stats, members)
            print(f"📋 {len(members)} Spieler als TB-Teilnehmer registriert.")
        else:
            print("⚠️ Rolle nicht gefunden — Teilnahme wird nicht getrackt.")

        await tw_channel.send("📣 @everyone Ein neuer TB hat gestartet!")
        print(f"✅ TB-Startnachricht gesendet. (TB #{tb_index + 1} in den Stats)")

        for i in range(6):
            wait_seconds = 22 * HOURS if i == 0 else 24 * HOURS
            print(f"⏳ Warte {wait_seconds // HOURS}h bis Phase {i + 1} endet...")
            await asyncio.sleep(wait_seconds)
            await handle_phase_end(i, tw_channel, stats, tb_index)

        print("🏁 Alle 6 Phasen abgeschlossen. TB-Sequenz beendet.")

        # Send summary to officer
        try:
            officer = await bot.fetch_user(OFFICER_ID)
            await send_stats_summary(officer, stats, tb_index)
        except discord.HTTPException as e:
            print(f"⚠️ Konnte Zusammenfassung nicht senden (Netzwerkfehler: {e})")
        except Exception as e:
            print(f"⚠️ Konnte Zusammenfassung nicht senden: {e}")

    except Exception as e:
        print(f"❌ Unerwarteter Fehler: {e}")
        raise
    finally:
        is_running = False


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.event
@bot.event
async def on_ready():
    guild = discord.Object(id=1269591429227745332)
    tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"✅ Eingeloggt als {bot.user} (ID: {bot.user.id})")
    print(f"   tw_channel  : {TW_CHANNEL_ID}")
    print(f"   officer     : {OFFICER_ID}")
    print(f"   rolle       : {MEMBER_ROLE_ID}")

@tree.command(name="start_tb_bot", description="Startet die TB-Phasen-Ankündigungen")
async def start(interaction: discord.Interaction):
    is_admin = interaction.user.guild_permissions.administrator
    is_officer = interaction.user.id in MANAGER_IDS

    if not is_admin and not is_officer:
        await interaction.response.send_message(
            "❌ Du benötigst Administrator-Rechte oder Officer-Status für diesen Befehl.",
            ephemeral=True,
        )
        return
    global is_running

    if is_running:
        await interaction.response.send_message(
            "⚠️ Eine TB-Sequenz läuft bereits! Warte bis sie abgeschlossen ist.",
            ephemeral=True,
        )
        return

    channel = bot.get_channel(TW_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"❌ Kanal mit ID `{TW_CHANNEL_ID}` nicht gefunden. Bitte `.env` prüfen.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ TB-Sequenz gestartet! Nachrichten gehen in {channel.mention}.",
        ephemeral=True,
    )

    is_running = True
    asyncio.create_task(run_sequence(channel))


bot.run(TOKEN)