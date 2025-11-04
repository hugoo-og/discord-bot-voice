import os
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify
import threading
from gtts import gTTS
import asyncio
import tempfile
import logging
import requests
import time

logging.basicConfig(level=logging.INFO)

# === Config ===
TOKEN = os.getenv("DISCORD_TOKEN")

# === Discord setup ===
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree  # slash commands

# === Minimal Flask (health endpoints only) ===
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot online", 200

@app.route("/status")
def status():
    return jsonify({"status": "ok", "guilds": len(bot.guilds)}), 200

def run_flask():
    # Flask en hilo para que no bloquee la loop de discord
    app.run(host="0.0.0.0", port=8080)

# arrancar Flask en hilo justo antes de bot.run (ver final)

# === Audio control ===
tts_lock = asyncio.Lock()  # evita solapamiento de /say

def play_audio(vc: discord.VoiceClient, source_path: str, after=None):
    """
    Reproduce un archivo con FFmpegPCMAudio en el VoiceClient.
    Usa vc.play(...) y maneja el callback after si se desea.
    """
    try:
        player = discord.FFmpegPCMAudio(source_path)
        vc.play(player, after=after)
    except Exception as e:
        logging.exception("Error al reproducir audio: %s", e)

# === Slash commands ===

@tree.command(name="join", description="Haz que el bot entre a un canal de voz (opcional: canal ID)")
@app_commands.describe(channel_id="ID del canal de voz (opcional)")
async def join(interaction: discord.Interaction, channel_id: str = None):
    await interaction.response.defer(ephemeral=True)
    try:
        if channel_id:
            channel = interaction.guild.get_channel(int(channel_id))
        else:
            channel = interaction.user.voice.channel if interaction.user.voice else None

        if not channel or channel.type != discord.ChannelType.voice:
            await interaction.followup.send("❌ Canal de voz no válido.", ephemeral=True)
            return

        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(channel)
            await interaction.followup.send(f"🔁 Movido a **{channel.name}**.", ephemeral=True)
        else:
            await channel.connect()
            await interaction.followup.send(f"✅ Conectado a **{channel.name}**.", ephemeral=True)
    except Exception as e:
        logging.exception("Error en /join")
        await interaction.followup.send(f"⚠️ Error al unir: `{e}`", ephemeral=True)

@tree.command(name="leave", description="Haz que el bot salga del canal de voz")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect()
            await interaction.followup.send("👋 Desconectado.", ephemeral=True)
        else:
            await interaction.followup.send("❌ No estoy en ningún canal.", ephemeral=True)
    except Exception as e:
        logging.exception("Error en /leave")
        await interaction.followup.send(f"⚠️ Error al salir: `{e}`", ephemeral=True)

@tree.command(name="say", description="El bot dice el texto en el canal de voz")
@app_commands.describe(texto="Texto a decir (máx 200 caracteres recomendado)")
async def say(interaction: discord.Interaction, texto: str):
    # limita longitud prudente
    if len(texto) > 1000:
        await interaction.response.send_message("❌ Texto demasiado largo.", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ El bot no está en ningún canal de voz. Usa /join antes.", ephemeral=True)
        return

    await interaction.response.send_message("📢 Generando audio...", ephemeral=True)

    # toma lock para que no haya dos TTS a la vez
    async with tts_lock:
        # genera archivo temporal con gTTS
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                tmp_path = tmp.name
            tts = gTTS(text=texto, lang="es")
            tts.save(tmp_path)
        except Exception as e:
            logging.exception("Error al generar TTS")
            await interaction.followup.send(f"⚠️ Error al generar TTS: `{e}`", ephemeral=True)
            # cleanup si existe
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except:
                pass
            return

        # Parar reproducción actual si existe
        try:
            if vc.is_playing():
                vc.stop()
        except Exception:
            logging.exception("Error al detener reproducción previa")

        # Reproducir y esperar fin (sin bloquear loop)
        finished = asyncio.Event()

        def after_playing(err):
            if err:
                logging.exception("Error en reproducción TTS: %s", err)
            # señalamos que ha terminado
            try:
                # programar en la loop principal la set() del evento
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(finished.set)
            except Exception:
                pass

        try:
            play_audio(vc, tmp_path, after=after_playing)
        except Exception:
            logging.exception("Error al iniciar reproducción")
            await interaction.followup.send("⚠️ No se pudo reproducir el audio.", ephemeral=True)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except:
                pass
            return

        # esperamos a que termine la reproducción
        await finished.wait()

        # cleanup archivo temporal
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            logging.exception("No se pudo borrar tmp TTS")

        await interaction.followup.send("✅ He terminado de hablar.", ephemeral=True)


# === Eventos ===
@bot.event
async def on_ready():
    # sincroniza slash commands (global o por guild según prefieras)
    try:
        await tree.sync()
        logging.info("Slash commands sincronizados.")
    except Exception:
        logging.exception("Error al sincronizar slash commands")
    logging.info(f"Bot conectado como {bot.user}.")


# === Keep Alive ===
def keep_alive():
    def ping():
        while True:
            try:
                requests.get("https://discord-bot-voice-cbpv.onrender.com")
            except:
                pass
            time.sleep(300)  # 5 minutos

    thread = threading.Thread(target=ping)
    thread.daemon = True
    thread.start()


# === Start server + bot ===
if __name__ == "__main__":
    # arrancar Flask en hilo (solo health endpoints)
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # arranca el bot
    bot.run(TOKEN)
