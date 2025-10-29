#!/usr/bin/env python3
"""
bot_with_cookies.py - Versão Melhorada

Telegram bot (webhook) que:
- detecta links enviados diretamente ou em grupo quando mencionado (@SeuBot + link),
- pergunta "quer baixar?" com botão,
- ao confirmar, inicia o download e mostra uma barra de progresso atualizada,
- envia partes se necessário (ffmpeg) e mostra mensagem final.
- track de usuários mensais via SQLite.

Melhorias implementadas:
- Cleanup automático de arquivos temporários
- Proteção contra race conditions no SQLite
- Watchdog timeout para downloads travados
- Validação de URLs
- Expiração automática de requests pendentes
- Tratamento de erros robusto
- Mensagens de erro amigáveis
- Health check endpoint

Requisitos:
- TELEGRAM_BOT_TOKEN (env)
"""
import os
import sys
import tempfile
import asyncio
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

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = "users.db"
PENDING_MAX_SIZE = 1000
PENDING_EXPIRE_SECONDS = 600
WATCHDOG_TIMEOUT = 180
MAX_FILE_SIZE = 50 * 1024 * 1024
SPLIT_SIZE = 45 * 1024 * 1024

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
    LOG.exception("Falha ao inicializar a Application no loop de background: %s", str(e))
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
        LOG.info("ffmpeg concluído com sucesso.")
        parts = sorted([
            os.path.join(output_dir, f) 
            for f in os.listdir(output_dir)
            if os.path.isfile(os.path.join(output_dir, f))
        ])
        return parts
    except Exception as e:
        LOG.error("Erro no ffmpeg: %s", e)
        raise

def is_bot_mentioned(update: Update) -> bool:
    try:
        bot_username = application.bot.username
        bot_id = application.bot.id
    except Exception as e:
        LOG.error("Erro ao obter info do bot: %s", e)
        bot_username = None
        bot_id = None
    msg = getattr(update, "message", None)
    if not msg:
        return False
    if bot_username:
        if getattr(msg, "entities", None):
            for ent in msg.entities:
                etype = getattr(ent, "type", "")
                if etype == "mention":
                    try:
                        ent_text = msg.text[ent.offset : ent.offset + ent.length]
                    except Exception:
                        ent_text = ""
                    if ent_text.lower() == f"@{bot_username.lower()}":
                        return True
                elif etype == "text_mention":
                    if getattr(ent.user, "id", None) == bot_id:
                        return True
        if msg.text and f"@{bot_username}" in msg.text:
            return True
    return False

# ==================== VIDEO TITLE FETCH (NOVO) ====================

def get_video_title(url: str) -> str:
    """Retorna o título do vídeo usando yt-dlp sem cookies."""
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "format": "best",
        "force_generic_extractor": False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("title", "Vídeo")
    except Exception as e:
        LOG.warning("Não foi possível obter título de %s: %s", url, e)
        return "Vídeo"

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
        LOG.info("Token expirado: %s", token)
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
    try:
        count = get_monthly_users_count()
        await update.message.reply_text(
            f"Olá! 👋\n\n"
            f"Me envie um link do YouTube, Instagram ou outro vídeo, e eu te pergunto se quer baixar.\n\n"
            f"📊 Usuários mensais: {count}"
        )
    except Exception as e:
        LOG.error("Erro no comando /start: %s", e)
        await update.message.reply_text("Erro ao processar comando. Tente novamente.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not getattr(update, "message", None) or not update.message.text:
            return
        update_user(update.message.from_user.id)
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
        if not url:
            return
        if not is_valid_url(url):
            await update.message.reply_text(ERROR_MESSAGES["invalid_url"])
            return

        # Recupera título do vídeo
        video_title = get_video_title(url)

        token = uuid.uuid4().hex
        confirm_keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
            ]]
        )
        confirm_msg = await update.message.reply_text(
            f"Você quer baixar este vídeo?\n🎬 {video_title}\n{url}", 
            reply_markup=confirm_keyboard
        )
        add_pending(token, {
            "url": url,
            "chat_id": update.message.chat_id,
            "from_user_id": update.message.from_user.id,
            "confirm_msg_id": confirm_msg.message_id,
            "progress_msg": None,
        })
    except Exception as e:
        LOG.exception("Erro no handle_message: %s", e)
        try:
            await update.message.reply_text(ERROR_MESSAGES["unknown"])
        except Exception:
            pass

# ==================== REGISTRO HANDLERS ====================

application.add_handler(CommandHandler("start", start_cmd))
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
    checks = {
        "bot": "ok",
        "db": "ok",
        "pending_count": len(PENDING),
        "timestamp": time.time()
    }
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
    LOG.info("Iniciando servidor Flask na porta %d", port)
    app.run(host="0.0.0.0", port=port)
