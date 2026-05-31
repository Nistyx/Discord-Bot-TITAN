import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import asyncio
import datetime
import random
import urllib.request
import urllib.parse
from collections import defaultdict
from aiohttp import web

# ─── Config ──────────────────────────────────────────────────────────────────

CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_guild_config(guild_id: int) -> dict:
    return config.get(str(guild_id), {})

def save_guild_config(guild_id: int, data: dict):
    config[str(guild_id)] = data
    save_config(config)

# ─── Bot Setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
config = load_config()

xp_data = defaultdict(lambda: defaultdict(lambda: {"xp": 0, "level": 0, "voice_joined": None, "last_message": 0}))
warns_data = defaultdict(lambda: defaultdict(list))
# Musik-Queue: guild_id -> list of {"title": str, "url": str}
music_queues = defaultdict(list)
# Aktive Giveaways: guild_id -> list of {"message_id", "channel_id", "prize", "end_time", "winners"}
giveaways = defaultdict(list)

XP_PER_MESSAGE = 10
XP_PER_VOICE_MINUTE = 3

def xp_for_level(level):
    return 250 * (level + 1)

async def send_log(guild, message: str, color=discord.Color.blurple()):
    cfg = get_guild_config(guild.id)
    log_channel_id = cfg.get("log_channel_id")
    if log_channel_id:
        ch = guild.get_channel(log_channel_id)
        if ch:
            embed = discord.Embed(description=message, color=color, timestamp=datetime.datetime.utcnow())
            try:
                await ch.send(embed=embed)
            except:
                pass

def get_temp_channel(interaction: discord.Interaction):
    guild_cfg = get_guild_config(interaction.guild.id)
    temp_channels = guild_cfg.get("temp_channels", {})
    if not interaction.user.voice or not interaction.user.voice.channel:
        return None, None, "❌ You are not in a voice channel."
    channel = interaction.user.voice.channel
    if str(channel.id) not in temp_channels:
        return None, None, "❌ You are not in a temporary channel."
    owner_id = temp_channels[str(channel.id)]
    if owner_id != interaction.user.id:
        return None, None, "❌ You are not the owner of this channel."
    return channel, guild_cfg, None

# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"🔁 {len(synced)} Commands synchronisiert.")
    except Exception as e:
        print(f"❌ Sync-Fehler: {e}")
    bot.loop.create_task(voice_xp_loop())
    bot.loop.create_task(giveaway_loop())
    bot.loop.create_task(star_citizen_loop())
    bot.loop.create_task(start_api())

@bot.event
async def on_member_join(member: discord.Member):
    cfg = get_guild_config(member.guild.id)
    auto_role_id = cfg.get("auto_role_id")
    if auto_role_id:
        role = member.guild.get_role(auto_role_id)
        if role:
            try:
                await member.add_roles(role)
            except:
                pass
    welcome_channel_id = cfg.get("welcome_channel_id")
    welcome_msg = cfg.get("welcome_message", "Willkommen auf dem Server, {user}! 🎉")
    if welcome_channel_id:
        ch = member.guild.get_channel(welcome_channel_id)
        if ch:
            embed = discord.Embed(
                title="👋 Welcome!",
                description=welcome_msg.replace("{user}", member.mention),
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"Mitglied #{member.guild.member_count}")
            try:
                await ch.send(embed=embed)
            except:
                pass
    await send_log(member.guild, f"➕ **{member}** hat den Server betreten.", discord.Color.green())

@bot.event
async def on_member_remove(member: discord.Member):
    await send_log(member.guild, f"➖ **{member}** hat den Server verlassen.", discord.Color.red())

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    await bot.process_commands(message)
    uid = str(message.author.id)
    gid = str(message.guild.id)
    user_xp = xp_data[gid][uid]
    now = datetime.datetime.utcnow().timestamp()
    if now - user_xp.get("last_message", 0) < 60:
        return
    user_xp["last_message"] = now
    user_xp["xp"] += XP_PER_MESSAGE
    needed = xp_for_level(user_xp["level"])
    if user_xp["xp"] >= needed:
        user_xp["xp"] -= needed
        user_xp["level"] += 1
        lvl = user_xp["level"]
        try:
            await message.channel.send(f"🎉 {message.author.mention} reached **Level {lvl}**!", delete_after=10)
        except:
            pass
        cfg = get_guild_config(message.guild.id)
        level_roles = cfg.get("level_roles", {})
        role_id = level_roles.get(str(lvl))
        if role_id:
            role = message.guild.get_role(role_id)
            if role:
                try:
                    await message.author.add_roles(role)
                except:
                    pass

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild_cfg = get_guild_config(member.guild.id)
    if not guild_cfg:
        return
    creator_id = guild_cfg.get("creator_channel_id")
    category_id = guild_cfg.get("category_id")
    temp_channels = guild_cfg.get("temp_channels", {})

    uid = str(member.id)
    gid = str(member.guild.id)
    if after.channel and not before.channel:
        xp_data[gid][uid]["voice_joined"] = datetime.datetime.utcnow().timestamp()
    elif before.channel and not after.channel:
        joined = xp_data[gid][uid].get("voice_joined")
        if joined:
            minutes = (datetime.datetime.utcnow().timestamp() - joined) / 60
            xp_data[gid][uid]["xp"] += int(minutes * XP_PER_VOICE_MINUTE)
            xp_data[gid][uid]["voice_joined"] = None

    if after.channel and after.channel.id == creator_id:
        category = member.guild.get_channel(category_id)
        channel_name = f"🔊 {member.display_name}'s Channel"
        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(connect=True, speak=True),
            member: discord.PermissionOverwrite(manage_channels=True, move_members=True, mute_members=True, connect=True, speak=True),
            member.guild.me: discord.PermissionOverwrite(manage_channels=True, connect=True)
        }
        new_channel = await member.guild.create_voice_channel(name=channel_name, category=category, overwrites=overwrites)
        await member.move_to(new_channel)
        temp_channels[str(new_channel.id)] = member.id
        guild_cfg["temp_channels"] = temp_channels
        save_guild_config(member.guild.id, guild_cfg)
        if category:
            for ch in category.text_channels:
                try:
                    embed = discord.Embed(
                        title="🔊 Your temporary channel",
                        description=(
                            f"{member.mention}, your channel has been created!\n\n"
                            "**Voice commands:**\n"
                            "`/rename` `/limit` `/lock` `/unlock`\n"
                            "`/kick` `/ban` `/invite` `/transfer`"
                        ),
                        color=discord.Color.blurple()
                    )
                    await ch.send(embed=embed, delete_after=30)
                    break
                except:
                    pass
        await send_log(member.guild, f"📢 **{member}** hat Temp-Channel **{new_channel.name}** erstellt.", discord.Color.blurple())

    if before.channel and str(before.channel.id) in temp_channels:
        channel = before.channel
        if len(channel.members) == 0:
            name = channel.name
            await channel.delete(reason="Temporärer Channel ist leer.")
            del temp_channels[str(channel.id)]
            guild_cfg["temp_channels"] = temp_channels
            save_guild_config(member.guild.id, guild_cfg)
            await send_log(member.guild, f"🗑️ Temp-Channel **{name}** wurde gelöscht.", discord.Color.orange())

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.emoji.name != "🎉":
        return
    gid = str(payload.guild_id)
    for gw in giveaways[gid]:
        if gw["message_id"] == payload.message_id and payload.user_id != bot.user.id:
            if payload.user_id not in gw.get("participants", []):
                gw.setdefault("participants", []).append(payload.user_id)

# ─── Background Loops ─────────────────────────────────────────────────────────

async def voice_xp_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(60)
        for guild in bot.guilds:
            for member in guild.members:
                if member.voice and member.voice.channel and not member.bot:
                    uid = str(member.id)
                    gid = str(guild.id)
                    xp_data[gid][uid]["xp"] += XP_PER_VOICE_MINUTE

async def star_citizen_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(300)  # alle 5 Minuten aktualisieren
        for guild in bot.guilds:
            cfg = get_guild_config(guild.id)
            sc_channel_id = cfg.get("sc_channel_id")
            if not sc_channel_id:
                continue
            channel = guild.get_channel(sc_channel_id)
            if not channel:
                continue
            count = 0
            for member in guild.members:
                if member.bot:
                    continue
                for activity in member.activities:
                    if isinstance(activity, discord.Game) and "star citizen" in activity.name.lower():
                        count += 1
                        break
                    if isinstance(activity, discord.Activity) and "star citizen" in activity.name.lower():
                        count += 1
                        break
            try:
                new_name = f"🚀 Star Citizen: {count} Spieler"
                if channel.name != new_name:
                    await channel.edit(name=new_name)
            except:
                pass

async def giveaway_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(10)
        now = datetime.datetime.utcnow().timestamp()
        for guild in bot.guilds:
            gid = str(guild.id)
            for gw in list(giveaways[gid]):
                if gw.get("ended"):
                    continue
                if now >= gw["end_time"]:
                    gw["ended"] = True
                    ch = guild.get_channel(gw["channel_id"])
                    if not ch:
                        continue
                    participants = gw.get("participants", [])
                    winner_count = gw.get("winners", 1)
                    if not participants:
                        try:
                            await ch.send(f"🎁 Das Giveaway für **{gw['prize']}** ist beendet. Leider hat niemand teilgenommen.")
                        except:
                            pass
                        continue
                    winners = random.sample(participants, min(winner_count, len(participants)))
                    winner_mentions = " ".join([f"<@{w}>" for w in winners])
                    embed = discord.Embed(
                        title="🎉 Giveaway beendet!",
                        description=f"**Preis:** {gw['prize']}\n**Gewinner:** {winner_mentions}",
                        color=discord.Color.gold()
                    )
                    try:
                        await ch.send(embed=embed)
                    except:
                        pass

# ══════════════════════════════════════════════════════════════════════════════
# HELP COMMAND
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="help", description="Shows all commands and features of the bot.")
async def hilfe(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 TITAN Bot — All Commands",
        description="Here is a complete overview of all available commands.",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="🔊 Temporary Voice Channels",
        value=(
            "`/rename [name]` — Channel umbenennen\n"
            "`/limit [zahl]` — Nutzerlimit setzen (0 = unbegrenzt)\n"
            "`/lock` — Channel für andere sperren\n"
            "`/unlock` — Channel entsperren\n"
            "`/kick [@user]` — Nutzer rauswerfen\n"
            "`/ban [@user]` — Nutzer aus Channel bannen\n"
            "`/invite [@user]` — Nutzer in gesperrten Channel einladen\n"
            "`/transfer [@user]` — Channel-Besitz übertragen\n"
            "`/cleanup` — Leere Channels aufräumen (Admin)"
        ),
        inline=False
    )
    embed.add_field(
        name="🛡️ Moderation",
        value=(
            "`/warn [@user] [grund]` — Nutzer verwarnen\n"
            "`/warns [@user]` — Verwarnungen anzeigen\n"
            "`/clearwarns [@user]` — Verwarnungen löschen (Admin)\n"
            "`/mute [@user] [min] [grund]` — Nutzer muten\n"
            "`/unmute [@user]` — Mute aufheben\n"
            "`/tempban [@user] [tage] [grund]` — Temporärer Ban"
        ),
        inline=False
    )
    embed.add_field(
        name="📊 Level & XP System",
        value=(
            "`/level [@user]` — Level & XP anzeigen\n"
            "`/leaderboard` — Top 10 Rangliste\n"
            "`/setlevelrole [level] [@rolle]` — Rolle für Level vergeben (Admin)\n"
            "💡 XP bekommst du durch Nachrichten & Zeit im Voice Channel"
        ),
        inline=False
    )
    embed.add_field(
        name="🎵 Music",
        value=(
            "`/play [suche]` — Song abspielen/zur Queue hinzufügen\n"
            "`/skip` — Aktuellen Song überspringen\n"
            "`/queue` — Aktuelle Warteschlange anzeigen\n"
            "`/stop` — Musik stoppen & Channel verlassen\n"
            "`/pause` — Musik pausieren\n"
            "`/resume` — Musik fortsetzen"
        ),
        inline=False
    )
    embed.add_field(
        name="🎁 Giveaways",
        value=(
            "`/giveaway [preis] [minuten] [gewinner]` — Giveaway starten (Admin)\n"
            "`/giveaway-stop [preis]` — Giveaway vorzeitig beenden (Admin)\n"
            "💡 Mit 🎉 reagieren um teilzunehmen"
        ),
        inline=False
    )
    embed.add_field(
        name="🌍 Translation",
        value=(
            "`/übersetze [text] [sprache]` — Text übersetzen\n"
            "💡 Sprachen: de, en, fr, es, it, pl, ru, tr, ja, zh"
        ),
        inline=False
    )
    embed.add_field(
        name="🎮 Fun",
        value=(
            "`/würfel [seiten]` — Würfel werfen\n"
            "`/münze` — Münze werfen\n"
            "`/poll [frage] [opt1] [opt2]` — Abstimmung erstellen\n"
            "`/remind [min] [nachricht]` — Erinnerung setzen"
        ),
        inline=False
    )
    embed.add_field(
        name="ℹ️ Info",
        value=(
            "`/userinfo [@user]` — Nutzer-Infos\n"
            "`/serverinfo` — Server-Infos\n"
            "`/hilfe` — Diese Übersicht"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Admin Setup",
        value=(
            "`/setup [channel] [kategorie]` — TempVC einrichten\n"
            "`/setwelcome [channel] [nachricht]` — Willkommensnachricht\n"
            "`/setlog [channel]` — Log-Channel setzen\n"
            "`/setautorole [@rolle]` — Auto-Rolle für neue Mitglieder"
        ),
        inline=False
    )
    embed.set_footer(text="TITAN Bot • All commands are slash commands (/)")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# VOICE CHANNEL COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="rename", description="Rename your voice channel.")
@app_commands.describe(name="New name")
async def rename(interaction: discord.Interaction, name: str):
    channel, _, err = get_temp_channel(interaction)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await channel.edit(name=name)
    await interaction.response.send_message(f"✅ Channel renamed to **{name}**.", ephemeral=True)

@bot.tree.command(name="limit", description="Set a user limit (0 = unlimited).")
@app_commands.describe(anzahl="Maximum user count")
async def limit(interaction: discord.Interaction, amount: int):
    channel, _, err = get_temp_channel(interaction)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await channel.edit(user_limit=max(0, min(anzahl, 99)))
    msg = f"✅ Limit set to **{anzahl}**." if anzahl > 0 else "✅ Limit removed."
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="lock", description="Lock your channel.")
async def lock(interaction: discord.Interaction):
    channel, _, err = get_temp_channel(interaction)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await channel.set_permissions(interaction.guild.default_role, connect=False)
    await interaction.response.send_message("🔒 Channel locked.", ephemeral=True)

@bot.tree.command(name="unlock", description="Unlock your channel.")
async def unlock(interaction: discord.Interaction):
    channel, _, err = get_temp_channel(interaction)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await channel.set_permissions(interaction.guild.default_role, connect=True)
    await interaction.response.send_message("🔓 Channel unlocked.", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a user from your channel.")
@app_commands.describe(user="User")
async def kick_vc(interaction: discord.Interaction, user: discord.Member):
    channel, _, err = get_temp_channel(interaction)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if user.voice and user.voice.channel == channel:
        await user.move_to(None)
        await interaction.response.send_message(f"👢 **{user.display_name}** was kicked.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ **{user.display_name}** is not in your channel.", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a user from your channel.")
@app_commands.describe(user="User")
async def ban_vc(interaction: discord.Interaction, user: discord.Member):
    channel, _, err = get_temp_channel(interaction)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await channel.set_permissions(user, connect=False, speak=False)
    if user.voice and user.voice.channel == channel:
        await user.move_to(None)
    await interaction.response.send_message(f"🚫 **{user.display_name}** was banned.", ephemeral=True)

@bot.tree.command(name="invite", description="Invite a user to your channel.")
@app_commands.describe(user="User")
async def invite_vc(interaction: discord.Interaction, user: discord.Member):
    channel, _, err = get_temp_channel(interaction)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await channel.set_permissions(user, connect=True)
    await interaction.response.send_message(f"✅ **{user.display_name}** was invited.", ephemeral=True)
    try:
        await user.send(f"📨 **{interaction.user.display_name}** hat dich in **{channel.name}** auf **{interaction.guild.name}** eingeladen!")
    except:
        pass

@bot.tree.command(name="transfer", description="Transfer channel ownership.")
@app_commands.describe(user="New owner")
async def transfer(interaction: discord.Interaction, user: discord.Member):
    channel, guild_cfg, err = get_temp_channel(interaction)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    temp_channels = guild_cfg.get("temp_channels", {})
    temp_channels[str(channel.id)] = user.id
    await channel.set_permissions(interaction.user, manage_channels=False, move_members=False, mute_members=False)
    await channel.set_permissions(user, manage_channels=True, move_members=True, mute_members=True, connect=True, speak=True)
    save_guild_config(interaction.guild.id, guild_cfg)
    await interaction.response.send_message(f"✅ Ownership transferred to **{user.display_name}**.", ephemeral=True)

@bot.tree.command(name="cleanup", description="Delete all empty temp channels (Admin).")
@app_commands.checks.has_permissions(administrator=True)
async def cleanup(interaction: discord.Interaction):
    guild_cfg = get_guild_config(interaction.guild.id)
    temp_channels = guild_cfg.get("temp_channels", {})
    deleted = 0
    to_remove = []
    for ch_id in list(temp_channels.keys()):
        ch = interaction.guild.get_channel(int(ch_id))
        if ch is None or len(ch.members) == 0:
            if ch:
                await ch.delete(reason="Cleanup")
            to_remove.append(ch_id)
            deleted += 1
    for ch_id in to_remove:
        del temp_channels[ch_id]
    guild_cfg["temp_channels"] = temp_channels
    save_guild_config(interaction.guild.id, guild_cfg)
    await interaction.response.send_message(f"🧹 {deleted} empty channel(s) deleted.", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# MODERATION
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="warn", description="Warn a user.")
@app_commands.describe(user="User", grund="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    gid = str(interaction.guild.id)
    uid = str(user.id)
    warns_data[gid][uid].append({"reason": reason, "time": str(datetime.datetime.utcnow())})
    count = len(warns_data[gid][uid])
    await interaction.response.send_message(f"⚠️ **{user.display_name}** warned. ({count}x)\nReason: {reason}", ephemeral=True)
    try:
        await user.send(f"⚠️ You were warned on **{interaction.guild.name}**.\nReason: **{reason}**")
    except:
        pass
    await send_log(interaction.guild, f"⚠️ **{user}** von **{interaction.user}** verwarnt. Reason: {reason}", discord.Color.yellow())

@bot.tree.command(name="warns", description="Show warnings of a user.")
@app_commands.describe(user="User")
@app_commands.checks.has_permissions(moderate_members=True)
async def warns(interaction: discord.Interaction, user: discord.Member):
    gid = str(interaction.guild.id)
    uid = str(user.id)
    user_warns = warns_data[gid][uid]
    if not user_warns:
        return await interaction.response.send_message(f"✅ **{user.display_name}** has no warnings.", ephemeral=True)
    embed = discord.Embed(title=f"⚠️ Warnings: {user.display_name}", color=discord.Color.yellow())
    for i, w in enumerate(user_warns, 1):
        embed.add_field(name=f"#{i}", value=f"**Grund:** {w['reason']}\n**Zeit:** {w['time'][:16]}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clearwarns", description="Clear all warnings of a user.")
@app_commands.describe(user="User")
@app_commands.checks.has_permissions(administrator=True)
async def clearwarns(interaction: discord.Interaction, user: discord.Member):
    warns_data[str(interaction.guild.id)][str(user.id)] = []
    await interaction.response.send_message(f"✅ Warnings of **{user.display_name}** cleared.", ephemeral=True)

@bot.tree.command(name="mute", description="Mute a user for X minutes.")
@app_commands.describe(user="User", minuten="Duration in minutes", grund="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason provided"):
    duration = datetime.timedelta(minutes=minuten)
    await user.timeout(duration, reason=grund)
    await interaction.response.send_message(f"🔇 **{user.display_name}** muted for **{minuten} minutes**.\nReason: {grund}", ephemeral=True)
    await send_log(interaction.guild, f"🔇 **{user}** von **{interaction.user}** für {minuten} Min. gemutet. Reason: {reason}", discord.Color.orange())

@bot.tree.command(name="unmute", description="Unmute a user.")
@app_commands.describe(user="User")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    await user.timeout(None)
    await interaction.response.send_message(f"🔊 **{user.display_name}** was unmuted.", ephemeral=True)

@bot.tree.command(name="tempban", description="Ban a user for X days.")
@app_commands.describe(user="User", tage="Duration in days", grund="Reason")
@app_commands.checks.has_permissions(ban_members=True)
async def tempban(interaction: discord.Interaction, user: discord.Member, days: int, reason: str = "No reason provided"):
    await interaction.response.defer(ephemeral=True)
    try:
        await user.send(f"🚫 Du wurdest von **{interaction.guild.name}** für **{tage} Tag(e)** gebannt.\nGrund: **{grund}**")
    except:
        pass
    await user.ban(reason=f"{grund} (Tempban: {tage} Tage)")
    await interaction.followup.send(f"🚫 **{user.display_name}** banned for **{tage} day(s)**.", ephemeral=True)
    await send_log(interaction.guild, f"🚫 **{user}** von **{interaction.user}** für {tage} Tag(e) gebannt. Reason: {reason}", discord.Color.red())
    await asyncio.sleep(tage * 86400)
    try:
        await interaction.guild.unban(user, reason="Tempban abgelaufen")
    except:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# LEVEL SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="level", description="Show your level and XP.")
@app_commands.describe(user="User (optional)")
async def level(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    gid = str(interaction.guild.id)
    uid = str(user.id)
    data = xp_data[gid][uid]
    lvl = data["level"]
    xp = data["xp"]
    needed = xp_for_level(lvl)
    bar_filled = int((xp / needed) * 20)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    embed = discord.Embed(title=f"📊 {user.display_name}", color=discord.Color.blurple())
    embed.add_field(name="Level", value=str(lvl), inline=True)
    embed.add_field(name="XP", value=f"{xp}/{needed}", inline=True)
    embed.add_field(name="Progress", value=f"`{bar}`", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Show the top 10 XP leaderboard.")
async def leaderboard(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    data = xp_data[gid]
    sorted_users = sorted(data.items(), key=lambda x: (x[1]["level"], x[1]["xp"]), reverse=True)[:10]
    embed = discord.Embed(title="🏆 XP Leaderboard", color=discord.Color.gold())
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, d) in enumerate(sorted_users):
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"Unbekannt"
        prefix = medals[i] if i < 3 else f"**#{i+1}**"
        embed.add_field(name=f"{prefix} {name}", value=f"Level {d['level']} • {d['xp']} XP", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setlevelrole", description="Assign a role to a level (Admin).")
@app_commands.describe(level_num="Level", role="Rolle")
@app_commands.checks.has_permissions(administrator=True)
async def setlevelrole(interaction: discord.Interaction, level_num: int, role: discord.Role):
    cfg = get_guild_config(interaction.guild.id)
    level_roles = cfg.get("level_roles", {})
    level_roles[str(level_num)] = role.id
    cfg["level_roles"] = level_roles
    save_guild_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ Level **{level_num}** → Rolle **{role.name}**.", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# MUSIK BOT
# ══════════════════════════════════════════════════════════════════════════════

def search_youtube(query: str):
    """Sucht auf YouTube und gibt Titel + URL zurück (ohne yt-dlp)."""
    try:
        query_enc = urllib.parse.quote(query)
        url = f"https://www.youtube.com/results?search_query={query_enc}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            html = r.read().decode()
        import re
        video_ids = re.findall(r'"videoId":"([^"]{11})"', html)
        titles = re.findall(r'"title":{"runs":\[{"text":"([^"]+)"', html)
        if video_ids and titles:
            return titles[0], f"https://www.youtube.com/watch?v={video_ids[0]}"
    except:
        pass
    return None, None

@bot.tree.command(name="play", description="Play a song (YouTube search).")
@app_commands.describe(suche="Song name or YouTube link")
async def play(interaction: discord.Interaction, search: str):
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ You must be in a voice channel.", ephemeral=True)

    try:
        import yt_dlp
        ydl_available = True
    except ImportError:
        ydl_available = False

    voice_channel = interaction.user.voice.channel
    gid = str(interaction.guild.id)

    if ydl_available:
        ydl_opts = {"format": "bestaudio/best", "quiet": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                if "youtube.com" in suche or "youtu.be" in suche:
                    info = ydl.extract_info(suche, download=False)
                else:
                    info = ydl.extract_info(f"ytsearch:{suche}", download=False)
                    info = info["entries"][0]
                title = info.get("title", suche)
                audio_url = info["url"]
            except Exception as e:
                return await interaction.followup.send(f"❌ Song nicht gefunden: {e}", ephemeral=True)

        music_queues[gid].append({"title": title, "url": audio_url})

        vc = interaction.guild.voice_client
        if not vc:
            vc = await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)

        if not vc.is_playing():
            await play_next(interaction.guild)
            await interaction.followup.send(f"🎵 Now playing: **{title}**")
        else:
            await interaction.followup.send(f"➕ Added to queue: **{title}** (Position {len(music_queues[gid])})")
    else:
        title, yt_url = search_youtube(suche)
        if title:
            embed = discord.Embed(
                title="🎵 Musik-Feature",
                description=f"Gefunden: **{title}**\n{yt_url}\n\n⚠️ Für echte Musikwiedergabe muss `yt-dlp` und `ffmpeg` auf dem Server installiert werden:\n```\npip3 install yt-dlp --break-system-packages\nsudo apt install ffmpeg -y\n```",
                color=discord.Color.orange()
            )
        else:
            embed = discord.Embed(
                title="🎵 Musik-Feature",
                description="⚠️ Für Musikwiedergabe wird `yt-dlp` und `ffmpeg` benötigt:\n```\npip3 install yt-dlp --break-system-packages\nsudo apt install ffmpeg -y\n```\nDanach den Bot neu starten.",
                color=discord.Color.orange()
            )
        await interaction.followup.send(embed=embed)

async def play_next(guild: discord.Guild):
    gid = str(guild.id)
    if not music_queues[gid]:
        vc = guild.voice_client
        if vc:
            await vc.disconnect()
        return
    song = music_queues[gid].pop(0)
    vc = guild.voice_client
    if not vc:
        return
    try:
        import yt_dlp
        FFMPEG_OPTIONS = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5", "options": "-vn"}
        source = discord.FFmpegPCMAudio(song["url"], **FFMPEG_OPTIONS)
        vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop))
    except Exception as e:
        print(f"Musik-Fehler: {e}")

@bot.tree.command(name="skip", description="Skip the current song.")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Song skipped.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

@bot.tree.command(name="stop", description="Stop the music.")
async def stop(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    music_queues[gid].clear()
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
    await interaction.response.send_message("⏹️ Music stopped.", ephemeral=True)

@bot.tree.command(name="pause", description="Pause the music.")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Music paused.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Nothing is playing right now.", ephemeral=True)

@bot.tree.command(name="resume", description="Resume the music.")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Music resumed.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Music is not paused.", ephemeral=True)

@bot.tree.command(name="queue", description="Show the music queue.")
async def queue(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    q = music_queues[gid]
    if not q:
        return await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blurple())
    for i, song in enumerate(q[:10], 1):
        embed.add_field(name=f"#{i}", value=song["title"], inline=False)
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# GIVEAWAY SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="giveaway", description="Start a giveaway.")
@app_commands.describe(preis="What is being given away?", minuten="Duration in minutes", gewinner="Number of winners")
@app_commands.checks.has_permissions(administrator=True)
async def giveaway(interaction: discord.Interaction, prize: str, minutes: int, gewinner: int = 1):
    end_time = datetime.datetime.utcnow().timestamp() + (minuten * 60)
    end_dt = datetime.datetime.utcfromtimestamp(end_time)

    embed = discord.Embed(
        title="🎁 GIVEAWAY!",
        description=(
            f"**Preis:** {preis}\n\n"
            f"Reagiere mit 🎉 um teilzunehmen!\n\n"
            f"**Endet:** {end_dt.strftime('%d.%m.%Y um %H:%M')} UTC\n"
            f"**Gewinner:** {gewinner}"
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Started by {interaction.user.display_name}")

    await interaction.response.send_message("✅ Giveaway started!", ephemeral=True)
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction("🎉")

    gid = str(interaction.guild.id)
    giveaways[gid].append({
        "message_id": msg.id,
        "channel_id": interaction.channel.id,
        "prize": preis,
        "end_time": end_time,
        "winners": gewinner,
        "participants": [],
        "ended": False
    })

@bot.tree.command(name="giveaway-end", description="End a giveaway early.")
@app_commands.describe(preis="Giveaway prize")
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_stop(interaction: discord.Interaction, prize: str):
    gid = str(interaction.guild.id)
    for gw in giveaways[gid]:
        if gw["prize"] == preis and not gw.get("ended"):
            gw["end_time"] = 0
            await interaction.response.send_message(f"✅ Giveaway for **{preis}** is being ended.", ephemeral=True)
            return
    await interaction.response.send_message("❌ No active giveaway found with this prize.", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# ÜBERSETZUNG
# ══════════════════════════════════════════════════════════════════════════════

SPRACHEN = {
    "de": "Deutsch", "en": "Englisch", "fr": "Französisch",
    "es": "Spanisch", "it": "Italienisch", "pl": "Polnisch",
    "ru": "Russisch", "tr": "Türkisch", "ja": "Japanisch", "zh": "Chinesisch"
}

@bot.tree.command(name="translate", description="Translate text into another language.")
@app_commands.describe(text="The text to translate", sprache="Target language (e.g. en, de, fr, es, it, pl, ru, tr, ja, zh)")
async def uebersetze(interaction: discord.Interaction, text: str, language: str):
    await interaction.response.defer()
    sprache = sprache.lower()
    if sprache not in SPRACHEN:
        return await interaction.followup.send(
            f"❌ Unbekannte Sprache. Verfügbar: {', '.join(SPRACHEN.keys())}", ephemeral=True
        )
    try:
        params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": sprache, "dt": "t", "q": text})
        url = f"https://translate.googleapis.com/translate_a/single?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        translated = "".join([s[0] for s in data[0] if s[0]])
        embed = discord.Embed(title="🌍 Translation", color=discord.Color.blurple())
        embed.add_field(name="Original", value=text[:1024], inline=False)
        embed.add_field(name=f"Translation ({SPRACHEN[sprache]})", value=translated[:1024], inline=False)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Übersetzung fehlgeschlagen: {e}", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# FUN
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="dice", description="Roll a dice.")
@app_commands.describe(seiten="Number of sides (default: 6)")
async def wuerfel(interaction: discord.Interaction, sides: int = 6):
    result = random.randint(1, max(2, seiten))
    await interaction.response.send_message(f"🎲 **{interaction.user.display_name}** rolls a D{seiten}: **{result}**!")

@bot.tree.command(name="coin", description="Flip a coin.")
async def muenze(interaction: discord.Interaction):
    result = random.choice(["Heads 👑", "Tails 🔢"])
    await interaction.response.send_message(f"🪙 **{result}!**")

@bot.tree.command(name="poll", description="Create a poll.")
@app_commands.describe(frage="The question", option1="Option 1", option2="Option 2", option3="Option 3 (optional)", option4="Option 4 (optional)")
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str, option3: str = None, option4: str = None):
    options = [option1, option2]
    if option3: options.append(option3)
    if option4: options.append(option4)
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    description = "\n".join([f"{emojis[i]} {opt}" for i, opt in enumerate(options)])
    embed = discord.Embed(title=f"📊 {frage}", description=description, color=discord.Color.blurple())
    embed.set_footer(text=f"Poll by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])

@bot.tree.command(name="remind", description="Set a reminder.")
@app_commands.describe(minuten="In how many minutes?", nachricht="What should I remind you of?")
async def remind(interaction: discord.Interaction, minutes: int, message: str):
    await interaction.response.send_message(f"⏰ I will remind you in **{minuten} minutes** about: **{nachricht}**", ephemeral=True)
    await asyncio.sleep(minuten * 60)
    try:
        await interaction.user.send(f"⏰ Reminder: **{nachricht}**")
    except:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# INFO
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="userinfo", description="Show info about a user.")
@app_commands.describe(user="User (optional)")
async def userinfo(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"👤 {user}", color=user.color)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Joined", value=user.joined_at.strftime("%d.%m.%Y"), inline=True)
    embed.add_field(name="Account created", value=user.created_at.strftime("%d.%m.%Y"), inline=True)
    roles = [r.mention for r in user.roles if r != interaction.guild.default_role]
    embed.add_field(name=f"Rollen ({len(roles)})", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="serverinfo", description="Show server info.")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=f"🏠 {g.name}", color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Members", value=str(g.member_count), inline=True)
    embed.add_field(name="Created", value=g.created_at.strftime("%d.%m.%Y"), inline=True)
    embed.add_field(name="Boost Level", value=str(g.premium_tier), inline=True)
    embed.add_field(name="Text Channels", value=str(len(g.text_channels)), inline=True)
    embed.add_field(name="Voice Channels", value=str(len(g.voice_channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN SETUP
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setup", description="Set up the TempVC system.")
@app_commands.describe(channel="Creator channel", category="Category for temp channels")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.VoiceChannel, category: discord.CategoryChannel):
    cfg = get_guild_config(interaction.guild.id)
    cfg["creator_channel_id"] = channel.id
    cfg["category_id"] = category.id
    cfg.setdefault("temp_channels", {})
    save_guild_config(interaction.guild.id, cfg)
    embed = discord.Embed(title="✅ TempVC Setup complete", color=discord.Color.green())
    embed.add_field(name="Creator channel", value=channel.mention, inline=True)
    embed.add_field(name="Category", value=category.name, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setwelcome", description="Set the welcome channel.")
@app_commands.describe(channel="Willkommens-Channel", nachricht="Message ({user} = mention)")
@app_commands.checks.has_permissions(administrator=True)
async def setwelcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Willkommen auf dem Server, {user}! 🎉"):
    cfg = get_guild_config(interaction.guild.id)
    cfg["welcome_channel_id"] = channel.id
    cfg["welcome_message"] = nachricht
    save_guild_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ Welcome channel set: {channel.mention}", ephemeral=True)

@bot.tree.command(name="setlog", description="Set the log channel.")
@app_commands.describe(channel="Log-Channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_guild_config(interaction.guild.id)
    cfg["log_channel_id"] = channel.id
    save_guild_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ Log channel set: {channel.mention}", ephemeral=True)

@bot.tree.command(name="setautorole", description="Set the auto role for new members.")
@app_commands.describe(role="Rolle")
@app_commands.checks.has_permissions(administrator=True)
async def setautorole(interaction: discord.Interaction, role: discord.Role):
    cfg = get_guild_config(interaction.guild.id)
    cfg["auto_role_id"] = role.id
    save_guild_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ Auto role set: **{role.name}**", ephemeral=True)

@bot.tree.command(name="setschannel", description="Set the Star Citizen player count channel.")
@app_commands.describe(channel="The voice channel to update")
@app_commands.checks.has_permissions(administrator=True)
async def setschannel(interaction: discord.Interaction, channel: discord.VoiceChannel):
    cfg = get_guild_config(interaction.guild.id)
    cfg["sc_channel_id"] = channel.id
    save_guild_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ Star Citizen channel set: {channel.mention}\nUpdates every 5 minutes.", ephemeral=True)

# ─── Error Handler ────────────────────────────────────────────────────────────

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You do not have permission for this command.", ephemeral=True)
    else:
        try:
            await interaction.response.send_message(f"❌ Fehler: {str(error)}", ephemeral=True)
        except:
            pass

# ─── Start ────────────────────────────────────────────────────────────────────

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("❌ DISCORD_TOKEN nicht gesetzt!")

bot.run(TOKEN)

# ══════════════════════════════════════════════════════════════════════════════
# REST API
# ══════════════════════════════════════════════════════════════════════════════

API_KEY = os.getenv("API_KEY", "geheimer-api-key")

def check_auth(request):
    key = request.headers.get("X-API-Key") or request.rel_url.query.get("key")
    return key == API_KEY

async def api_members(request):
    if not check_auth(request):
        return web.Response(status=401, text=json.dumps({"error": "Unauthorized"}), content_type="application/json")
    
    result = []
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            result.append({
                "id": str(member.id),
                "name": member.name,
                "display_name": member.display_name,
                "discriminator": member.discriminator,
                "avatar": str(member.display_avatar.url),
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "roles": [{"id": str(r.id), "name": r.name} for r in member.roles if r.name != "@everyone"],
                "guild": {"id": str(guild.id), "name": guild.name},
                "online": str(member.status) if member.status else "offline",
                "playing": next((a.name for a in member.activities if isinstance(a, (discord.Game, discord.Activity))), None)
            })
    
    return web.Response(
        text=json.dumps(result, ensure_ascii=False, indent=2),
        content_type="application/json"
    )

async def api_member(request):
    if not check_auth(request):
        return web.Response(status=401, text=json.dumps({"error": "Unauthorized"}), content_type="application/json")
    
    user_id = request.match_info.get("user_id")
    for guild in bot.guilds:
        member = guild.get_member(int(user_id))
        if member:
            data = {
                "id": str(member.id),
                "name": member.name,
                "display_name": member.display_name,
                "avatar": str(member.display_avatar.url),
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "roles": [{"id": str(r.id), "name": r.name} for r in member.roles if r.name != "@everyone"],
                "guild": {"id": str(guild.id), "name": guild.name},
                "online": str(member.status),
                "playing": next((a.name for a in member.activities if isinstance(a, (discord.Game, discord.Activity))), None)
            }
            return web.Response(text=json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json")
    
    return web.Response(status=404, text=json.dumps({"error": "Member not found"}), content_type="application/json")

async def api_roles(request):
    if not check_auth(request):
        return web.Response(status=401, text=json.dumps({"error": "Unauthorized"}), content_type="application/json")
    
    result = []
    for guild in bot.guilds:
        for role in guild.roles:
            if role.name == "@everyone":
                continue
            result.append({
                "id": str(role.id),
                "name": role.name,
                "color": str(role.color),
                "members": [str(m.id) for m in role.members],
                "guild": {"id": str(guild.id), "name": guild.name}
            })
    
    return web.Response(text=json.dumps(result, ensure_ascii=False, indent=2), content_type="application/json")

async def api_status(request):
    guilds = [{"id": str(g.id), "name": g.name, "members": g.member_count} for g in bot.guilds]
    data = {"status": "online", "bot": str(bot.user), "guilds": guilds}
    return web.Response(text=json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json")

async def start_api():
    await bot.wait_until_ready()
    app = web.Application()
    app.router.add_get("/", api_status)
    app.router.add_get("/members", api_members)
    app.router.add_get("/members/{user_id}", api_member)
    app.router.add_get("/roles", api_roles)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("🌐 REST API läuft auf Port 8080")
