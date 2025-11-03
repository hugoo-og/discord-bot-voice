import os
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
import threading

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
@tree.command(name="join", description="Haz que el bot entre a un canal de voz")
@app_commands.describe(channel_id="ID del canal de voz (opcional)")
async def join(interaction: discord.Interaction, channel_id: str = None):
    await interaction.response.defer()  # ✅ sin ephemeral

    voice_channel = None

    if channel_id:
        voice_channel = interaction.guild.get_channel(int(channel_id))
    elif interaction.user.voice:
        voice_channel = interaction.user.voice.channel

    if not voice_channel:
        await interaction.followup.send("❌ No se encontró ningún canal de voz.")
        return

    try:
        await voice_channel.connect()
        await interaction.followup.send(f"✅ Conectado a **{voice_channel.name}**")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error al conectar: `{e}`")


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



@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot conectado como {bot.user}.")


bot.run(os.getenv("DISCORD_TOKEN"))
