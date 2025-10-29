#!/usr/bin/env python3
"""
bot_with_cookies.py

Telegram bot (webhook) que:
- detecta links enviados diretamente ou em grupo quando mencionado (@SeuBot + link),
- pergunta "quer baixar?" com botÃ£o,
- ao confirmar, inicia o download e mostra uma barra de progresso atualizada,
- envia partes se necessÃ¡rio (ffmpeg) e mostra mensagem final.
- track de usuÃ¡rios mensais via SQLite.

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

# Logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger("ytbot")

# Token
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN nÃ£o definido. Defina o secret TELEGRAM_BOT_TOKEN e redeploy.")
    sys.exit(1)

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# Flask app
app = Flask(__name__)

# Construir a aplicaÃ§Ã£o do telegram
try:
    application = ApplicationBuilder().token(TOKEN).build()
except Exception:
    LOG.exception("Erro ao construir ApplicationBuilder().")
    sys.exit(1)

# Cria loop asyncio persistente em background
APP_LOOP = asyncio.new_event_loop()

def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

LOG.info("Iniciando event loop de background...")
loop_thread = threading.Thread(target=_start_loop, args=(APP_LOOP,), daemon=True)
loop_thread.start()

# Inicializa a application no loop de background
try:
    fut = asyncio.run_coroutine_threadsafe(application.initialize(), APP_LOOP)
    fut.result(timeout=30)
    LOG.info("Application inicializada no loop de background.")
except Exception:
    LOG.exception("Falha ao inicializar a Application no loop de background.")
    sys.exit(1)

URL_RE = re.compile(r"(https?://[^\s]+)")
PENDING = {}  # token -> metadata (in-memory)

# ------------------- SQLite para usuÃ¡rios mensais -------------------
DB_FILE = "users.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS monthly_users (
            user_id INTEGER PRIMARY KEY,
            last_month TEXT
        )
    """)
    conn.commit()
    conn.close()

def update_user(user_id):
    """Atualiza a tabela com o usuÃ¡rio atual."""
    conn = sqlite3.connect(DB_FILE)
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
    conn.close()

def get_monthly_users_count():
    month = time.strftime("%Y-%m")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM monthly_users WHERE last_month=?", (month,))
    count = c.fetchone()[0]
    conn.close()
    return count

init_db()

# ------------------- Cookies -------------------
def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("Nenhuma variÃ¡vel %s encontrada â€” rodando sem cookies.", env_var)
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

# ------------------- Helpers -------------------
def is_bot_mentioned(update: Update) -> bool:
    try:
        bot_username = application.bot.username
        bot_id = application.bot.id
    except Exception:
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

# ------------------- Handlers -------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"OlÃ¡! Me envie um link do YouTube ou outro vÃ­deo, e eu te pergunto se quer baixar.\n"
        f"UsuÃ¡rios mensais: {get_monthly_users_count()}"
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = get_monthly_users_count()
    await update.message.reply_text(f"ðŸ“Š UsuÃ¡rios mensais: {count}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not getattr(update, "message", None) or not update.message.text:
        return

    # Track user
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

    token = uuid.uuid4().hex
    confirm_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ðŸ“¥ Baixar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("âŒ Cancelar", callback_data=f"cancel:{token}"),
            ]
        ]
    )

    confirm_msg = await update.message.reply_text(f"VocÃª quer baixar este link?\n{url}", reply_markup=confirm_keyboard)
    PENDING[token] = {
        "url": url,
        "chat_id": update.message.chat_id,
        "from_user_id": update.message.from_user.id,
        "confirm_msg_id": confirm_msg.message_id,
        "progress_msg": None,
    }

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("dl:"):
        token = data.split("dl:", 1)[1]
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Esse pedido expirou ou Ã© invÃ¡lido.")
            return
        if query.from_user.id != entry["from_user_id"]:
            await query.edit_message_text("Apenas quem solicitou pode confirmar o download.")
            return

        try:
            await query.edit_message_text("Iniciando download... ðŸŽ¬")
        except Exception:
            pass

        progress_msg = await context.bot.send_message(chat_id=entry["chat_id"], text="ðŸ“¥ Baixando: 0% [â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€]")
        entry["progress_msg"] = {"chat_id": progress_msg.chat_id, "message_id": progress_msg.message_id}
        asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)

    elif data.startswith("cancel:"):
        token = data.split("cancel:", 1)[1]
        entry = PENDING.pop(token, None)
        if not entry:
            await query.edit_message_text("Cancelamento: pedido jÃ¡ expirou.")
            return
        await query.edit_message_text("Cancelado âœ…")

# ------------------- Download task -------------------
async def start_download_task(token: str):
    """Executa o download e envia arquivos."""
    entry = PENDING.get(token)
    if not entry:
        LOG.info("Token nÃ£o encontrado")
        return
    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry["progress_msg"]
    if not pm:
        LOG.info("progress_msg nÃ£o encontrado")
        return

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
                        bar = "â–ˆ" * blocks + "â”€" * (20 - blocks)
                        text = f"ðŸ“¥ Baixando: {percent}% [{bar}]"
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
                            text="âœ… Download concluÃ­do, processando o envio...", chat_id=pm["chat_id"], message_id=pm["message_id"]
                        ),
                        APP_LOOP,
                    )
                except Exception:
                    pass
        except Exception:
            LOG.exception("Erro no progress_hook")

    ydl_opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "quiet": False,
        "logger": LOG,
        "format": "best[height<=720]+bestaudio/best",
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 1,
        "force_ipv4": True,
        "socket_timeout": 30,
        "http_chunk_size": 1048576,
        "retries": 20,
        "fragment_retries": 20,
        **({"cookiefile": COOKIE_PATH} if COOKIE_PATH else {}),
    }

    try:
        await asyncio.to_thread(lambda: _run_ydl(ydl_opts, [url]))
    except Exception as e:
        LOG.exception("Erro no yt-dlp: %s", e)
        try:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text=f"âš ï¸ Erro no download: {str(e)}", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
        except Exception:
            pass
        PENDING.pop(token, None)
        return

    # enviar arquivos
    arquivos = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))]
    for path in arquivos:
        try:
            tamanho = os.path.getsize(path)
            if tamanho > 50 * 1024 * 1024:
                partes_dir = os.path.join(tmpdir, "partes")
                os.makedirs(partes_dir, exist_ok=True)
                os.system(f'ffmpeg -y -i "{path}" -c copy -map 0 -fs 45M "{partes_dir}/part%03d.mp4"')
                partes = sorted(os.listdir(partes_dir))
                for p in partes:
                    ppath = os.path.join(partes_dir, p)
                    with open(ppath, "rb") as fh:
                        await application.bot.send_video(chat_id=chat_id, video=fh)
            else:
                with open(path, "rb") as fh:
                    await application.bot.send_video(chat_id=chat_id, video=fh)
        except Exception:
            LOG.exception("Erro ao enviar arquivo %s", path)

    asyncio.run_coroutine_threadsafe(
        application.bot.edit_message_text(
            text="âœ… Download finalizado e enviado!", chat_id=pm["chat_id"], message_id=pm["message_id"]
        ),
        APP_LOOP,
    )
    PENDING.pop(token, None)

def _run_ydl(options, urls):
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

# ------------------- Handlers registration -------------------
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# ------------------- Webhook -------------------
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
