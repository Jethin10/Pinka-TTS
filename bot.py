import discord
from discord import app_commands
import os
import asyncio
from dotenv import load_dotenv
from flask import Flask
from threading import Thread
import edge_tts

# --- 1. INITIAL SETUP ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# --- NEW: Server Settings & State Management ---
# This dictionary holds both the settings and the active state for each server.
# Using a database would be better for a large-scale bot.
bot_data = {}

# Default settings for any new server
DEFAULT_SETTINGS = {
    "voice": "en-US-JennyNeural",
    "rate": "+0%",   # Speed
    "pitch": "+0Hz",  # Clarity/Pitch
}

TIMEOUT_SECONDS = 900  # 15 minutes

# --- 2. THE INTERACTIVE SETTINGS PANEL (THE "DROPBOX" PART) ---

def create_settings_embed(guild_id):
    """Creates the embed that displays the current settings for a server."""
    # Get or create settings for the guild
    settings = bot_data.setdefault(guild_id, {}).setdefault("settings", DEFAULT_SETTINGS.copy())
    
    embed = discord.Embed(
        title="TTS Bot Settings",
        description="Adjust the voice, speed, and clarity for live reading.",
        color=discord.Color.purple()
    )
    embed.add_field(name="🗣️ Voice (Accent)", value=f"`{settings['voice']}`", inline=False)
    embed.add_field(name="⏩ Speed", value=f"`{settings['rate']}`", inline=True)
    embed.add_field(name="🎼 Clarity (Pitch)", value=f"`{settings['pitch']}`", inline=True)
    return embed

class SettingsView(discord.ui.View):
    """The View containing the dropdown menus for settings."""
    def __init__(self, guild_id):
        super().__init__(timeout=180) # View times out after 3 minutes
        self.guild_id = guild_id

    async def update_message(self, interaction: discord.Interaction):
        embed = create_settings_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.select(
        placeholder="Choose a voice/accent...",
        options=[
            discord.SelectOption(label="Jenny (US Female)", value="en-US-JennyNeural", description="Default, friendly voice."),
            discord.SelectOption(label="Guy (US Male)", value="en-US-GuyNeural", description="A popular male voice."),
            discord.SelectOption(label="Libby (UK Female)", value="en-GB-LibbyNeural", description="British accent."),
            discord.SelectOption(label="Ryan (UK Male)", value="en-GB-RyanNeural", description="British accent."),
            discord.SelectOption(label="Natasha (AU Female)", value="en-AU-NatashaNeural", description="Australian accent."),
        ]
    )
    async def voice_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = bot_data.setdefault(self.guild_id, {})
        guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['voice'] = select.values[0]
        await self.update_message(interaction)

    @discord.ui.select(
        placeholder="Choose the speech speed...",
        options=[
            discord.SelectOption(label="Slower", value="-25%"),
            discord.SelectOption(label="Normal", value="+0%"),
            discord.SelectOption(label="Faster", value="+25%"),
        ]
    )
    async def rate_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = bot_data.setdefault(self.guild_id, {})
        guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['rate'] = select.values[0]
        await self.update_message(interaction)

    @discord.ui.select(
        placeholder="Choose the voice clarity/pitch...",
        options=[
            discord.SelectOption(label="Lower", value="-20Hz"),
            discord.SelectOption(label="Normal", value="+0Hz"),
            discord.SelectOption(label="Higher", value="+20Hz"),
        ]
    )
    async def pitch_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = bot_data.setdefault(self.guild_id, {})
        guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['pitch'] = select.values[0]
        await self.update_message(interaction)


# --- 3. THE "ALWAYS ON" WEB SERVER (UNCHANGED) ---
app = Flask('')
@app.route('/')
def home():
    return "TTS Bot is alive!"
def run():
    app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run)
    t.start()

# --- 4. CORE BOT LOGIC ---

async def autoleave_task(guild_id):
    """The timer that waits for 15 minutes of inactivity."""
    await asyncio.sleep(TIMEOUT_SECONDS)
    
    if guild_id in bot_data and "vc" in bot_data[guild_id]:
        guild_data = bot_data[guild_id]
        await guild_data["tc"].send(f"No activity for {int(TIMEOUT_SECONDS/60)} minutes, leaving voice channel.")
        await guild_data["vc"].disconnect()
        del bot_data[guild_id]["vc"]
        del bot_data[guild_id]["tc"]
        del bot_data[guild_id]["task"]

async def say(vc, text, settings):
    """Generates and plays audio using edge-tts with the server's settings."""
    try:
        communicate = edge_tts.Communicate(text, settings["voice"], rate=settings["rate"], pitch=settings["pitch"])
        
        audio_stream = b''
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_stream += chunk["data"]
        
        while vc.is_playing():
            await asyncio.sleep(0.1)

        audio_source = discord.FFmpegPCMAudio(audio_stream, pipe=True)
        vc.play(audio_source)
    except Exception as e:
        print(f"Error in say function: {e}")

@client.event
async def on_message(message):
    """The main event that listens for messages and reads them aloud."""
    if message.author.bot or not message.guild:
        return

    guild_id = message.guild.id
    if guild_id in bot_data and "vc" in bot_data[guild_id]:
        guild_data = bot_data[guild_id]
        
        if message.channel.id == guild_data["tc"].id:
            # Cancel and restart the autoleave timer
            guild_data["task"].cancel()
            guild_data["task"] = asyncio.create_task(autoleave_task(guild_id))
            
            # Read the message using the saved settings
            vc = guild_data["vc"]
            settings = guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())
            if vc.is_connected():
                await say(vc, message.content, settings)

# --- 5. SLASH COMMANDS ---

@tree.command(name="join", description="Joins your VC and reads messages from this text channel.")
async def join(interaction: discord.Interaction):
    if interaction.user.voice is None:
        return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)

    voice_channel = interaction.user.voice.channel
    text_channel = interaction.channel

    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()

    try:
        vc = await voice_channel.connect()
    except Exception as e:
        return await interaction.response.send_message(f"Failed to connect: {e}", ephemeral=True)

    guild_data = bot_data.setdefault(interaction.guild.id, {})
    guild_data["task"] = asyncio.create_task(autoleave_task(interaction.guild.id))
    guild_data["vc"] = vc
    guild_data["tc"] = text_channel
    
    await interaction.response.send_message(f"Joined **{voice_channel.name}** and will read messages from this channel.")
    
    settings = guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())
    await say(vc, "Connected.", settings)

@tree.command(name="leave", description="Stops reading messages and leaves the voice channel.")
async def leave(interaction: discord.Interaction):
    if interaction.guild.id not in bot_data or "vc" not in bot_data[interaction.guild.id]:
        return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)

    guild_data = bot_data[interaction.guild.id]
    guild_data["task"].cancel()
    await guild_data["vc"].disconnect()
    
    del bot_data[interaction.guild.id]["vc"]
    del bot_data[interaction.guild.id]["tc"]
    del bot_data[interaction.guild.id]["task"]
    
    await interaction.response.send_message("Left the voice channel.")

@tree.command(name="settings", description="Opens the TTS settings panel.")
async def settings(interaction: discord.Interaction):
    embed = create_settings_embed(interaction.guild.id)
    view = SettingsView(interaction.guild.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# --- 6. BOT STARTUP ---
@client.event
async def on_ready():
    await tree.sync()
    print(f'Logged in as {client.user}!')
    print('Slash commands synced. Bot is ready.')

keep_alive()
client.run(DISCORD_TOKEN)