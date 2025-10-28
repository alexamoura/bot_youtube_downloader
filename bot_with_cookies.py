#!/usr/bin/env python3
"""
bot_with_cookies.py

Bot Telegram (webhook) com:
- confirmação antes do download,
- escolha de qualidade (720/480/360) ou MP3 (áudio),
- barra de progresso atualizada no Telegram,
- divisão automática em partes >50MB (ffmpeg),
- suporte opcional a cookies via YT_COOKIES_B64 (Netscape -> base64).
"""
import os
import sys
import tempfile
import asyncio
import base64
import logging
import threading
import uuid
import re
import time
import yt_dlp
import subprocess
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger("ytbot")

# ---------- atualiza yt-dlp (opcional, silencioso) ----------
try:
    LOG.info("Atualizando yt-dlp...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    LOG.info("yt-dlp atualizado.")
except Exception:
    LOG.warning("Não foi possível atualizar yt-dlp. Continuando com a versão atual.")

# ---------- token ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido. Defina a variável de ambiente e faça redeploy.")
    sys.exit(1)
LOG.info("Token encontrado (len=%d).", len(TOKEN))

# ---------- Flask ----------
app = Flask(__name__)

# ---------- preparar cookies (opcional) ----------
def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("Nenhuma variável %s encontrada — rodando sem cookies.", env_var)
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:
        LOG.exception("Falha ao decodificar %s.", env_var)
        return None

    fd, path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except Exception:
        LOG.exception("Falha ao escrever cookies em %s", path)
        return None

    LOG.info("Cookies gravados em %s", path)
    return path

COOKIE_PATH = prepare_cookies_from_env()

# ---------- app Telegram (inicialização no loop separado) ----------
try:
    application = ApplicationBuilder().token(TOKEN).build()
except Exception:
    LOG.exception("Erro ao construir ApplicationBuilder().")
    sys.exit(1)

# cria loop asyncio separado para processar tarefas do bot (usado por run_coroutine_threadsafe)
APP_LOOP = asyncio.new_event_loop()

def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

LOG.info("Iniciando event loop de background...")
loop_thread = threading.Thread(target=_start_loop, args=(APP_LOOP,), daemon=True)
loop_thread.start()

# inicializa a application no loop de background
try:
    fut = asyncio.run_coroutine_threadsafe(application.initialize(), APP_LOOP)
    fut.result(timeout=30)
    LOG.info("Application inicializada no loop de background.")
except Exception:
    LOG.exception("Falha ao inicializar a Application no loop de background.")
    sys.exit(1)

# ---------- util ----------
URL_RE = re.compile(r"(https?://[^\s]+)")
PENDING = {}  # token -> metadata

def _run_ydl(options, urls):
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

# ---------- handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! Envie um link do YouTube ou Shopee (vídeo) e eu pergunto se deseja baixar.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not getattr(update, "message", None) or not update.message.text:
        return

    text = update.message.text.strip()
    chat_type = update.message.chat.type

    # aceitar mensagem se for privada ou se o bot for mencionado
    if chat_type != "private":
        # checar se mencionou o bot
        mentioned = False
        bot_username = application.bot.username if application and application.bot else None
        if bot_username and update.message.entities:
            for ent in update.message.entities:
                if ent.type == "mention":
                    try:
                        ent_text = update.message.text[ent.offset:ent.offset + ent.length]
                        if ent_text.lower() == f"@{bot_username.lower()}":
                            mentioned = True
                            break
                    except Exception:
                        pass
        if not mentioned:
            return

    # extrair URL
    url = None
    if getattr(update.message, "entities", None):
        for ent in update.message.entities:
            if ent.type in ("url", "text_link"):
                if getattr(ent, "url", None):
                    url = ent.url
                else:
                    try:
                        url = update.message.text[ent.offset:ent.offset + ent.length]
                    except Exception:
                        url = None
                break

    if not url:
        m = URL_RE.search(text)
        if m:
            url = m.group(1)

    if not url:
        if chat_type != "private":
            try:
                await update.message.reply_text("Envie o link do vídeo junto com a menção, por exemplo: @MeuBot https://...")
            except Exception:
                pass
        return

    # checar domínio suportado (YouTube e Shopee)
    lower = url.lower()
    if not ("youtube.com" in lower or "youtu.be" in lower or "shopee" in lower):
        await update.message.reply_text("Desculpe — atualmente aceito links do YouTube e Shopee Video.")
        return

    token = uuid.uuid4().hex
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"), InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")],
        ]
    )
    try:
        confirm_msg = await update.message.reply_text(f"Você quer baixar este link?\n{url}", reply_markup=keyboard)
    except Exception:
        confirm_msg = await context.bot.send_message(chat_id=update.message.chat_id, text=f"Você quer baixar este link?\n{url}", reply_markup=keyboard)

    PENDING[token] = {
        "url": url,
        "chat_id": update.message.chat_id,
        "from_user_id": update.message.from_user.id,
        "confirm_msg_id": confirm_msg.message_id,
        "progress_msg": None,
        "quality": None,
    }

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # confirmar download -> mostrar opções de qualidade + MP3 (720 para baixo)
    if data.startswith("dl:"):
        token = data.split("dl:", 1)[1]
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Esse pedido expirou ou é inválido.")
            return
        if query.from_user.id != entry["from_user_id"]:
            await query.edit_message_text("Apenas quem solicitou pode confirmar o download.")
            return

        # teclado de qualidade (720 / 480 / 360) + MP3
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🎬 720p", callback_data=f"q:720:{token}")],
                [InlineKeyboardButton("🎬 480p", callback_data=f"q:480:{token}")],
                [InlineKeyboardButton("🎬 360p", callback_data=f"q:360:{token}")],
                [InlineKeyboardButton("🎵 MP3 (áudio)", callback_data=f"qa:mp3:{token}")],
                [InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")],
            ]
        )
        await query.edit_message_text("Escolha a qualidade ou formato:", reply_markup=keyboard)
        return

    # cancelar
    if data.startswith("cancel:"):
        token = data.split("cancel:", 1)[1]
        entry = PENDING.pop(token, None)
        try:
            await query.edit_message_text("Cancelado ✅")
        except Exception:
            pass
        return

    # qualidade selecionada (video)
    if data.startswith("q:"):
        _, q_value, token = data.split(":", 2)
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Esse pedido expirou ou é inválido.")
            return
        # forçamos máximos entre 360-720 (segurança)
        qv = int(q_value)
        if qv not in (360, 480, 720):
            qv = 720
        entry["quality"] = qv
        try:
            await query.edit_message_text(f"🎬 Qualidade escolhida: {qv}p\nIniciando download...")
        except Exception:
            pass

        # cria mensagem de progresso
        progress_msg = await context.bot.send_message(chat_id=entry["chat_id"], text="📥 Baixando: 0% [────────────────────]")
        entry["progress_msg"] = {"chat_id": progress_msg.chat_id, "message_id": progress_msg.message_id}
        # iniciar tarefa de download no loop em background
        asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)
        return

    # áudio selecionado (mp3)
    if data.startswith("qa:"):
        _, fmt, token = data.split(":", 2)
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Esse pedido expirou ou é inválido.")
            return
        entry["quality"] = "mp3"
        try:
            await query.edit_message_text("🎵 Formato escolhido: MP3\nIniciando download...")
        except Exception:
            pass

        progress_msg = await context.bot.send_message(chat_id=entry["chat_id"], text="📥 Baixando: 0% [────────────────────]")
        entry["progress_msg"] = {"chat_id": progress_msg.chat_id, "message_id": progress_msg.message_id}
        asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)
        return

# ---------- função principal de download ----------
async def start_download_task(token: str):
    entry = PENDING.get(token)
    if not entry:
        LOG.info("start_download_task: token não encontrado")
        return

    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry["progress_msg"]
    quality = entry.get("quality", 720)

    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

    last_percent = -1
    last_update_ts = time.time()
    WATCHDOG_TIMEOUT = 180

    def progress_hook(d):
        nonlocal last_percent, last_update_ts
        try:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    percent = int(downloaded * 100 / total)
                    if percent != last_percent:
                        last_percent = percent
                        last_update_ts = time.time()
                        blocks = int(percent / 5)
                        bar = "█" * blocks + "─" * (20 - blocks)
                        text = f"📥 Baixando: {percent}% [{bar}]"
                        try:
                            asyncio.run_coroutine_threadsafe(
                                application.bot.edit_message_text(
                                    text=text, chat_id=pm["chat_id"], message_id=pm["message_id"]
                                ),
                                APP_LOOP,
                            )
                        except Exception:
                            pass
            elif status == "finished":
                last_update_ts = time.time()
                try:
                    asyncio.run_coroutine_threadsafe(
                        application.bot.edit_message_text(
                            text="✅ Download concluído, processando o envio...", chat_id=pm["chat_id"], message_id=pm["message_id"]
                        ),
                        APP_LOOP,
                    )
                except Exception:
                    pass
        except Exception:
            LOG.exception("Erro no progress_hook")

    # Detecta Shopee
    is_shopee = "shopee" in url.lower()

    # monta opções do yt-dlp conforme escolha e domínio
    if quality == "mp3":
        ydl_opts = {
            "outtmpl": outtmpl,
            "progress_hooks": [progress_hook],
            "quiet": True,
            "logger": LOG,
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            # retries / timeouts
            "retries": 8,
            "fragment_retries": 8,
            "socket_timeout": 30,
            "http_chunk_size": 2 * 1024 * 1024,
            **({"cookiefile": COOKIE_PATH} if COOKIE_PATH else {}),
        }
    else:
        qv = int(quality) if isinstance(quality, int) or (isinstance(quality, str) and quality.isdigit()) else 720
        if is_shopee:
            # Shopee: força ext=mp4 / tentar HLS/m3u8 ou mp4 direto; user-agent + headers ajudam
            ydl_format = "best[ext=mp4]/best"
            ydl_opts = {
                "outtmpl": outtmpl,
                "progress_hooks": [progress_hook],
                "quiet": True,
                "logger": LOG,
                "format": ydl_format,
                "merge_output_format": "mp4",
                "concurrent_fragment_downloads": 3,
                "force_ipv4": True,
                "socket_timeout": 30,
                "http_chunk_size": 2 * 1024 * 1024,
                "retries": 10,
                "fragment_retries": 10,
                "source_address": "0.0.0.0",
                "noplaylist": True,
                "geo_bypass": True,
                "http_headers": {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36"},
                **({"cookiefile": COOKIE_PATH} if COOKIE_PATH else {}),
            }
        else:
            # YouTube / demais
            ydl_opts = {
                "outtmpl": outtmpl,
                "progress_hooks": [progress_hook],
                "quiet": True,
                "logger": LOG,
                "format": f"bestvideo[height<={qv}]+bestaudio/best",
                "merge_output_format": "mp4",
                "concurrent_fragment_downloads": 4,
                "force_ipv4": True,
                "socket_timeout": 30,
                "http_chunk_size": 2 * 1024 * 1024,
                "retries": 12,
                "fragment_retries": 12,
                "noplaylist": True,
                **({"cookiefile": COOKIE_PATH} if COOKIE_PATH else {}),
            }

    # executa download em thread (bloqueante do yt-dlp)
    try:
        LOG.info("Iniciando yt-dlp: url=%s, shopee=%s, quality=%s", url, is_shopee, quality)
        await asyncio.to_thread(lambda: _run_ydl(ydl_opts, [url]))
    except Exception as e:
        LOG.exception("Erro no yt-dlp: %s", e)
        try:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text=f"⚠️ Erro no download: {str(e)}", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
        except Exception:
            pass
        PENDING.pop(token, None)
        # cleanup
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass
        return

    # watchdog
    if time.time() - last_update_ts > WATCHDOG_TIMEOUT:
        try:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="⚠️ Download travou (sem progresso). O yt-dlp continuará tentando; se quiser, tente novamente mais tarde.",
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"],
                ),
                APP_LOOP,
            )
        except Exception:
            pass

    # listar arquivos gerados
    arquivos = [f for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))]
    if not arquivos:
        try:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="⚠️ Nenhum arquivo gerado.", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
        except Exception:
            pass
        PENDING.pop(token, None)
        # cleanup
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass
        return

    # envio (faz split se >45MB por parte para sobrar margem)
    sent_any = False
    try:
        for f in arquivos:
            path = os.path.join(tmpdir, f)
            tamanho = os.path.getsize(path)
            if tamanho > 50 * 1024 * 1024:
                # split por tamanho (ffmpeg -fs)
                partes_dir = os.path.join(tmpdir, "partes")
                os.makedirs(partes_dir, exist_ok=True)
                cmd = f'ffmpeg -y -i "{path}" -c copy -map 0 -fs 45M "{partes_dir}/part%03d.mp4"'
                LOG.info("Split: %s", cmd)
                os.system(cmd)
                partes = sorted([p for p in os.listdir(partes_dir) if p.endswith(".mp4") or p.endswith(".m4a") or p.endswith(".mp3")])
                for p in partes:
                    ppath = os.path.join(partes_dir, p)
                    try:
                        # enviar de forma thread-safe
                        fut = asyncio.run_coroutine_threadsafe(application.bot.send_video(chat_id=chat_id, video=open(ppath, "rb")), APP_LOOP)
                        fut.result(timeout=120)
                        sent_any = True
                    except Exception:
                        LOG.exception("Erro ao enviar parte %s", ppath)
            else:
                try:
                    if quality == "mp3":
                        fut = asyncio.run_coroutine_threadsafe(application.bot.send_audio(chat_id=chat_id, audio=open(path, "rb")), APP_LOOP)
                        fut.result(timeout=120)
                    else:
                        fut = asyncio.run_coroutine_threadsafe(application.bot.send_video(chat_id=chat_id, video=open(path, "rb")), APP_LOOP)
                        fut.result(timeout=120)
                    sent_any = True
                except Exception:
                    LOG.exception("Erro ao enviar arquivo %s", path)
    finally:
        # cleanup completo
        try:
            for root, dirs, files in os.walk(tmpdir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(tmpdir)
        except Exception:
            pass

    # atualizar mensagem final
    try:
        if sent_any:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="✅ Download finalizado e enviado!", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
        else:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="⚠️ Falha ao enviar o arquivo.", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
    except Exception:
        pass

    PENDING.pop(token, None)

# ---------- registrar handlers ----------
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:|q:|qa:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# ---------- webhook endpoint ----------
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True)
    update = Update.de_json(update_data, application.bot)
    try:
        asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    except Exception:
        LOG.exception("Falha ao agendar process_update")
    return "ok"

@app.route("/")
def index():
    return "Bot rodando"

# ---------- main ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # Nota: em production, use um WSGI server (gunicorn) para rodar Flask; aqui colocamos app.run para facilitar testes locais.
    app.run(host="0.0.0.0", port=port)
