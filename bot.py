import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN          = os.getenv("DISCORD_TOKEN")
TW_CHANNEL_ID  = int(os.getenv("TW_CHANNEL_ID"))
OFFICER_ID     = int(os.getenv("OFFICER_ID"))
MEMBER_ROLE_ID = int(os.getenv("MEMBER_ROLE_ID"))

HOURS = 3600
OFFICER_TIMEOUT = 1 * HOURS

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


# ── Player selection UI (supports up to 50 members via two dropdowns) ─────────

class PlayerSelectView(discord.ui.View):
    def __init__(self, members: list[discord.Member], phase: int):
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
        # Update selected IDs from whichever dropdown was just used
        chosen = set(interaction.data["values"])
        custom_id = interaction.data["custom_id"]

        # Determine which pool this dropdown covers so we can remove deselected ones
        if custom_id == "select_1":
            pool = {o.value for item in self.children
                    if isinstance(item, discord.ui.Select) and item.custom_id == "select_1"
                    for o in item.options}
        else:
            pool = {o.value for item in self.children
                    if isinstance(item, discord.ui.Select) and item.custom_id == "select_2"
                    for o in item.options}

        # Remove previously selected from this pool, then add new selection
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
        # Try to edit the original DM to inform the officer
        try:
            await self.message.edit(
                content="⏰ Zeit abgelaufen! Keine Auswahl getroffen — eine generische Nachricht wurde gesendet.",
                view=None,
            )
        except Exception:
            pass  # message may no longer be editable


# ── Core logic ────────────────────────────────────────────────────────────────

async def handle_phase_end(phase_index: int, tw_channel: discord.TextChannel):
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

    view = PlayerSelectView(members, phase_index)

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
        for uid in view.selected_ids:
            member = member_map.get(uid)
            if member:
                try:
                    await member.send(f"⚔️ {phase_msg}")
                    sent += 1
                except discord.Forbidden:
                    print(f"⚠️ Konnte {member.display_name} keine DM senden (DMs deaktiviert)")
                    failed += 1
        print(f"✅ Phase {phase_num}: {sent} DMs gesendet, {failed} fehlgeschlagen.")
        await officer.send(
            f"✅ Erledigt! **{sent}** Spieler wurden per DM benachrichtigt" +
            (f", **{failed}** konnten nicht erreicht werden (DMs deaktiviert)." if failed else ".")
        )
    else:
        reason = "Übersprungen" if view._skipped else "Timeout"
        print(f"⚠️ Phase {phase_num}: {reason} — generische Nachricht wird gesendet.")
        await send_generic()


async def run_sequence(tw_channel: discord.TextChannel):
    global is_running
    try:
        await tw_channel.send("📣 @everyone Ein neuer TB hat gestartet!")
        print("✅ TB-Startnachricht gesendet.")

        for i in range(6):
            wait_seconds = 22 * HOURS if i == 0 else 24 * HOURS
            print(f"⏳ Warte {wait_seconds // HOURS}h bis Phase {i + 1} endet...")
            await asyncio.sleep(wait_seconds)
            await handle_phase_end(i, tw_channel)

        print("🏁 Alle 6 Phasen abgeschlossen. TB-Sequenz beendet.")
    except Exception as e:
        print(f"❌ Unerwarteter Fehler: {e}")
        raise
    finally:
        is_running = False


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Eingeloggt als {bot.user} (ID: {bot.user.id})")
    print(f"   tw_channel  : {TW_CHANNEL_ID}")
    print(f"   officer     : {OFFICER_ID}")
    print(f"   rolle       : {MEMBER_ROLE_ID}")


@tree.command(name="start", description="Startet die TB-Phasen-Ankündigungen")
@app_commands.checks.has_permissions(administrator=True)
async def start(interaction: discord.Interaction):
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


@start.error
async def start_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ Du benötigst Administrator-Rechte für diesen Befehl.",
            ephemeral=True,
        )


bot.run(TOKEN)
