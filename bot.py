# bot.py — TTS-only behavior: join->speak->disconnect (if bot joined)
import os
import logging
import tempfile
import asyncio
import time
from typing import Optional
import threading

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


@tree.command(name="say", description="El bot dice el texto (entra si hace falta, habla y sale si se unió automáticamente)")
@app_commands.describe(texto="Texto a decir (máx 300 caracteres recomendado)")
async def say(interaction: discord.Interaction, texto: str):
    # usamos defer para evitar problemas de "Unknown interaction"
    await interaction.response.defer(ephemeral=True)

    if len(texto) > 300:
        await interaction.followup.send("❌ Texto demasiado largo (máx 300).", ephemeral=True)
        return

    # comprobar voz y decidir si desconectar después
    vc = interaction.guild.voice_client
    connected_before = bool(vc and vc.is_connected())
    should_disconnect_after = False

    if not connected_before:
        # si no hay vc, intentamos unirnos al canal del usuario
        if interaction.user.voice and interaction.user.voice.channel:
            try:
                vc = await interaction.user.voice.channel.connect(reconnect=True)
                should_disconnect_after = True  # nos desconectaremos tras hablar
            except Exception as e:
                LOG.exception("No puedo unirme al canal del usuario")
                await interaction.followup.send("❌ No puedo unirme a tu canal.", ephemeral=True)
                return
        else:
            await interaction.followup.send("❌ Debes estar en un canal de voz.", ephemeral=True)
            return

    # notificar al usuario que generamos TTS
    try:
        await interaction.followup.send("🔊 Generando voz (ElevenLabs)...", ephemeral=True)
    except Exception:
        LOG.debug("No se pudo enviar followup; seguimos de todas formas.")

    async with tts_lock:
        audio_path = None
        try:
            # archivo temporal
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                audio_path = tmp.name

            # pedir TTS (puede devolver generator)
            audio_gen = client_el.text_to_speech.convert(
                text=texto,
                voice_id=VOICE_ID,
                model_id="eleven_multilingual_v2",
                output_format="mp3_44100_128",
            )

            # escribir chunks si viene generator, o bytes directo
            with open(audio_path, "wb") as f:
                if isinstance(audio_gen, (bytes, bytearray)):
                    f.write(audio_gen)
                else:
                    for chunk in audio_gen:
                        if isinstance(chunk, (bytes, bytearray)):
                            f.write(chunk)

        except Exception:
            LOG.exception("Error generando audio ElevenLabs")
            try:
                if audio_path and os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception:
                pass
            await interaction.followup.send("⚠️ Error generando la voz externa.", ephemeral=True)
            # si nos unimos solo para esto, desconectar
            if should_disconnect_after and vc and vc.is_connected():
                try:
                    await vc.disconnect()
                except Exception:
                    LOG.exception("No se pudo desconectar tras fallo TTS")
            return

        # reproducir y esperar a que termine
        try:
            await play_file_and_wait(vc, audio_path)
        except Exception:
            LOG.exception("Error reproduciendo TTS")
            await interaction.followup.send("⚠️ Error al reproducir el audio.", ephemeral=True)
            try:
                if audio_path and os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception:
                pass
            if should_disconnect_after and vc and vc.is_connected():
                try:
                    await vc.disconnect()
                except Exception:
                    LOG.exception("No se pudo desconectar tras reproducción fallida")
            return

        # borrar temporal
        try:
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception:
            LOG.exception("No se pudo borrar tmp TTS")

    # si el bot se unió solo para hablar, desconectamos
    if should_disconnect_after:
        try:
            if vc and vc.is_connected():
                await vc.disconnect()
        except Exception:
            LOG.exception("Error al desconectar tras /say")

    # mensaje final
    try:
        await interaction.followup.send("✅ He terminado de hablar.", ephemeral=True)
    except Exception:
        LOG.debug("No se pudo enviar followup final (posible interacción expirada).")


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
    start_internal_keepalive(KEEPALIVE_URL, interval=300)

    LOG.info("Arrancando bot (calling bot.run)...")
    # Lanzar python en modo sin buffer en Render (ver comando siguiente)
    bot.run(TOKEN)
