import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import asyncio

# ─── Config laden ────────────────────────────────────────────────────────────

CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ─── Bot Setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# guild_id -> { "creator_channel_id": int, "category_id": int, "temp_channels": [int, ...] }
config = load_config()

# ─── Helper ───────────────────────────────────────────────────────────────────

def get_guild_config(guild_id: int) -> dict:
    return config.get(str(guild_id), {})

def save_guild_config(guild_id: int, data: dict):
    config[str(guild_id)] = data
    save_config(config)

# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Bot ist online als {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"🔁 {len(synced)} Slash-Commands synchronisiert.")
    except Exception as e:
        print(f"❌ Fehler beim Sync: {e}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild_cfg = get_guild_config(member.guild.id)
    if not guild_cfg:
        return

    creator_id = guild_cfg.get("creator_channel_id")
    category_id = guild_cfg.get("category_id")
    temp_channels = guild_cfg.get("temp_channels", [])

    # ── Nutzer betritt den Creator-Channel → neuen Temp-Channel erstellen ──
    if after.channel and after.channel.id == creator_id:
        category = member.guild.get_channel(category_id)
        channel_name = f"🔊 {member.display_name}'s Channel"

        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(connect=True, speak=True),
            member: discord.PermissionOverwrite(
                manage_channels=True,
                move_members=True,
                mute_members=True,
                connect=True,
                speak=True
            ),
            member.guild.me: discord.PermissionOverwrite(manage_channels=True, connect=True)
        }

        new_channel = await member.guild.create_voice_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites
        )

        await member.move_to(new_channel)

        temp_channels.append(new_channel.id)
        guild_cfg["temp_channels"] = temp_channels
        save_guild_config(member.guild.id, guild_cfg)

        print(f"📢 Temp-Channel erstellt: {new_channel.name} für {member.display_name}")

    # ── Nutzer verlässt einen Temp-Channel → löschen wenn leer ──
    if before.channel and before.channel.id in temp_channels:
        channel = before.channel
        if len(channel.members) == 0:
            await channel.delete(reason="Temporärer Channel ist leer.")
            temp_channels.remove(channel.id)
            guild_cfg["temp_channels"] = temp_channels
            save_guild_config(member.guild.id, guild_cfg)
            print(f"🗑️  Temp-Channel gelöscht: {channel.name}")

# ─── Slash Commands ───────────────────────────────────────────────────────────

@bot.tree.command(name="setup", description="Richtet das TempVC-System ein (nur Admins).")
@app_commands.describe(
    channel="Der 'Erstellen'-Channel, den Nutzer betreten sollen",
    category="Kategorie, in der Temp-Channels erstellt werden"
)
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.VoiceChannel, category: discord.CategoryChannel):
    guild_cfg = get_guild_config(interaction.guild.id)
    guild_cfg["creator_channel_id"] = channel.id
    guild_cfg["category_id"] = category.id
    guild_cfg.setdefault("temp_channels", [])
    save_guild_config(interaction.guild.id, guild_cfg)

    embed = discord.Embed(
        title="✅ TempVC Setup abgeschlossen",
        color=discord.Color.green()
    )
    embed.add_field(name="Creator-Channel", value=channel.mention, inline=True)
    embed.add_field(name="Kategorie", value=category.name, inline=True)
    embed.set_footer(text="Nutzer können jetzt durch Betreten des Channels eigene Räume erstellen.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="vc-rename", description="Benenne deinen temporären Voice Channel um.")
@app_commands.describe(name="Neuer Name für deinen Channel")
async def vc_rename(interaction: discord.Interaction, name: str):
    guild_cfg = get_guild_config(interaction.guild.id)
    temp_channels = guild_cfg.get("temp_channels", [])

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Du bist in keinem Voice Channel.", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    if channel.id not in temp_channels:
        await interaction.response.send_message("❌ Du bist nicht in einem temporären Channel.", ephemeral=True)
        return

    if not channel.permissions_for(interaction.user).manage_channels:
        await interaction.response.send_message("❌ Du bist nicht der Besitzer dieses Channels.", ephemeral=True)
        return

    old_name = channel.name
    await channel.edit(name=name)
    await interaction.response.send_message(f"✅ Channel umbenannt: **{old_name}** → **{name}**", ephemeral=True)

@bot.tree.command(name="vc-limit", description="Setze ein Nutzerlimit für deinen Channel.")
@app_commands.describe(limit="Maximale Nutzerzahl (0 = unbegrenzt)")
async def vc_limit(interaction: discord.Interaction, limit: int):
    guild_cfg = get_guild_config(interaction.guild.id)
    temp_channels = guild_cfg.get("temp_channels", [])

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Du bist in keinem Voice Channel.", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    if channel.id not in temp_channels:
        await interaction.response.send_message("❌ Du bist nicht in einem temporären Channel.", ephemeral=True)
        return

    if not channel.permissions_for(interaction.user).manage_channels:
        await interaction.response.send_message("❌ Du bist nicht der Besitzer dieses Channels.", ephemeral=True)
        return

    await channel.edit(user_limit=max(0, min(limit, 99)))
    msg = f"✅ Nutzerlimit gesetzt auf **{limit}**." if limit > 0 else "✅ Nutzerlimit entfernt (unbegrenzt)."
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="vc-lock", description="Sperre deinen Channel für neue Nutzer.")
async def vc_lock(interaction: discord.Interaction):
    guild_cfg = get_guild_config(interaction.guild.id)
    temp_channels = guild_cfg.get("temp_channels", [])

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Du bist in keinem Voice Channel.", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    if channel.id not in temp_channels:
        await interaction.response.send_message("❌ Du bist nicht in einem temporären Channel.", ephemeral=True)
        return

    if not channel.permissions_for(interaction.user).manage_channels:
        await interaction.response.send_message("❌ Du bist nicht der Besitzer dieses Channels.", ephemeral=True)
        return

    await channel.set_permissions(interaction.guild.default_role, connect=False)
    await interaction.response.send_message("🔒 Channel gesperrt. Niemand kann mehr joinen.", ephemeral=True)

@bot.tree.command(name="vc-unlock", description="Entsperre deinen Channel wieder.")
async def vc_unlock(interaction: discord.Interaction):
    guild_cfg = get_guild_config(interaction.guild.id)
    temp_channels = guild_cfg.get("temp_channels", [])

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Du bist in keinem Voice Channel.", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    if channel.id not in temp_channels:
        await interaction.response.send_message("❌ Du bist nicht in einem temporären Channel.", ephemeral=True)
        return

    if not channel.permissions_for(interaction.user).manage_channels:
        await interaction.response.send_message("❌ Du bist nicht der Besitzer dieses Channels.", ephemeral=True)
        return

    await channel.set_permissions(interaction.guild.default_role, connect=True)
    await interaction.response.send_message("🔓 Channel entsperrt. Jeder kann wieder joinen.", ephemeral=True)

@bot.tree.command(name="vc-kick", description="Kicke einen Nutzer aus deinem Channel.")
@app_commands.describe(member="Der Nutzer, den du kicken möchtest")
async def vc_kick(interaction: discord.Interaction, member: discord.Member):
    guild_cfg = get_guild_config(interaction.guild.id)
    temp_channels = guild_cfg.get("temp_channels", [])

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Du bist in keinem Voice Channel.", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    if channel.id not in temp_channels:
        await interaction.response.send_message("❌ Du bist nicht in einem temporären Channel.", ephemeral=True)
        return

    if not channel.permissions_for(interaction.user).move_members:
        await interaction.response.send_message("❌ Du bist nicht der Besitzer dieses Channels.", ephemeral=True)
        return

    if member.voice and member.voice.channel == channel:
        await member.move_to(None)
        await interaction.response.send_message(f"👢 **{member.display_name}** wurde aus dem Channel geworfen.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ **{member.display_name}** ist nicht in deinem Channel.", ephemeral=True)

@bot.tree.command(name="vc-info", description="Zeigt das aktuelle Setup des TempVC-Systems an.")
@app_commands.checks.has_permissions(administrator=True)
async def vc_info(interaction: discord.Interaction):
    guild_cfg = get_guild_config(interaction.guild.id)

    if not guild_cfg:
        await interaction.response.send_message("❌ Das TempVC-System ist noch nicht eingerichtet. Nutze `/setup`.", ephemeral=True)
        return

    creator = interaction.guild.get_channel(guild_cfg.get("creator_channel_id"))
    category = interaction.guild.get_channel(guild_cfg.get("category_id"))
    temp_count = len(guild_cfg.get("temp_channels", []))

    embed = discord.Embed(title="📋 TempVC Status", color=discord.Color.blurple())
    embed.add_field(name="Creator-Channel", value=creator.mention if creator else "Nicht gefunden", inline=True)
    embed.add_field(name="Kategorie", value=category.name if category else "Nicht gefunden", inline=True)
    embed.add_field(name="Aktive Temp-Channels", value=str(temp_count), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── Error Handler ────────────────────────────────────────────────────────────

@setup.error
@vc_info.error
async def admin_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Du brauchst Admin-Rechte für diesen Befehl.", ephemeral=True)

# ─── Start ────────────────────────────────────────────────────────────────────

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("❌ DISCORD_TOKEN Umgebungsvariable nicht gesetzt!")

bot.run(TOKEN)
