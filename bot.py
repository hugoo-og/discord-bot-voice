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

# Guarda el último canal de voz por guild para posibles reconexiones
guild_last_voice_channel: dict[int, int] = {}

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
    Si ya está reproduciendo, lo para antes y espera un momento.
    """
    try:
        if vc.is_playing():
            try:
                vc.stop()
            except Exception:
                pass
            # dejar que ffmpeg muera un momento
            import time as _t
            _t.sleep(0.1)

        player = discord.FFmpegPCMAudio(source_path)
        vc.play(player, after=after)
    except Exception as e:
        logging.exception("Error al reproducir audio: %s", e)
        raise



# === Slash commands ===

@tree.command(name="join", description="Haz que el bot entre a un canal de voz (opcional: canal ID)")
@app_commands.describe(channel_id="ID del canal de voz (opcional)")
async def join(interaction: discord.Interaction, channel_id: str | None = None):
    # respondemos rápido para evitar timeout de la interacción
    await interaction.response.send_message("🔄 Intentando conectarme... (operación en background)", ephemeral=True)

    # calculamos channel_id numérico
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

    # lanzamos tarea background que avisará por el canal del comando
    notify_channel_id = interaction.channel.id if interaction.channel else None
    bot.loop.create_task(_do_connect(interaction.guild.id, target_channel_id, notify_channel_id))


async def _do_connect(guild_id: int, channel_id: int, notify_channel_id: int | None):
    """
    Tarea en background para conectar al canal de voz y notificar.
    """
    try:
        guild = bot.get_guild(guild_id)
        if not guild:
            logging.warning("Guild not found for id %s", guild_id)
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
    except Exception as e:
        logging.exception("Error en background connect")
        try:
            if notify_channel_id:
                guild = bot.get_guild(guild_id)
                if guild:
                    ch = guild.get_channel(notify_channel_id)
                    if ch:
                        await ch.send(f"⚠️ Error en conexión (background): `{e}`")
        except Exception:
            pass


@tree.command(name="leave", description="Haz que el bot salga del canal de voz")
async def leave(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Desconectando (background)...", ephemeral=True)
    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            # desconecta en background para no bloquear interacción
            async def _do_leave():
                try:
                    await vc.disconnect()
                except Exception:
                    logging.exception("Error al desconectar background")
            bot.loop.create_task(_do_leave())
            # limpiar último canal conocido
            guild_last_voice_channel.pop(interaction.guild.id, None)
            await interaction.followup.send("👋 He pedido desconectar.", ephemeral=True)
        else:
            await interaction.followup.send("❌ No estoy en ningún canal.", ephemeral=True)
    except Exception as e:
        logging.exception("Error en /leave")
        await interaction.followup.send(f"⚠️ Error al desconectar: `{e}`", ephemeral=True)


@tree.command(name="say", description="El bot dice el texto en el canal de voz")
@app_commands.describe(texto="Texto a decir (máx 200 caracteres recomendado)")
async def say(interaction: discord.Interaction, texto: str):
    if len(texto) > 1000:
        await interaction.response.send_message("❌ Texto demasiado largo.", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ El bot no está en ningún canal de voz. Usa /join antes.", ephemeral=True)
        return

    await interaction.response.send_message("📢 Generando audio y preparándome...", ephemeral=True)

    async with tts_lock:
        # generar TTS
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                tmp_path = tmp.name
            tts = gTTS(text=texto, lang="es")
            tts.save(tmp_path)
        except Exception as e:
            logging.exception("Error al generar TTS")
            await interaction.followup.send(f"⚠️ Error al generar TTS: `{e}`", ephemeral=True)
            try:
                if os.path.exists(tmp_path): os.remove(tmp_path)
            except: pass
            return

        # aseguramos que no esté reproduciendo; si lo está, lo paramos y esperamos
        try:
            if vc.is_playing():
                vc.stop()
                # small wait to let ffmpeg die
                await asyncio.sleep(0.15)
        except Exception:
            logging.exception("Error al parar reproducción previa")

        finished = asyncio.Event()

        def after_playing(err):
            if err:
                logging.exception("Error en reproducción TTS: %s", err)
            try:
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(finished.set)
            except Exception:
                pass

        # intentamos reproducir con manejo de Already playing
        try:
            play_audio(vc, tmp_path, after=after_playing)
        except discord.errors.ClientException as e:
            # Already playing o Not connected
            logging.exception("ClientException en play_audio: %s", e)
            try:
                if vc.is_playing():
                    vc.stop()
                    await asyncio.sleep(0.15)
                play_audio(vc, tmp_path, after=after_playing)
            except Exception as e2:
                logging.exception("Segundo intento de reproducir falló: %s", e2)
                await interaction.followup.send("⚠️ No se pudo reproducir el audio.", ephemeral=True)
                try:
                    if os.path.exists(tmp_path): os.remove(tmp_path)
                except: pass
                return

        # esperar a termino
        await finished.wait()

        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            logging.exception("No se pudo borrar tmp TTS")

        await interaction.followup.send("✅ He terminado de hablar.", ephemeral=True)


@bot.event
async def on_voice_state_update(member, before, after):
    # Si es el bot y ha perdido la conexión, intentamos reconectar al último canal conocido
    if member.id != bot.user.id:
        return

    guild_id = member.guild.id
    vc = member.guild.voice_client

    # Si el bot quedó sin canal (after.channel es None) => intento reconectar si tenemos registro
    if after.channel is None:
        logging.warning("Bot desconectado de voz en guild %s", guild_id)
        last_chan = guild_last_voice_channel.get(guild_id)
        if last_chan:
            # reconectar en background (no spam)
            bot.loop.create_task(_do_connect(guild_id, last_chan, None))



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

    # keepalive interno para que Render no duerma el servicio
    def _keep_alive():
        def ping():
            while True:
                try:
                    requests.get("https://discord-bot-voice-cbpv.onrender.com")
                except:
                    pass
                time.sleep(300)
        t = threading.Thread(target=ping)
        t.daemon = True
        t.start()

    _keep_alive()

    # arranca el bot
    bot.run(TOKEN)

