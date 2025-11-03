import os
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
import threading
from gtts import gTTS
import asyncio

# === Discord setup ===
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree  # para slash commands


# === Flask keepalive ===
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot online and running!"

@app.route("/status")
def status():
    return {"status": "ok", "guilds": len(bot.guilds)}

def run_flask():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_flask).start()


# === Slash commands ===
@tree.command(name="join", description="El bot entra al canal de voz")
@app_commands.describe(channel_id="ID del canal de voz")
async def join(interaction: discord.Interaction, channel_id: str):

    await interaction.response.send_message("✅ Uniéndome al canal...", ephemeral=True)

    channel = interaction.guild.get_channel(int(channel_id))
    if not channel or channel.type != discord.ChannelType.voice:
        await interaction.followup.send("❌ Ese canal no es válido", ephemeral=True)
        return

    voice_client = interaction.guild.voice_client

    if voice_client and voice_client.is_connected():
        await voice_client.move_to(channel)
    else:
        voice_client = await channel.connect()

    # Reproducir silencio en loop
    silence = discord.FFmpegPCMAudio("silence.mp3")
    voice_client.play(silence, after=lambda e: restart_silence(voice_client))

def restart_silence(vc: discord.VoiceClient):
    if not vc.is_playing():
        silence = discord.FFmpegPCMAudio("silence.mp3")
        vc.play(silence, after=lambda e: restart_silence(vc))



@tree.command(name="leave", description="Haz que el bot salga del canal de voz")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer()

    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        try:
            await vc.disconnect()
            await interaction.followup.send("👋 Desconectado del canal.")
        except Exception as e:
            await interaction.followup.send(f"⚠️ Error al salir: `{e}`")
    else:
        await interaction.followup.send("❌ No estoy en ningún canal.")


@tree.command(name="say", description="El bot habla en el canal de voz")
@app_commands.describe(texto="Lo que quieres que diga el bot")
async def say(interaction: discord.Interaction, texto: str):

    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("❌ El bot no está en un canal de voz", ephemeral=True)
        return

    await interaction.response.send_message(f"📢 Hablando...", ephemeral=True)

    # Parar silencio
    if vc.is_playing():
        vc.stop()

    # Crear audio
    filename = "tts.mp3"
    tts = gTTS(text=texto, lang="es")
    tts.save(filename)

    # Reproducir voz
    vc.play(discord.FFmpegPCMAudio(filename))

    # Esperar a que termine
    while vc.is_playing():
        await asyncio.sleep(0.5)

    os.remove(filename)

    # Reanudar silencio infinito
    silence = discord.FFmpegPCMAudio("silence.mp3")
    vc.play(silence, after=lambda e: restart_silence(vc))



@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot conectado como {bot.user}.")


bot.run(os.getenv("DISCORD_TOKEN"))
