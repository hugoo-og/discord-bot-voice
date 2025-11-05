# bot.py — TTS-only behavior: join->speak->disconnect (if bot joined)
import os
import logging
import tempfile
import asyncio
import time
from typing import Optional
import threading
import requests

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify
from elevenlabs.client import ElevenLabs

# ----------------------
# Config + logging
# ----------------------
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_TOKEN")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
VOICE_ID = "Nh2zY9kknu6z4pZy6FhD"  # tu voice id elegido

if not TOKEN:
    LOG.error("DISCORD_TOKEN no configurado. Saliendo.")
    raise SystemExit(1)

if not ELEVEN_API_KEY:
    LOG.warning("ELEVEN_API_KEY no configurada — /say fallará hasta que la pongas.")

# Cliente ElevenLabs
client_el = ElevenLabs(api_key=ELEVEN_API_KEY)

# ----------------------
# Discord setup
# ----------------------
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# lock para evitar solapamiento de /say
tts_lock = asyncio.Lock()

# ----------------------
# Flask health endpoints (opcional)
# ----------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot online", 200

@app.route("/status")
def status():
    return jsonify({"status": "ok", "guilds": len(bot.guilds)}), 200

def run_flask():
    # Bind to the port Render gives us (important). Print to stdout immediately.
    port = int(os.environ.get("PORT", os.environ.get("PORTAL_PORT", 8080)))
    LOG.info("Starting Flask on 0.0.0.0:%s", port)
    # app.run() es suficiente en hilo, no hace falta debug ni reloader
    app.run(host="0.0.0.0", port=port, threaded=True)


# ----------------------
# Helpers audio
# ----------------------
def _after_set_event(finished_event: asyncio.Event):
    """
    Devuelve una función 'after' que setea finished_event (thread-safe).
    """
    def _after(err):
        if err:
            LOG.exception("Error en reproducción (after): %s", err)
        try:
            bot.loop.call_soon_threadsafe(finished_event.set)
        except Exception:
            LOG.exception("No se pudo señalizar finished_event desde after.")
    return _after


async def play_file_and_wait(vc: discord.VoiceClient, file_path: str):
    """
    Reproduce file_path en vc y espera a que termine.
    """
    finished = asyncio.Event()
    try:
        player = discord.FFmpegPCMAudio(file_path)
        vc.play(player, after=_after_set_event(finished))
    except Exception:
        LOG.exception("Error iniciando reproducción en play_file_and_wait")
        raise
    await finished.wait()


# ----------------------
# Slash commands
# ----------------------
@tree.command(name="join", description="Conecta el bot al canal de voz (no obligatorio)")
@app_commands.describe(channel_id="ID del canal de voz (opcional)")
async def join(interaction: discord.Interaction, channel_id: Optional[str] = None):
    await interaction.response.send_message("🔄 Intentando conectarme...", ephemeral=True)
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
        LOG.exception("Error en /join")
        await interaction.followup.send(f"⚠️ Error al unir: `{e}`", ephemeral=True)


@tree.command(name="leave", description="Desconecta el bot del canal de voz")
async def leave(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Desconectando...", ephemeral=True)
    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect()
            await interaction.followup.send("👋 Desconectado.", ephemeral=True)
        else:
            await interaction.followup.send("❌ No estoy en ningún canal.", ephemeral=True)
    except Exception as e:
        LOG.exception("Error en /leave")
        await interaction.followup.send(f"⚠️ Error al desconectar: `{e}`", ephemeral=True)


@tree.command(name="say", description="El bot dice el texto en el canal de voz")
@app_commands.describe(texto="Texto a decir (máx 1000 caracteres recomendado)")
async def say(interaction: discord.Interaction, texto: str):
    # Intentamos defer pero no romper si la interacción ya expiró/ha sido respondida
    deferred = False
    try:
        await interaction.response.defer(ephemeral=True)
        deferred = True
    except discord.errors.NotFound:
        # interacción ya no válida -> seguiremos con followups directos si es posible
        deferred = False
    except Exception:
        logging.exception("Error al hacer defer de la interacción")
        deferred = False

    if len(texto) > 1000:
        if deferred:
            await interaction.followup.send("❌ Texto demasiado largo.", ephemeral=True)
        else:
            try: await interaction.channel.send("❌ Texto demasiado largo.")
            except: pass
        return

    # Obtener VoiceClient (y unir si hace falta)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        if interaction.user.voice and interaction.user.voice.channel:
            try:
                vc = await interaction.user.voice.channel.connect(reconnect=True)
            except Exception as e:
                logging.exception("No puedo unirme al canal")
                if deferred:
                    await interaction.followup.send("❌ No puedo unirme al canal de voz.", ephemeral=True)
                else:
                    try: await interaction.channel.send("❌ No puedo unirme al canal de voz.")
                    except: pass
                return
        else:
            if deferred:
                await interaction.followup.send("❌ No estás en un canal de voz.", ephemeral=True)
            else:
                try: await interaction.channel.send("❌ No estás en un canal de voz.")
                except: pass
            return

    # Aviso de generación
    try:
        if deferred:
            await interaction.followup.send("📢 Generando audio...", ephemeral=True)
        else:
            await interaction.channel.send("📢 Generando audio...")
    except Exception:
        pass

    # COMPROBACIÓN API KEY ElevenLabs
    ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
    if not ELEVEN_API_KEY:
        msg = "❌ ELEVEN_API_KEY no configurada en las env vars."
        logging.error(msg)
        if deferred:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            try: await interaction.channel.send(msg)
            except: pass
        return

    # Generar audio por streaming y guardarlo en temporal
    try:
        client = ElevenLabs(api_key=ELEVEN_API_KEY)
        audio_gen = client.text_to_speech.convert(
            text=texto,
            voice_id="Nh2zY9kknu6z4pZy6FhD",
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",
        )
    except Exception as e:
        logging.exception("Error generando audio ElevenLabs")
        # Si ElevenLabs devuelve ApiError con status 401, damos mensaje específico
        try:
            from elevenlabs.core.api_error import ApiError
            if isinstance(e, ApiError) and getattr(e, "status_code", None) == 401:
                msg = ("❌ ElevenLabs: Unauthorized / Free tier bloqueado. "
                       "Comprueba tu API key o considera contratar un plan de pago "
                       "o usar otro TTS (Coqui local).")
                logging.error("ElevenLabs 401 - free tier blocked / unusual activity")
                if deferred:
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    try: await interaction.channel.send(msg)
                    except: pass
                return
        except Exception:
            pass

        # Mensaje genérico
        if deferred:
            await interaction.followup.send(f"⚠️ Error ElevenLabs: {e}", ephemeral=True)
        else:
            try: await interaction.channel.send(f"⚠️ Error ElevenLabs: {e}")
            except: pass
        return

    # Guardar stream en temporal
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tmp_path = tmp.name
        with open(tmp_path, "wb") as f:
            for chunk in audio_gen:
                # chunk debería ser bytes/bytearray; si no, convertir
                if isinstance(chunk, (bytes, bytearray)):
                    f.write(chunk)
                else:
                    try:
                        f.write(bytes(chunk))
                    except Exception:
                        logging.exception("Chunk no escribible, lo ignoro")
    except Exception as e:
        logging.exception("Error escribiendo temporal ElevenLabs")
        if deferred:
            await interaction.followup.send("⚠️ Error guardando audio.", ephemeral=True)
        else:
            try: await interaction.channel.send("⚠️ Error guardando audio.")
            except: pass
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
        return

    # Reproducir: capturamos loop principal para notificar final desde callback
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def after_playing(err):
        if err:
            logging.exception("Error en reproducción TTS: %s", err)
        # señalamos que ha terminado en la loop principal
        loop.call_soon_threadsafe(finished.set)

    try:
        # stop previo si existe
        try:
            if vc.is_playing():
                vc.stop()
        except Exception:
            pass

        # reproducir
        player = discord.FFmpegPCMAudio(tmp_path)
        vc.play(player, after=after_playing)
    except Exception as e:
        logging.exception("Error reproduciendo audio")
        if deferred:
            await interaction.followup.send("⚠️ Error al reproducir audio.", ephemeral=True)
        else:
            try: await interaction.channel.send("⚠️ Error al reproducir audio.")
            except: pass
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
        return

    # Esperar a que termine
    try:
        await finished.wait()
    except Exception:
        pass

    # Borrar temporal
    try:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception:
        logging.exception("No pude borrar tmp")

    # Mensaje final
    try:
        if deferred:
            await interaction.followup.send("✅ He terminado de hablar.", ephemeral=True)
        else:
            try: await interaction.channel.send("✅ He terminado de hablar.")
            except: pass
    except Exception:
        pass


# ----------------------
# Events (sin reconexión automática)
# ----------------------
@bot.event
async def on_ready():
    try:
        await tree.sync()
        LOG.info("Slash commands sincronizados.")
    except Exception:
        LOG.exception("Error al sincronizar slash commands")
    LOG.info("Bot conectado como %s.", bot.user)


# ----------------------
# Start server + bot
# ----------------------
if __name__ == "__main__":
    # arrancar Flask en hilo
    flask_thread = threading.Thread(target=run_flask, name="flask-thread", daemon=True)
    flask_thread.start()

    # small log to force flush
    LOG.info("Flask thread arrancado; esperando 1s antes de iniciar bot...")
    # permitir que Flask se vincule al puerto antes de iniciar el bot
    time.sleep(1)

    LOG.info("Iniciando keepalive interno (si configurado).")
    def start_internal_keepalive(url, interval=300):
        def loop():
            while True:
                try:
                    requests.get(url, timeout=10)
                    logging.info("Keepalive interno enviado.")
                except Exception as e:
                    logging.error(f"Keepalive error: {e}")
                time.sleep(interval)
    
        threading.Thread(target=loop, daemon=True).start()

    LOG.info("Arrancando bot (calling bot.run)...")
    # Lanzar python en modo sin buffer en Render (ver comando siguiente)
    bot.run(TOKEN)
