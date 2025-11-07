# bot.py — Coqui TTS (local) — join -> speak -> disconnect (si entró el bot)
import os
import logging
import tempfile
import asyncio
import time
import threading
import requests

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify
from TTS.api import TTS  # Coqui TTS local API

# ----------------------
# Config + logging
# ----------------------
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_TOKEN")
COQUI_MODEL = os.getenv("COQUI_MODEL", "tts_models/es/css10/vits")
PORT = int(os.environ.get("PORT", 10000))

if not TOKEN:
    LOG.error("DISCORD_TOKEN no configurado. Saliendo.")
    raise SystemExit(1)

# ----------------------
# Globals
# ----------------------
# modelo Coqui (lazy)
tts_model: TTS | None = None
tts_model_lock = threading.Lock()

# guarda último canal por guild para re-conexiones/rejoins si quieres
guild_last_voice_channel: dict[int, int] = {}

# lock para evitar solapamiento de /say
tts_lock = asyncio.Lock()

# ----------------------
# Discord setup
# ----------------------
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

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
    LOG.info("Starting Flask on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)


# ----------------------
# Helpers audio / TTS
# ----------------------
def ensure_coqui_model():
    """
    Inicializa el modelo Coqui TTS de forma thread-safe y sin bloquear el loop principal.
    Se puede llamar desde run_in_executor para no bloquear.
    """
    global tts_model
    if tts_model is not None:
        return
    with tts_model_lock:
        if tts_model is None:
            LOG.info("Cargando modelo Coqui: %s (esto puede tardar)", COQUI_MODEL)
            tts_model = TTS(COQUI_MODEL)
            LOG.info("Modelo Coqui cargado.")


def play_audio(vc: discord.VoiceClient, source_path: str, after=None):
    """
    Reproduce con FFmpegPCMAudio. Si ya está reproduciendo lo para antes.
    'after' será llamada por el hilo del player; no debe acceder al loop directamente.
    """
    try:
        if vc.is_playing():
            try:
                vc.stop()
            except Exception:
                pass
            # dejar que ffmpeg muera un momento
            import time as _t
            _t.sleep(0.05)

        player = discord.FFmpegPCMAudio(source_path)
        vc.play(player, after=after)
    except Exception:
        LOG.exception("Error al reproducir audio en play_audio")
        raise


async def play_file_and_wait(vc: discord.VoiceClient, file_path: str):
    """
    Reproduce file_path en vc y espera a que termine (usa Event y call_soon_threadsafe).
    """
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _after(error):
        if error:
            LOG.exception("Error dentro de after: %s", error)
        loop.call_soon_threadsafe(finished.set)

    try:
        play_audio(vc, file_path, after=_after)
    except Exception:
        raise
    await finished.wait()


async def safe_followup(interaction: discord.Interaction, content: str, ephemeral: bool = True):
    """
    Intenta enviar followup; si falla porque la interacción expiró, intenta canal.
    """
    try:
        await interaction.followup.send(content, ephemeral=ephemeral)
    except discord.NotFound:
        # interacción desconocida/expirada
        try:
            if interaction.channel:
                await interaction.channel.send(content)
        except Exception:
            LOG.exception("No pude enviar mensaje por canal tras fallar followup.")
    except Exception:
        LOG.exception("Error al enviar followup.")


# ----------------------
# Slash commands
# ----------------------
@tree.command(name="join", description="El bot entra al canal de voz (mencion / seleccionado).")
@app_commands.describe(channel="Canal de voz objetivo")
async def join(interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
    # Responder rápido para no caducar la interacción
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        # si ya expiró, se intenta seguir adelante
        pass

    target = channel
    if target is None:
        if interaction.user.voice and interaction.user.voice.channel:
            target = interaction.user.voice.channel
        else:
            await safe_followup(interaction, "❌ No has especificado canal y no estás en uno.", True)
            return

    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(target)
        else:
            await target.connect(reconnect=True)
        guild_last_voice_channel[interaction.guild.id] = target.id
        await safe_followup(interaction, f"✅ Conectado a **{target.name}**", True)
    except Exception as e:
        LOG.exception("Error al conectar")
        await safe_followup(interaction, f"⚠️ Error al conectar: `{e}`", True)


@tree.command(name="leave", description="Haz que el bot salga del canal de voz")
async def leave(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            try:
                await vc.disconnect()
            except Exception:
                LOG.exception("Error al desconectar")
            guild_last_voice_channel.pop(interaction.guild.id, None)
            await safe_followup(interaction, "👋 Desconectado.", True)
        else:
            await safe_followup(interaction, "❌ No estoy en ningún canal.", True)
    except Exception as e:
        LOG.exception("Error en /leave")
        await safe_followup(interaction, f"⚠️ Error al desconectar: `{e}`", True)


@tree.command(name="say", description="El bot dice el texto en el canal de voz")
@app_commands.describe(texto="Texto a decir (máx 200 caracteres recomendado)")
async def say(interaction: discord.Interaction, texto: str):
    # Defer defensivo (evita Unknown interaction por tiempo de generación)
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        # si ya expiró, seguimos intentando enviar followups
        pass

    if len(texto) > 1000:
        await safe_followup(interaction, "❌ Texto demasiado largo.", True)
        return

    # Garantizar conexión al canal del usuario (si el bot ya está, usarla; sino unirse temporalmente)
    vc = interaction.guild.voice_client
    connected_here = False
    if not vc or not vc.is_connected():
        if interaction.user.voice and interaction.user.voice.channel:
            try:
                vc = await interaction.user.voice.channel.connect(reconnect=True)
                connected_here = True
                guild_last_voice_channel[interaction.guild.id] = interaction.user.voice.channel.id
            except Exception as e:
                LOG.exception("No puedo unirme al canal")
                await safe_followup(interaction, "❌ No puedo unirme al canal.", True)
                return
        else:
            await safe_followup(interaction, "❌ No estás en un canal de voz.", True)
            return

    await safe_followup(interaction, "📢 Generando audio (Coqui TTS)...", True)

    async with tts_lock:
        tmp_path = None
        try:
            # crear temporal WAV
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                tmp_path = tmp.name

            # cargar modelo (si hace falta) y generar en executor para no bloquear loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, ensure_coqui_model)

            # tts_model.tts_to_file es bloqueante => ejecutarlo en executor:
            def synth_to_file(text, path):
                # tts_model está garantizado por ensure_coqui_model
                global tts_model
                tts_model.tts_to_file(text=text, file_path=path)

            await loop.run_in_executor(None, synth_to_file, texto, tmp_path)

        except Exception as e:
            LOG.exception("Error al generar TTS Coqui")
            if tmp_path and os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass
            await safe_followup(interaction, f"⚠️ Error TTS: `{e}`", True)
            # si nos conectamos sólo para esto, desconectar
            if connected_here:
                try:
                    await vc.disconnect()
                    guild_last_voice_channel.pop(interaction.guild.id, None)
                except Exception:
                    LOG.exception("Error al desconectar tras fallar TTS")
            return

        # reproducir y esperar
        try:
            await play_file_and_wait(vc, tmp_path)
        except Exception:
            LOG.exception("Error al reproducir audio")
            await safe_followup(interaction, "⚠️ Error al reproducir audio.", True)
            # borrar temporal
            if tmp_path and os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass
            if connected_here:
                try:
                    await vc.disconnect()
                    guild_last_voice_channel.pop(interaction.guild.id, None)
                except Exception:
                    LOG.exception("Error al desconectar después de fallo reproducción")
            return

        # borrar temporal
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            LOG.exception("No pude borrar el temporal")

        await safe_followup(interaction, "✅ He terminado de hablar.", True)

        # si nos conectamos solamente para decir esto, desconectamos
        if connected_here:
            try:
                await vc.disconnect()
                guild_last_voice_channel.pop(interaction.guild.id, None)
            except Exception:
                LOG.exception("Error al desconectar después de hablar")


# ----------------------
# Events
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
    # arrancar Flask en hilo (bind al puerto que dé Render)
    flask_thread = threading.Thread(target=run_flask, name="flask-thread", daemon=True)
    flask_thread.start()

    LOG.info("Flask thread arrancado; esperando 1s antes de iniciar bot...")
    time.sleep(1)

    # keepalive interno opcional: si defines KEEPALIVE_URL en env arrancará
    KEEPALIVE_URL = os.getenv("KEEPALIVE_URL")
    if KEEPALIVE_URL:
        def start_internal_keepalive(url, interval=300):
            def loop_ping():
                while True:
                    try:
                        requests.get(url, timeout=10)
                        LOG.info("Keepalive interno enviado.")
                    except Exception as e:
                        LOG.error("Keepalive error: %s", e)
                    time.sleep(interval)
            threading.Thread(target=loop_ping, daemon=True).start()
        LOG.info("Iniciando keepalive interno (si configurado).")
        start_internal_keepalive(KEEPALIVE_URL, interval=int(os.getenv("KEEPALIVE_INTERVAL", 300)))

    LOG.info("Arrancando bot (calling bot.run)...")
    bot.run(TOKEN)
