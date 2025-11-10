# bot_gtts.py — gTTS TTS only: join -> speak -> disconnect (if bot joined)
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
from gtts import gTTS

# ----------------------
# Config + logging
# ----------------------
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

if not TOKEN:
    LOG.error("DISCORD_TOKEN no configurado. Saliendo.")
    raise SystemExit(1)

# ----------------------
# Globals
# ----------------------
# guarda último canal por guild para re-conexiones/rejoins si quieres
guild_last_voice_channel: dict[int, int] = {}

# lock para evitar solapamiento de /say
tts_lock = asyncio.Lock()

# timestamp del último audio terminado (para garantizar 1s entre reproducciones)
LAST_SPOKEN = 0.0
LAST_SPOKEN_LOCK = threading.Lock()

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
# Helpers audio
# ----------------------

def play_audio(vc: discord.VoiceClient, source_path: str, after=None):
    try:
        if vc.is_playing():
            try:
                vc.stop()
            except Exception:
                pass
            import time as _t
            _t.sleep(0.05)

        player = discord.FFmpegPCMAudio(source_path)
        vc.play(player, after=after)
    except Exception:
        LOG.exception("Error al reproducir audio en play_audio")
        raise


async def play_file_and_wait(vc: discord.VoiceClient, file_path: str):
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _after(error):
        if error:
            LOG.exception("Error dentro de after: %s", error)
        try:
            loop.call_soon_threadsafe(finished.set)
        except Exception:
            LOG.exception("No se pudo señalizar finished desde after")

    try:
        play_audio(vc, file_path, after=_after)
    except Exception:
        raise
    await finished.wait()


async def safe_followup(interaction: discord.Interaction, content: str, ephemeral: bool = True):
    try:
        # si no se ha hecho defer, followup fallará; usamos send_message cuando proceda
        await interaction.followup.send(content, ephemeral=ephemeral)
    except Exception:
        try:
            # si interaction.response no fue usado o expiró, intentamos channel
            if interaction.channel:
                await interaction.channel.send(content)
        except Exception:
            LOG.exception("No pude enviar mensaje de followup/safe")


# ----------------------
# Slash commands
# ----------------------
@tree.command(name="join", description="El bot entra al canal de voz (mencion / seleccionado).")
@app_commands.describe(channel="Canal de voz objetivo")
async def join(interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
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
    # Defer para evitar "Unknown interaction" cuando la generación tarde
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    if len(texto) > 1000:
        await safe_followup(interaction, "❌ Texto demasiado largo.", True)
        return

    # Asegurar conexión al canal del usuario
    vc = interaction.guild.voice_client
    connected_here = False
    if not vc or not vc.is_connected():
        if interaction.user.voice and interaction.user.voice.channel:
            try:
                vc = await interaction.user.voice.channel.connect(reconnect=True)
                connected_here = True
                guild_last_voice_channel[interaction.guild.id] = interaction.user.voice.channel.id
            except Exception:
                LOG.exception("No puedo unirme al canal")
                await safe_followup(interaction, "❌ No puedo unirme al canal.", True)
                return
        else:
            await safe_followup(interaction, "❌ No estás en un canal de voz.", True)
            return

    # notificar al invocador en privado
    await safe_followup(interaction, "📢 Generando audio (gTTS)...", True)

    global LAST_SPOKEN

    async with tts_lock:
        tmp_path = None
        try:
            # Si otro audio terminó hace <1s, esperar el resto para dejar 1s de gap
            with LAST_SPOKEN_LOCK:
                last = LAST_SPOKEN
            now = time.time()
            wait = max(0.0, 1.0 - (now - last))
            if wait > 0:
                await asyncio.sleep(wait)

            # crear temporal MP3
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                tmp_path = tmp.name

            loop = asyncio.get_running_loop()

            def synth_save(text, path):
                # gTTS hace la petición a Google; se ejecuta en executor para no bloquear
                tts = gTTS(text=text, lang='es')
                tts.save(path)

            await loop.run_in_executor(None, synth_save, texto, tmp_path)

        except Exception as e:
            LOG.exception("Error generando TTS gTTS")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            await safe_followup(interaction, f"⚠️ Error TTS: `{e}`", True)
            if connected_here:
                try:
                    await vc.disconnect()
                    guild_last_voice_channel.pop(interaction.guild.id, None)
                except Exception:
                    LOG.exception("Error desconectando tras fallo TTS")
            return

        # reproducir y esperar
        try:
            await play_file_and_wait(vc, tmp_path)
        except Exception:
            LOG.exception("Error al reproducir audio")
            await safe_followup(interaction, "⚠️ Error al reproducir audio.", True)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if connected_here:
                try:
                    await vc.disconnect()
                    guild_last_voice_channel.pop(interaction.guild.id, None)
                except Exception:
                    LOG.exception("Error al desconectar después de fallo reproducción")
            return

        # actualizar timestamp para gap entre mensajes
        with LAST_SPOKEN_LOCK:
            LAST_SPOKEN = time.time()

        # borrar temporal
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            LOG.exception("No pude borrar el temporal")

        # respuesta privada al que pidió el comando
        await safe_followup(interaction, "✅ He terminado de hablar.", True)

        # si nos conectamos sólo para esto, desconectamos
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
    flask_thread = threading.Thread(target=run_flask, name="flask-thread", daemon=True)
    flask_thread.start()

    LOG.info("Flask thread arrancado; esperando 1s antes de iniciar bot...")
    time.sleep(1)

    # keepalive interno opcional
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
