#!/usr/bin/env python3
"""
bot_with_cookies.py - Versão Corrigida

Telegram bot (webhook) que:
- detecta links enviados diretamente ou em grupo quando mencionado (@SeuBot + link),
- pergunta "quer baixar?" com botão,
- ao confirmar, inicia o download e mostra uma barra de progresso atualizada,
- envia partes se necessário (ffmpeg) e mostra mensagem final.
- track de usuários mensais via SQLite.

Requisitos:
- TELEGRAM_BOT_TOKEN (env)
- YT_COOKIES_B64 (opcional; base64 do cookies.txt em formato Netscape)
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
import sqlite3
import shutil
import subprocess
from collections import OrderedDict
from contextlib import contextmanager
from urllib.parse import urlparse
import yt_dlp

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

# ==================== CONFIGURAÇÃO ====================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ytbot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido. Defina o secret TELEGRAM_BOT_TOKEN e redeploy.")
    sys.exit(1)

URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = "users.db"
PENDING_MAX_SIZE = 1000
PENDING_EXPIRE_SECONDS = 600  # 10 minutos
WATCHDOG_TIMEOUT = 180  # 3 minutos
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
SPLIT_SIZE = 45 * 1024 * 1024  # 45MB

PENDING = OrderedDict()
DB_LOCK = threading.Lock()

ERROR_MESSAGES = {
    "timeout": "⏱️ O download demorou muito e foi cancelado.",
    "invalid_url": "⚠️ Esta URL não é válida ou não é suportada.",
    "file_too_large": "📦 O arquivo é muito grande para processar.",
    "network_error": "🌐 Erro de conexão. Tente novamente em alguns minutos.",
    "ffmpeg_error": "🎬 Erro ao processar o vídeo.",
    "upload_error": "📤 Erro ao enviar o arquivo.",
    "unknown": "❌ Ocorreu um erro inesperado. Tente novamente.",
    "expired": "⏰ Este pedido expirou. Envie o link novamente.",
}

app = Flask(__name__)

# ==================== TELEGRAM APPLICATION ====================

try:
    application = ApplicationBuilder().token(TOKEN).build()
    LOG.info("ApplicationBuilder criado com sucesso.")
except Exception as e:
    LOG.exception("Erro ao construir ApplicationBuilder: %s", str(e))
    sys.exit(1)

APP_LOOP = asyncio.new_event_loop()
def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

loop_thread = threading.Thread(target=_start_loop, args=(APP_LOOP,), daemon=True)
loop_thread.start()

try:
    fut = asyncio.run_coroutine_threadsafe(application.initialize(), APP_LOOP)
    fut.result(timeout=30)
    LOG.info("Application inicializada no loop de background.")
except Exception as e:
    LOG.exception("Falha ao inicializar a Application: %s", str(e))
    sys.exit(1)

# ==================== SQLITE DATABASE ====================

def init_db():
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS monthly_users (
                    user_id INTEGER PRIMARY KEY,
                    last_month TEXT
                )
            """)
            conn.commit()
            LOG.info("Banco de dados inicializado.")
        except sqlite3.Error as e:
            LOG.error("Erro ao inicializar banco de dados: %s", e)
            raise
        finally:
            conn.close()

def update_user(user_id: int):
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            month = time.strftime("%Y-%m")
            c.execute("SELECT last_month FROM monthly_users WHERE user_id=?", (user_id,))
            row = c.fetchone()
            if row:
                if row[0] != month:
                    c.execute("UPDATE monthly_users SET last_month=? WHERE user_id=?", (month, user_id))
            else:
                c.execute("INSERT INTO monthly_users (user_id, last_month) VALUES (?, ?)", (user_id, month))
            conn.commit()
        except sqlite3.Error as e:
            LOG.error("Erro SQLite ao atualizar user %s: %s", user_id, e)
        finally:
            conn.close()

def get_monthly_users_count() -> int:
    month = time.strftime("%Y-%m")
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM monthly_users WHERE last_month=?", (month,))
            count = c.fetchone()[0]
            return count
        except sqlite3.Error as e:
            LOG.error("Erro ao obter contagem de usuários: %s", e)
            return 0
        finally:
            conn.close()

init_db()

# ==================== COOKIES ====================

def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("Nenhuma variável %s encontrada – rodando sem cookies.", env_var)
        return None
    
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        LOG.error("Falha ao decodificar %s: %s", env_var, e)
        return None

    try:
        fd, path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(raw)
        LOG.info("Cookies gravados em %s", path)
        return path
    except Exception as e:
        LOG.error("Falha ao escrever cookies: %s", e)
        return None

COOKIE_PATH = prepare_cookies_from_env()

# ==================== UTILITIES ====================

def is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False

@contextmanager
def temp_download_dir():
    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    LOG.info("Diretório temporário criado: %s", tmpdir)
    try:
        yield tmpdir
    finally:
        try:
            shutil.rmtree(tmpdir)
            LOG.info("Cleanup: removido %s", tmpdir)
        except Exception as e:
            LOG.error("Falha no cleanup de %s: %s", tmpdir, e)

def split_video_file(input_path: str, output_dir: str) -> list:
    os.makedirs(output_dir, exist_ok=True)
    output_pattern = os.path.join(output_dir, "part%03d.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c", "copy", "-map", "0",
        "-fs", f"{SPLIT_SIZE}",
        output_pattern
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
        parts = sorted([os.path.join(output_dir, f) for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f))])
        return parts
    except Exception as e:
        LOG.error("Erro ao dividir arquivo com ffmpeg: %s", e)
        raise

def is_bot_mentioned(update: Update) -> bool:
    try:
        bot_username = application.bot.username
        bot_id = application.bot.id
    except Exception as e:
        LOG.error("Erro ao obter info do bot: %s", e)
        return False

    msg = getattr(update, "message", None)
    if not msg:
        return False

    if bot_username and msg.text and f"@{bot_username}" in msg.text:
        return True
    return False

# ==================== PENDING MANAGEMENT ====================

def add_pending(token: str, data: dict):
    if len(PENDING) >= PENDING_MAX_SIZE:
        oldest = next(iter(PENDING))
        PENDING.pop(oldest)
        LOG.warning("PENDING cheio, removido token: %s", oldest)
    
    data["created_at"] = time.time()
    PENDING[token] = data
    
    asyncio.run_coroutine_threadsafe(_expire_pending(token), APP_LOOP)

async def _expire_pending(token: str):
    await asyncio.sleep(PENDING_EXPIRE_SECONDS)
    entry = PENDING.pop(token, None)
    if entry:
        try:
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["expired"],
                chat_id=entry["chat_id"],
                message_id=entry["confirm_msg_id"]
            )
        except Exception:
            pass

# ==================== TELEGRAM HANDLERS ====================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = get_monthly_users_count()
    await update.message.reply_text(f"Olá! 👋\nUsuários mensais: {count}")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = get_monthly_users_count()
    pending_count = len(PENDING)
    await update.message.reply_text(f"👥 Usuários mensais: {count}\n⏳ Downloads pendentes: {pending_count}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not getattr(update, "message", None) or not update.message.text:
        return
    try:
        update_user(update.message.from_user.id)
    except:
        pass

    text = update.message.text.strip()
    chat_type = update.message.chat.type
    if chat_type != "private" and not is_bot_mentioned(update):
        return

    url = None
    if getattr(update.message, "entities", None):
        for ent in update.message.entities:
            if ent.type in ("url", "text_link"):
                url = getattr(ent, "url", None) or text[ent.offset:ent.offset+ent.length]
                break
    if not url:
        m = URL_RE.search(text)
        if m:
            url = m.group(1)
    if not url or not is_valid_url(url):
        await update.message.reply_text(ERROR_MESSAGES["invalid_url"])
        return

    token = uuid.uuid4().hex
    confirm_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
         InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")]
    ])
    confirm_msg = await update.message.reply_text(f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard)

    add_pending(token, {
        "url": url,
        "chat_id": update.message.chat_id,
        "from_user_id": update.message.from_user.id,
        "confirm_msg_id": confirm_msg.message_id,
        "progress_msg": None,
    })

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("dl:"):
        token = data.split("dl:", 1)[1]
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text(ERROR_MESSAGES["expired"])
            return
        if query.from_user.id != entry["from_user_id"]:
            await query.answer("⚠️ Apenas quem solicitou pode confirmar.", show_alert=True)
            return
        await query.edit_message_text("Iniciando download... 🎬")
        progress_msg = await context.bot.send_message(chat_id=entry["chat_id"], text="📥 Baixando: 0% [────────────────────]")
        entry["progress_msg"] = {"chat_id": progress_msg.chat_id, "message_id": progress_msg.message_id}
        asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)
    elif data.startswith("cancel:"):
        token = data.split("cancel:", 1)[1]
        entry = PENDING.pop(token, None)
        if entry:
            await query.edit_message_text("Cancelado ✅")
        else:
            await query.edit_message_text("Cancelamento: pedido já expirou.")

# ==================== DOWNLOAD TASK ====================

async def start_download_task(token: str):
    entry = PENDING.get(token)
    if not entry or not entry.get("progress_msg"):
        return
    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry["progress_msg"]

    watchdog_task = asyncio.create_task(_watchdog(token, WATCHDOG_TIMEOUT))
    try:
        with temp_download_dir() as tmpdir:
            await _do_download(token, url, tmpdir, chat_id, pm)
    except asyncio.CancelledError:
        await _notify_error(pm, "timeout")
    except Exception:
        await _notify_error(pm, "unknown")
    finally:
        watchdog_task.cancel()
        PENDING.pop(token, None)

async def _watchdog(token: str, timeout: int):
    await asyncio.sleep(timeout)
    entry = PENDING.pop(token, None)
    if entry and entry.get("progress_msg"):
        await _notify_error(entry["progress_msg"], "timeout")

async def _notify_error(pm: dict, error_type: str):
    message = ERROR_MESSAGES.get(error_type, ERROR_MESSAGES["unknown"])
    try:
        await application.bot.edit_message_text(text=message, chat_id=pm["chat_id"], message_id=pm["message_id"])
    except:
        pass

async def _do_download(token: str, url: str, tmpdir: str, chat_id: int, pm: dict):
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
    last_percent = -1

    def progress_hook(d):
        nonlocal last_percent
        try:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    percent = int(downloaded*100/total)
                    if percent != last_percent and percent % 5 == 0:
                        last_percent = percent
                        blocks = int(percent/5)
                        bar = "█"*blocks + "─"*(20-blocks)
                        text = f"📥 Baixando: {percent}% [{bar}]"
                        asyncio.run_coroutine_threadsafe(
                            application.bot.edit_message_text(
                                text=text,
                                chat_id=pm["chat_id"],
                                message_id=pm["message_id"]
                            ), APP_LOOP)
            elif status == "finished":
                asyncio.run_coroutine_threadsafe(
                    application.bot.edit_message_text(
                        text="✅ Download concluído, processando o envio...",
                        chat_id=pm["chat_id"],
                        message_id=pm["message_id"]
                    ), APP_LOOP)
        except Exception as e:
            LOG.error("Erro no progress_hook: %s", e)

    ydl_opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "quiet": False,
        "logger": LOG,
        "format": "bestvideo[height<=720]+bestaudio/best/best",
        "merge_output_format": "mp4",
    }
    if COOKIE_PATH:
        ydl_opts["cookiefile"] = COOKIE_PATH

    def _run_ydl_resilient(options, urls):
        base_opts = options.copy()
        opts_primary = {**base_opts, "format": "bestvideo[height<=720]+bestaudio/best/best"}
        try:
            with yt_dlp.YoutubeDL(opts_primary) as ydl:
                ydl.download(urls)
        except yt_dlp.utils.DownloadError:
            opts_fallback = {**base_opts, "format": "best"}
            with yt_dlp.YoutubeDL(opts_fallback) as ydl:
                ydl.download(urls)

    try:
        await asyncio.to_thread(lambda: _run_ydl_resilient(ydl_opts, [url]))
    except Exception:
        await _notify_error(pm, "network_error")
        return

    arquivos = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))]
    if not arquivos:
        await _notify_error(pm, "unknown")
        return

    for path in arquivos:
        try:
            tamanho = os.path.getsize(path)
            if tamanho > MAX_FILE_SIZE:
                partes_dir = os.path.join(tmpdir, "partes")
                partes = split_video_file(path, partes_dir)
                for idx, ppath in enumerate(partes, 1):
                    with open(ppath, "rb") as fh:
                        await application.bot.send_video(chat_id=chat_id, video=fh, caption=f"Parte {idx}/{len(partes)}")
            else:
                with open(path, "rb") as fh:
                    await application.bot.send_video(chat_id=chat_id, video=fh)
        except Exception:
            await _notify_error(pm, "upload_error")
            return

    try:
        await application.bot.edit_message_text(text="✅ Download finalizado e enviado!", chat_id=pm["chat_id"], message_id=pm["message_id"])
    except:
        pass

# ==================== HANDLERS REGISTRATION ====================

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# ==================== FLASK ROUTES ====================

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    try:
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    except Exception as e:
        LOG.exception("Falha ao processar webhook: %s", e)
    return "ok"

@app.route("/")
def index():
    return "Bot rodando ✅"

@app.route("/health")
def health():
    checks = {"bot": "ok", "db": "ok", "pending_count": len(PENDING), "timestamp": time.time()}
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DB_FILE, timeout=5)
            conn.execute("SELECT 1")
            conn.close()
    except Exception as e:
        checks["db"] = f"error: {str(e)}"
    try:
        bot_info = application.bot.get_me()
        checks["bot_username"] = bot_info.username
    except Exception as e:
        checks["bot"] = f"error: {str(e)}"
    status = 200 if checks["bot"] == "ok" and checks["db"] == "ok" else 503
    return checks, status

# ==================== MAIN ====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
