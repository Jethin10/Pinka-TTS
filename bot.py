import discord
# NO LONGER NEEDED: from discord import app_commands
import os
import asyncio
from dotenv import load_dotenv
from quart import Quart
from hypercorn.config import Config
from hypercorn.asyncio import serve
import edge_tts

# --- 1. INITIAL SETUP ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class LiveTTSBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # --- THE FIRST FIX ---
        # We now create the CommandTree directly from the discord object
        self.tree = discord.app_commands.CommandTree(self)
        self.active_guilds = {}
        self.DEFAULT_SETTINGS = {"voice": "en-US-JennyNeural", "rate": "+0%", "pitch": "+0Hz"}
        self.TIMEOUT_SECONDS = 900

    async def setup_hook(self) -> None:
        await self.tree.sync()
        port = int(os.environ.get("PORT", 8080))
        config = Config()
        config.bind = [f"0.0.0.0:{port}"]
        self.loop.create_task(serve(app, config))

    async def on_ready(self):
        print(f'Logged in as {self.user}!')
        print('Slash commands synced. Bot is ready.')

client = LiveTTSBot(intents=intents)
app = Quart('')

@app.route('/')
async def home():
    return "TTS Bot is alive!"

# --- SETTINGS UI (UNCHANGED) ---
def create_settings_embed(guild_id):
    settings = client.active_guilds.setdefault(guild_id, {}).setdefault("settings", client.DEFAULT_SETTINGS.copy())
    embed = discord.Embed(title="TTS Bot Settings", description="Adjust the voice, speed, and clarity for live reading.", color=discord.Color.purple())
    embed.add_field(name="🗣️ Voice (Accent)", value=f"`{settings['voice']}`", inline=False)
    embed.add_field(name="⏩ Speed", value=f"`{settings['rate']}`", inline=True)
    embed.add_field(name="🎼 Clarity (Pitch)", value=f"`{settings['pitch']}`", inline=True)
    return embed
class SettingsView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
    async def update_message(self, interaction: discord.Interaction):
        embed = create_settings_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)
    @discord.ui.select( placeholder="Choose a voice/accent...", options=[ discord.SelectOption(label="Jenny (US Female)", value="en-US-JennyNeural"), discord.SelectOption(label="Guy (US Male)", value="en-US-GuyNeural"), discord.SelectOption(label="Libby (UK Female)", value="en-GB-LibbyNeural"), discord.SelectOption(label="Ryan (UK Male)", value="en-GB-RyanNeural"), discord.SelectOption(label="Natasha (AU Female)", value="en-AU-NatashaNeural"), ] )
    async def voice_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = client.active_guilds.setdefault(self.guild_id, {}); guild_data.setdefault("settings", client.DEFAULT_SETTINGS.copy())['voice'] = select.values[0]; await self.update_message(interaction)
    @discord.ui.select( placeholder="Choose the speech speed...", options=[ discord.SelectOption(label="Slower", value="-25%"), discord.SelectOption(label="Normal", value="+0%"), discord.SelectOption(label="Faster", value="+25%"), ] )
    async def rate_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = client.active_guilds.setdefault(self.guild_id, {}); guild_data.setdefault("settings", client.DEFAULT_SETTINGS.copy())['rate'] = select.values[0]; await self.update_message(interaction)
    @discord.ui.select( placeholder="Choose the voice clarity/pitch...", options=[ discord.SelectOption(label="Lower", value="-20Hz"), discord.SelectOption(label="Normal", value="+0Hz"), discord.SelectOption(label="Higher", value="+20Hz"), ] )
    async def pitch_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = client.active_guilds.setdefault(self.guild_id, {}); guild_data.setdefault("settings", client.DEFAULT_SETTINGS.copy())['pitch'] = select.values[0]; await self.update_message(interaction)

# --- CORE LOGIC (UNCHANGED) ---
async def autoleave_task(guild_id):
    await asyncio.sleep(client.TIMEOUT_SECONDS)
    if guild_id in client.active_guilds:
        guild = client.get_guild(guild_id)
        if guild and guild.voice_client:
            await client.active_guilds[guild_id]["tc"].send(f"No activity for {int(client.TIMEOUT_SECONDS/60)} minutes, leaving.")
            await guild.voice_client.disconnect()
        if guild_id in client.active_guilds:
            del client.active_guilds[guild_id]
async def say(vc, text, settings):
    if not vc or not vc.is_connected(): return
    try:
        communicate = edge_tts.Communicate(text, settings["voice"], rate=settings["rate"], pitch=settings["pitch"])
        audio_stream = b''
        async for chunk in communicate.stream():
            if chunk["type"] == "audio": audio_stream += chunk["data"]
        while vc.is_playing(): await asyncio.sleep(0.1)
        audio_source = discord.FFmpegPCMAudio(audio_stream, pipe=True)
        vc.play(audio_source)
    except Exception as e: print(f"Error in say function: {e}")
@client.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    guild_id = message.guild.id
    if guild_id in client.active_guilds and "tc" in client.active_guilds[guild_id]:
        guild_info = client.active_guilds[guild_id]
        if message.channel.id == guild_info["tc"].id:
            vc = message.guild.voice_client
            if vc:
                guild_info["task"].cancel()
                guild_info["task"] = asyncio.create_task(autoleave_task(guild_id))
                settings = guild_info.setdefault("settings", client.DEFAULT_SETTINGS.copy())
                await say(vc, message.content, settings)

# --- COMMANDS (UNCHANGED) ---
@client.tree.command(name="join", description="Joins your VC and reads messages from this text channel.")
async def join(interaction: discord.Interaction):
    if interaction.user.voice is None: return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    voice_channel = interaction.user.voice.channel
    try:
        if interaction.guild.voice_client: await interaction.guild.voice_client.move_to(voice_channel)
        else: await voice_channel.connect()
    except asyncio.TimeoutError: return await interaction.followup.send("Could not connect to the voice channel in time.", ephemeral=True)
    except Exception as e: return await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)
    vc = interaction.guild.voice_client
    if interaction.guild.id in client.active_guilds and 'task' in client.active_guilds[interaction.guild.id]: client.active_guilds[interaction.guild.id]['task'].cancel()
    client.active_guilds[interaction.guild.id] = { "tc": interaction.channel, "task": asyncio.create_task(autoleave_task(interaction.guild.id)), "settings": client.active_guilds.get(interaction.guild.id, {}).get("settings", client.DEFAULT_SETTINGS.copy()) }
    await interaction.followup.send(f"Joined **{voice_channel.name}** and will read messages from this channel.", ephemeral=False)
    settings = client.active_guilds[interaction.guild.id]["settings"]
    await say(vc, "Connected.", settings)
@client.tree.command(name="leave", description="Stops reading messages and leaves the voice channel.")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    vc = interaction.guild.voice_client
    if not vc: return await interaction.followup.send("I'm not in a voice channel.", ephemeral=True)
    await vc.disconnect()
    if interaction.guild.id in client.active_guilds:
        client.active_guilds[interaction.guild.id]['task'].cancel()
        del client.active_guilds[interaction.guild.id]
    await interaction.followup.send("Left the voice channel.", ephemeral=True)
@client.tree.command(name="settings", description="Opens the TTS settings panel.")
async def settings(interaction: discord.Interaction):
    embed = create_settings_embed(interaction.guild.id)
    view = SettingsView(interaction.guild.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# --- FINAL STARTUP ---
client.run(DISCORD_TOKEN)
