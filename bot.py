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
from elevenlabs.core.api_error import ApiError as ElevenApiError
import TTS

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

async def ensure_vc_and_record(interaction: discord.Interaction):
    """
    Asegura que el bot esté en el canal de voz del usuario.
    Devuelve (vc, error_msg) donde vc es VoiceClient o None si error.
    También actualiza guild_last_voice_channel.
    """
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        return vc, None

    # intentar unir al canal del user
    if interaction.user.voice and interaction.user.voice.channel:
        try:
            vc = await interaction.user.voice.channel.connect(reconnect=True)
            # guardar último canal conocido
            guild_last_voice_channel[interaction.guild.id] = interaction.user.voice.channel.id
            return vc, None
        except Exception as e:
            logging.exception("No puedo unirme al canal desde ensure_vc")
            return None, f"No puedo unirme al canal: {e}"
    else:
        return None, "No estás en un canal de voz."



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


@tree.command(name="leave", description="Haz que el bot salga del canal de voz")
async def leave(interaction: discord.Interaction):
    # Defer defensivo
    try:
        await interaction.response.send_message("🔄 Desconectando...", ephemeral=True)
    except discord.errors.NotFound:
        # interacción expirada: enviar por canal si posible
        try:
            await interaction.channel.send("🔄 Desconectando...")
        except:
            pass
    except Exception:
        logging.exception("Error al responder /leave")

    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            # desconecta en background
            async def _do_leave():
                try:
                    await vc.disconnect()
                except Exception:
                    logging.exception("Error al desconectar en background")
            bot.loop.create_task(_do_leave())
            guild_last_voice_channel.pop(interaction.guild.id, None)
            try:
                await interaction.followup.send("👋 He pedido desconectar.", ephemeral=True)
            except:
                pass
        else:
            try:
                await interaction.followup.send("❌ No estoy en ningún canal.", ephemeral=True)
            except:
                try: await interaction.channel.send("❌ No estoy en ningún canal.")
                except: pass
    except Exception as e:
        logging.exception("Error en /leave")
        try:
            await interaction.followup.send(f"⚠️ Error al desconectar: `{e}`", ephemeral=True)
        except:
            try: await interaction.channel.send(f"⚠️ Error al desconectar: `{e}`")
            except: pass


@tree.command(name="say", description="El bot dice el texto en el canal de voz")
@app_commands.describe(texto="Texto a decir (máx 1000 caracteres recomendado)")
async def say(interaction: discord.Interaction, texto: str):
    # Defer defensivo
    tried_defer = False
    try:
        await interaction.response.defer(ephemeral=True)
        tried_defer = True
    except discord.errors.NotFound:
        # interacción no válida ya; seguiremos con channel messages
        tried_defer = False
    except Exception:
        logging.exception("Error al defer interaction")
        tried_defer = False

    if len(texto) > 1000:
        msg = "❌ Texto demasiado largo."
        if tried_defer:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            try: await interaction.channel.send(msg)
            except: pass
        return

    # Asegurar conexión
    vc, err = await ensure_vc_and_record(interaction)
    if not vc:
        if tried_defer:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
        else:
            try: await interaction.channel.send(f"❌ {err}")
            except: pass
        return

    # Aviso de generación
    try:
        if tried_defer:
            await interaction.followup.send("📢 Generando audio...", ephemeral=True)
        else:
            await interaction.channel.send("📢 Generando audio...")
    except Exception:
        pass

    # Intento ElevenLabs si hay API key
    ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
    tmp_path = None

    # Preparar loop y evento para esperar reproducción
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def after_playing(err):
        if err:
            logging.exception("Error en reproducción: %s", err)
        loop.call_soon_threadsafe(finished.set)

    # Helper para reproducción desde fichero
    async def play_and_wait(path: str):
        try:
            # parar previo
            try:
                if vc.is_playing():
                    vc.stop()
            except Exception:
                pass
            player = discord.FFmpegPCMAudio(path)
            vc.play(player, after=after_playing)
            await finished.wait()
            return True
        except Exception:
            logging.exception("Error al reproducir archivo")
            return False

    # 1) Intentamos ElevenLabs
    eleven_failed = False
    if ELEVEN_API_KEY:
        try:
            client = ElevenLabs(api_key=ELEVEN_API_KEY)
            audio_gen = client.text_to_speech.convert(
                text=texto,
                voice_id="Nh2zY9kknu6z4pZy6FhD",
                model_id="eleven_multilingual_v2",
                output_format="mp3_44100_128",
            )
            # Guardar stream a temporal
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                tmp_path = tmp.name
            with open(tmp_path, "wb") as f:
                for chunk in audio_gen:
                    # chunk debería ser bytes
                    if isinstance(chunk, (bytes, bytearray)):
                        f.write(chunk)
                    else:
                        try:
                            f.write(bytes(chunk))
                        except Exception:
                            logging.exception("No pude convertir chunk a bytes")
            # si llegamos aquí, tenemos tmp_path con mp3
            ok = await play_and_wait(tmp_path)
            if not ok:
                if tried_defer:
                    await interaction.followup.send("⚠️ Error al reproducir audio ElevenLabs.", ephemeral=True)
                else:
                    try: await interaction.channel.send("⚠️ Error al reproducir audio ElevenLabs.")
                    except: pass
            # limpiar archivo temporal
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                logging.exception("No pude borrar tmp ElevenLabs")
            return
        except ElevenApiError as e:
            # ElevenLabs API devolvió error (p. ej. 401)
            logging.exception("ElevenLabs ApiError")
            eleven_failed = True
            # caemos al fallback
        except Exception:
            logging.exception("Error generando audio ElevenLabs")
            eleven_failed = True

    else:
        eleven_failed = True

    # 2) Fallback: Coqui TTS (local) si ElevenLabs falló
    if eleven_failed:
        logging.info("ElevenLabs falló; intentando fallback Coqui TTS")
        try:
            # import perezoso (no obligamos a tener Coqui en startup)
            from TTS.api import TTS as CoquiTTS
        except Exception:
            logging.exception("Coqui TTS no disponible")
            msg = ("❌ No pude generar la voz: ElevenLabs falló y Coqui TTS no está instalado.\n"
                   "Soluciones: 1) Revisa tu ELEVEN_API_KEY y plan; 2) instala Coqui TTS en el entorno.")
            if tried_defer:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                try: await interaction.channel.send(msg)
                except: pass
            return

        # Instanciar modelo (perezoso). Elige el modelo que quieras; este es un ejemplo.
        try:
            # Puedes cambiar el modelo por uno masculino si lo tienes: configura COQUI_MODEL env var si quieres.
            coqui_model = os.getenv("COQUI_MODEL", "tts_models/es/css10/vits")
            coqui = CoquiTTS(coqui_model)
            # crear temporal y generar
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                tmp_path = tmp.name
            # método que guarda directo a archivo
            coqui.tts_to_file(text=texto, file_path=tmp_path)
            # reproducir WAV (ffmpeg lo aceptará)
            ok = await play_and_wait(tmp_path)
            if not ok:
                if tried_defer:
                    await interaction.followup.send("⚠️ Error al reproducir audio Coqui.", ephemeral=True)
                else:
                    try: await interaction.channel.send("⚠️ Error al reproducir audio Coqui.")
                    except: pass
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                logging.exception("No pude borrar tmp Coqui")
            return
        except Exception:
            logging.exception("Error generando audio con Coqui")
            msg = "⚠️ Error generando audio con Coqui."
            if tried_defer:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                try: await interaction.channel.send(msg)
                except: pass
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except:
                pass
            return


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
