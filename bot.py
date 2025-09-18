import discord
from discord import app_commands
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

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# We only store the text channel and the timeout task now.
# The voice client (vc) will be fetched directly from discord.py when needed.
active_guilds = {}
DEFAULT_SETTINGS = {
    "voice": "en-US-JennyNeural",
    "rate": "+0%",
    "pitch": "+0Hz",
}
TIMEOUT_SECONDS = 900

# --- 2. THE WEB SERVER (USING QUART) ---
app = Quart('')
@app.route('/')
async def home():
    return "TTS Bot is alive!"

# --- 3. BOT FEATURES (SETTINGS PART IS UNCHANGED) ---

def create_settings_embed(guild_id):
    settings = active_guilds.setdefault(guild_id, {}).setdefault("settings", DEFAULT_SETTINGS.copy())
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
    # This class is unchanged
    def __init__(self, guild_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
    async def update_message(self, interaction: discord.Interaction):
        embed = create_settings_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)
    @discord.ui.select( placeholder="Choose a voice/accent...", options=[ discord.SelectOption(label="Jenny (US Female)", value="en-US-JennyNeural"), discord.SelectOption(label="Guy (US Male)", value="en-US-GuyNeural"), discord.SelectOption(label="Libby (UK Female)", value="en-GB-LibbyNeural"), discord.SelectOption(label="Ryan (UK Male)", value="en-GB-RyanNeural"), discord.SelectOption(label="Natasha (AU Female)", value="en-AU-NatashaNeural"), ] )
    async def voice_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = active_guilds.setdefault(self.guild_id, {}); guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['voice'] = select.values[0]; await self.update_message(interaction)
    @discord.ui.select( placeholder="Choose the speech speed...", options=[ discord.SelectOption(label="Slower", value="-25%"), discord.SelectOption(label="Normal", value="+0%"), discord.SelectOption(label="Faster", value="+25%"), ] )
    async def rate_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = active_guilds.setdefault(self.guild_id, {}); guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['rate'] = select.values[0]; await self.update_message(interaction)
    @discord.ui.select( placeholder="Choose the voice clarity/pitch...", options=[ discord.SelectOption(label="Lower", value="-20Hz"), discord.SelectOption(label="Normal", value="+0Hz"), discord.SelectOption(label="Higher", value="+20Hz"), ] )
    async def pitch_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = active_guilds.setdefault(self.guild_id, {}); guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['pitch'] = select.values[0]; await self.update_message(interaction)

# --- 4. CORE BOT LOGIC (REWRITTEN FOR RELIABILITY) ---

async def autoleave_task(guild_id):
    """Waits for inactivity and then leaves."""
    await asyncio.sleep(TIMEOUT_SECONDS)
    
    # Check if the bot is still supposed to be active in this guild
    if guild_id in active_guilds:
        guild = client.get_guild(guild_id)
        if guild and guild.voice_client:
            await active_guilds[guild_id]["tc"].send(f"No activity for {int(TIMEOUT_SECONDS/60)} minutes, leaving.")
            await guild.voice_client.disconnect()
        # Clean up our state regardless
        del active_guilds[guild_id]

async def say(vc, text, settings):
    """Generates and plays audio. This function is now more robust."""
    if not vc or not vc.is_connected():
        print("Say command called but not connected to a voice channel.")
        return
        
    try:
        communicate = edge_tts.Communicate(text, settings["voice"], rate=settings["rate"], pitch=settings["pitch"])
        audio_stream = b''
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_stream += chunk["data"]
        
        # This loop prevents messages from cutting each other off
        while vc.is_playing():
            await asyncio.sleep(0.1)
            
        audio_source = discord.FFmpegPCMAudio(audio_stream, pipe=True)
        vc.play(audio_source)
    except Exception as e:
        print(f"Error in say function: {e}")

@client.event
async def on_message(message):
    """Listens for messages and reads them aloud."""
    if message.author.bot or not message.guild:
        return

    guild_id = message.guild.id
    # Check if this guild is marked as active in our state
    if guild_id in active_guilds:
        guild_info = active_guilds[guild_id]
        
        # Check if the message is in the correct text channel
        if message.channel.id == guild_info["tc"].id:
            # Get the current, live voice client from the guild
            vc = message.guild.voice_client
            if vc:
                # Cancel and restart the autoleave timer
                guild_info["task"].cancel()
                guild_info["task"] = asyncio.create_task(autoleave_task(guild_id))
                
                settings = guild_info.setdefault("settings", DEFAULT_SETTINGS.copy())
                await say(vc, message.content, settings)

@tree.command(name="join", description="Joins your VC and reads messages from this text channel.")
async def join(interaction: discord.Interaction):
    if interaction.user.voice is None:
        return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
    
    voice_channel = interaction.user.voice.channel
    
    # If already in a voice channel in this guild, move to the new one
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(voice_channel)
    else:
        await voice_channel.connect()

    # Get the freshly connected voice client (the source of truth)
    vc = interaction.guild.voice_client
    
    # Store our state, but WITHOUT the vc object
    if interaction.guild.id in active_guilds:
        active_guilds[interaction.guild.id]['task'].cancel() # Cancel old task if exists
    
    active_guilds[interaction.guild.id] = {
        "tc": interaction.channel,
        "task": asyncio.create_task(autoleave_task(interaction.guild.id)),
        "settings": active_guilds.get(interaction.guild.id, {}).get("settings", DEFAULT_SETTINGS.copy())
    }
    
    await interaction.response.send_message(f"Joined **{voice_channel.name}** and will read messages from this channel.")
    
    settings = active_guilds[interaction.guild.id]["settings"]
    await say(vc, "Connected.", settings)

@tree.command(name="leave", description="Stops reading messages and leaves the voice channel.")
async def leave(interaction: discord.Interaction):
    # Get the current voice client directly from the guild
    vc = interaction.guild.voice_client
    
    if not vc:
        return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
    
    # Disconnect the voice client
    await vc.disconnect()
    
    # Clean up our own state dictionary
    if interaction.guild.id in active_guilds:
        active_guilds[interaction.guild.id]['task'].cancel()
        del active_guilds[interaction.guild.id]
    
    await interaction.response.send_message("Left the voice channel.")

@tree.command(name="settings", description="Opens the TTS settings panel.")
async def settings(interaction: discord.Interaction):
    embed = create_settings_embed(interaction.guild.id)
    view = SettingsView(interaction.guild.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@client.event
async def on_ready():
    await tree.sync()
    print(f'Logged in as {client.user}!')
    print('Slash commands synced. Bot is ready.')

# --- 5. THE UNIFIED STARTUP PROCESS ---
port = int(os.environ.get("PORT", 8080))
config = Config()
config.bind = [f"0.0.0.0:{port}"]

async def main():
    await asyncio.gather(
        serve(app, config),
        client.start(DISCORD_TOKEN)
    )

if __name__ == "__main__":
    asyncio.run(main())
