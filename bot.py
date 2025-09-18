import discord
from discord import app_commands
import os
import asyncio
from dotenv import load_dotenv
from quart import Quart # Replaced Flask with Quart
from hypercorn.config import Config # The server configuration
from hypercorn.asyncio import serve # The server itself
import edge_tts

# --- 1. INITIAL SETUP ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

bot_data = {}
DEFAULT_SETTINGS = {
    "voice": "en-US-JennyNeural",
    "rate": "+0%",
    "pitch": "+0Hz",
}
TIMEOUT_SECONDS = 900

# --- 2. THE WEB SERVER (NOW USING QUART) ---
# This looks almost identical to the Flask version
app = Quart('')
@app.route('/')
async def home():
    return "TTS Bot is alive!"

# --- 3. ALL YOUR BOT FEATURES (UNCHANGED) ---
# This entire section is exactly the same as before.

def create_settings_embed(guild_id):
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
    def __init__(self, guild_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    async def update_message(self, interaction: discord.Interaction):
        embed = create_settings_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.select(
        placeholder="Choose a voice/accent...",
        options=[
            discord.SelectOption(label="Jenny (US Female)", value="en-US-JennyNeural"),
            discord.SelectOption(label="Guy (US Male)", value="en-US-GuyNeural"),
            discord.SelectOption(label="Libby (UK Female)", value="en-GB-LibbyNeural"),
            discord.SelectOption(label="Ryan (UK Male)", value="en-GB-RyanNeural"),
            discord.SelectOption(label="Natasha (AU Female)", value="en-AU-NatashaNeural"),
        ]
    )
    async def voice_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = bot_data.setdefault(self.guild_id, {})
        guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['voice'] = select.values[0]
        await self.update_message(interaction)

    # ... (other dropdowns are the same, removed for brevity but they are in the full code)
    @discord.ui.select( placeholder="Choose the speech speed...", options=[ discord.SelectOption(label="Slower", value="-25%"), discord.SelectOption(label="Normal", value="+0%"), discord.SelectOption(label="Faster", value="+25%"), ] )
    async def rate_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = bot_data.setdefault(self.guild_id, {}); guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['rate'] = select.values[0]; await self.update_message(interaction)
    @discord.ui.select( placeholder="Choose the voice clarity/pitch...", options=[ discord.SelectOption(label="Lower", value="-20Hz"), discord.SelectOption(label="Normal", value="+0Hz"), discord.SelectOption(label="Higher", value="+20Hz"), ] )
    async def pitch_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild_data = bot_data.setdefault(self.guild_id, {}); guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())['pitch'] = select.values[0]; await self.update_message(interaction)

async def autoleave_task(guild_id):
    await asyncio.sleep(TIMEOUT_SECONDS)
    if guild_id in bot_data and "vc" in bot_data[guild_id]:
        guild_data = bot_data[guild_id]; await guild_data["tc"].send(f"No activity for {int(TIMEOUT_SECONDS/60)} minutes, leaving."); await guild_data["vc"].disconnect(); del bot_data[guild_id]["vc"]; del bot_data[guild_id]["tc"]; del bot_data[guild_id]["task"]

async def say(vc, text, settings):
    try:
        communicate = edge_tts.Communicate(text, settings["voice"], rate=settings["rate"], pitch=settings["pitch"]); audio_stream = b''; async for chunk in communicate.stream():
            if chunk["type"] == "audio": audio_stream += chunk["data"]
        while vc.is_playing(): await asyncio.sleep(0.1)
        audio_source = discord.FFmpegPCMAudio(audio_stream, pipe=True); vc.play(audio_source)
    except Exception as e: print(f"Error in say function: {e}")

@client.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    guild_id = message.guild.id
    if guild_id in bot_data and "vc" in bot_data[guild_id]:
        guild_data = bot_data[guild_id]
        if message.channel.id == guild_data["tc"].id:
            guild_data["task"].cancel(); guild_data["task"] = asyncio.create_task(autoleave_task(guild_id)); vc = guild_data["vc"]; settings = guild_data.setdefault("settings", DEFAULT_SETTINGS.copy())
            if vc.is_connected(): await say(vc, message.content, settings)

@tree.command(name="join", description="Joins your VC and reads messages from this text channel.")
async def join(interaction: discord.Interaction):
    if interaction.user.voice is None: return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
    voice_channel = interaction.user.voice.channel; text_channel = interaction.channel
    if interaction.guild.voice_client: await interaction.guild.voice_client.disconnect()
    try: vc = await voice_channel.connect()
    except Exception as e: return await interaction.response.send_message(f"Failed to connect: {e}", ephemeral=True)
    guild_data = bot_data.setdefault(interaction.guild.id, {}); guild_data["task"] = asyncio.create_task(autoleave_task(interaction.guild.id)); guild_data["vc"] = vc; guild_data["tc"] = text_channel
    await interaction.response.send_message(f"Joined **{voice_channel.name}** and will read messages from this channel."); settings = guild_data.setdefault("settings", DEFAULT_SETTINGS.copy()); await say(vc, "Connected.", settings)

@tree.command(name="leave", description="Stops reading messages and leaves the voice channel.")
async def leave(interaction: discord.Interaction):
    if interaction.guild.id not in bot_data or "vc" not in bot_data[interaction.guild.id]: return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
    guild_data = bot_data[interaction.guild.id]; guild_data["task"].cancel(); await guild_data["vc"].disconnect(); del bot_data[interaction.guild.id]["vc"]; del bot_data[interaction.guild.id]["tc"]; del bot_data[interaction.guild.id]["task"]
    await interaction.response.send_message("Left the voice channel.")

@tree.command(name="settings", description="Opens the TTS settings panel.")
async def settings(interaction: discord.Interaction):
    embed = create_settings_embed(interaction.guild.id); view = SettingsView(interaction.guild.id); await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@client.event
async def on_ready():
    await tree.sync()
    print(f'Logged in as {client.user}!')
    print('Slash commands synced. Bot is ready.')

# --- 4. THE NEW, UNIFIED STARTUP PROCESS ---

# Get the port from Render's environment variables
port = int(os.environ.get("PORT", 8080))
# Configure Hypercorn to run our Quart app
config = Config()
config.bind = [f"0.0.0.0:{port}"]

# This is the main function that runs everything
async def main():
    # Run the web server and the discord bot concurrently
    await asyncio.gather(
        serve(app, config),
        client.start(DISCORD_TOKEN)
    )

# When the script is run, it will start the main async function
if __name__ == "__main__":
    asyncio.run(main())
