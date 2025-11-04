# bot.py
import os
import logging
import threading
import tempfile
import asyncio
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify
from elevenlabs import generate, set_api_key
from elevenlabs.client import ElevenLabs

# ----------------------
# Config + logging
# ----------------------
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_TOKEN")
client_el = ElevenLabs(api_key=os.getenv("ELEVEN_API_KEY"))
KEEPALIVE_URL = "https://discord-bot-voice-cbpv.onrender.com"  # reemplaza si cambia

# ----------------------
# TTS model (Coqui)
# ----------------------
# Cargar el modelo una vez al inicio. Si esto tarda, es normal (descarga/cache).
try:
    tts_model = TTS("tts_models/es/css10/vits")
    LOG.info("TTS model cargado correctamente.")
except Exception as e:
    LOG.exception("Error cargando TTS model: %s", e)
    tts_model = None

# ----------------------
# Discord setup
# ----------------------
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# guarda último canal por guild para intentar reconectar si se cae
guild_last_voice_channel: dict[int, int] = {}

# lock para evitar solapamientos de /say
tts_lock = asyncio.Lock()

# ----------------------
# Flask health endpoints
# ----------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot online", 200


@app.route("/status")
def status():
    return jsonify({"status": "ok", "guilds": len(bot.guilds)}), 200


def run_flask():
    # arrancado en hilo daemon
    app.run(host="0.0.0.0", port=8080)


# ----------------------
# Keepalive interno (ping a la URL cada X segundos)
# ----------------------
def start_internal_keepalive(url: str, interval: int = 300):
    import requests

    def ping_loop():
        while True:
            try:
                requests.get(url, timeout=5)
            except Exception:
                # no queremos que falle el hilo por un fallo de red
                LOG.debug("Keepalive: ping fallo (ignorado).")
            time.sleep(interval)

    t = threading.Thread(target=ping_loop, name="keepalive-loop", daemon=True)
    t.start()


# ----------------------
# Audio playback helper (no sleeps bloqueantes aquí)
# ----------------------
def play_audio(vc: discord.VoiceClient, source_path: str, after=None):
    """
    Reproduce un archivo con FFmpegPCMAudio en el VoiceClient.
    No hace sleeps; las esperas se gestionan en async desde la corutina.
    """
    try:
        player = discord.FFmpegPCMAudio(source_path)
        vc.play(player, after=after)
    except Exception:
        LOG.exception("Error al reproducir audio.")
        raise


# ----------------------
# Background connect utility
# ----------------------
async def _do_connect(guild_id: int, channel_id: int, notify_channel_id: Optional[int] = None):
    """
    Intenta conectar al canal en background y notifica al canal que solicitó la acción.
    """
    try:
        guild = bot.get_guild(guild_id)
        if not guild:
            LOG.warning("Guild %s no encontrada.", guild_id)
            return
        channel = guild.get_channel(channel_id)
        if not channel or channel.type != discord.ChannelType.voice:
            if notify_channel_id:
                ch = guild.get_channel(notify_channel_id)
                if ch:
                    await ch.send("❌ Canal de voz inválido (background).")
            return

        vc = guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(channel)
            if notify_channel_id:
                ch = guild.get_channel(notify_channel_id)
                if ch:
                    await ch.send(f"🔁 Movido a **{channel.name}** (background).")
        else:
            await channel.connect(reconnect=True)
            guild_last_voice_channel[guild_id] = channel_id
            if notify_channel_id:
                ch = guild.get_channel(notify_channel_id)
                if ch:
                    await ch.send(f"✅ Conectado a **{channel.name}** (background).")
    except Exception:
        LOG.exception("Error en background connect")
        try:
            if notify_channel_id:
                guild = bot.get_guild(guild_id)
                if guild:
                    ch = guild.get_channel(notify_channel_id)
                    if ch:
                        await ch.send(f"⚠️ Error en conexión (background).")
        except Exception:
            pass


# ----------------------
# Slash commands
# ----------------------
@tree.command(name="join", description="Haz que el bot entre a un canal de voz (opcional: canal ID)")
@app_commands.describe(channel_id="ID del canal de voz (opcional)")
async def join(interaction: discord.Interaction, channel_id: str | None = None):
    # Responder rápido para evitar timeout de interacción
    await interaction.response.send_message("🔄 Intentando conectarme... (operación en background)", ephemeral=True)

    # Determinar canal objetivo
    target_channel_id = None
    try:
        if channel_id:
            target_channel_id = int(channel_id)
        else:
            if interaction.user.voice and interaction.user.voice.channel:
                target_channel_id = interaction.user.voice.channel.id
    except Exception:
        target_channel_id = None

    if not target_channel_id:
        await interaction.followup.send("❌ No se ha encontrado canal de voz para unirme.", ephemeral=True)
        return

    notify_channel_id = interaction.channel.id if interaction.channel else None
    bot.loop.create_task(_do_connect(interaction.guild.id, target_channel_id, notify_channel_id))


@tree.command(name="leave", description="Haz que el bot salga del canal de voz")
async def leave(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Desconectando (background)...", ephemeral=True)
    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            async def _do_leave():
                try:
                    await vc.disconnect()
                except Exception:
                    LOG.exception("Error al desconectar background")

            bot.loop.create_task(_do_leave())
            guild_last_voice_channel.pop(interaction.guild.id, None)
            await interaction.followup.send("👋 He pedido desconectar.", ephemeral=True)
        else:
            await interaction.followup.send("❌ No estoy en ningún canal.", ephemeral=True)
    except Exception:
        LOG.exception("Error en /leave")
        await interaction.followup.send("⚠️ Error al desconectar.", ephemeral=True)


@tree.command(name="say", description="El bot dice el texto en el canal de voz (ElevenLabs)")
@app_commands.describe(texto="Texto a decir (máx 300 caracteres recomendado)")
async def say(interaction: discord.Interaction, texto: str):
    await interaction.response.defer(ephemeral=True)

    if not os.getenv("ELEVEN_API_KEY"):
        await interaction.followup.send("⚠️ ELEVEN_API_KEY no configurada.", ephemeral=True)
        return

    if len(texto) > 300:
        await interaction.followup.send("❌ Texto demasiado largo (máx 300).", ephemeral=True)
        return

    # Asegurar conexión de voz (se une al canal del usuario si es necesario)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        if interaction.user.voice and interaction.user.voice.channel:
            try:
                vc = await interaction.user.voice.channel.connect(reconnect=True)
                guild_last_voice_channel[interaction.guild.id] = interaction.user.voice.channel.id
            except Exception:
                logging.exception("No puedo unirme al canal del usuario")
                await interaction.followup.send("❌ No puedo unirme a tu canal.", ephemeral=True)
                return
        else:
            await interaction.followup.send("❌ Debes estar en un canal de voz.", ephemeral=True)
            return

    # Intentar notificar (si la interacción expiró, seguimos igual)
    try:
        await interaction.followup.send("🔊 Generando voz (ElevenLabs)...", ephemeral=True)
    except discord.HTTPException:
        logging.debug("followup.send falló (posible interacción expirada). Continuamos.")

    async with tts_lock:
        tmp_path = None
        try:
            # archivo temporal .mp3
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                tmp_path = tmp.name

            # Generar audio con ElevenLabs (usa el voice_id que pediste)
            audio = client_el.text_to_speech.convert(
                text=texto,
                voice_id="Nh2zY9kknu6z4pZy6FhD",
                model_id="eleven_multilingual_v2",
                output_format="mp3_44100_128",
            )

            # 'audio' viene como bytes — lo guardamos
            with open(tmp_path, "wb") as f:
                f.write(audio)

        except Exception as e:
            logging.exception("Error generando audio ElevenLabs")
            # cleanup
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except:
                pass
            await interaction.followup.send(f"⚠️ Error TTS externo: `{e}`", ephemeral=True)
            return

        # Si se está reproduciendo algo, parar y dar un breve margen asíncrono
        try:
            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.12)
        except Exception:
            logging.exception("Error al detener reproducción previa (ignorado)")

        # Evento para saber cuándo ha acabado la reproducción
        finished = asyncio.Event()

        def after_playing(error):
            if error:
                logging.exception("Error en after_playing: %s", error)
            try:
                bot.loop.call_soon_threadsafe(finished.set)
            except Exception:
                logging.exception("No se pudo señalizar finished desde after_playing")

        # Reproducir el mp3 generado
        try:
            play_audio(vc, tmp_path, after=after_playing)
        except Exception:
            logging.exception("Error iniciando reproducción")
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except:
                pass
            await interaction.followup.send("⚠️ Error al reproducir el audio.", ephemeral=True)
            return

        # Esperar a que termine (no bloqueante)
        try:
            await finished.wait()
        except Exception:
            logging.exception("Error esperando finished")

        # Borrar archivo temporal
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            logging.exception("No se pudo borrar tmp TTS")

        # Aviso final (si aún se puede)
        try:
            await interaction.followup.send("✅ He terminado de hablar.", ephemeral=True)
        except discord.HTTPException:
            logging.debug("No se pudo enviar followup final (interacción posiblemente expirada).")



# ----------------------
# Reconexion: detecta si el bot ha sido desconectado y reintenta
# ----------------------
@bot.event
async def on_voice_state_update(member, before, after):
    # Sólo nos interesa si es el bot
    if not bot.user:
        return
    if member.id != bot.user.id:
        return

    guild_id = member.guild.id

    # Si el bot quedó sin canal (desconexión) -> reintentar si hay último canal conocido
    if after.channel is None:
        LOG.warning("Bot desconectado de voz en guild %s", guild_id)
        last_chan = guild_last_voice_channel.get(guild_id)
        if last_chan:
            # lanzamos intento de reconexión con poca demora para evitar bucles inmediatos
            async def _reconnect():
                await asyncio.sleep(2)
                try:
                    await _do_connect(guild_id, last_chan, None)
                except Exception:
                    LOG.exception("Reintento de reconexión falló")
            bot.loop.create_task(_reconnect())


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
# Start everything
# ----------------------
if __name__ == "__main__":
    # Flask
    flask_thread = threading.Thread(target=run_flask, name="flask-thread", daemon=True)
    flask_thread.start()

    # Keepalive interno (ping)
    start_internal_keepalive(KEEPALIVE_URL, interval=300)

    # Arrancar bot
    if not TOKEN:
        LOG.error("DISCORD_TOKEN no configurado.")
        raise SystemExit(1)

    bot.run(TOKEN)
