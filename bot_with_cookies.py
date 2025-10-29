#!/usr/bin/env python3
"""
Bot Telegram com:
- Download de vídeos via yt-dlp
- Controle de limite de 3 downloads grátis/mês
- Cobrança via Pix Mercado Pago (R$ 9,90) para acesso premium por 30 dias
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
import mercadopago
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

# ------------------- Configurações -------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger("ytbot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido.")
    sys.exit(1)

MP_TOKEN = os.getenv("MERCADO_PAGO_TOKEN")
sdk = mercadopago.SDK(MP_TOKEN)

app = Flask(__name__)
application = ApplicationBuilder().token(TOKEN).build()

APP_LOOP = asyncio.new_event_loop()
def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
threading.Thread(target=_start_loop, args=(APP_LOOP,), daemon=True).start()
asyncio.run_coroutine_threadsafe(application.initialize(), APP_LOOP)

URL_RE = re.compile(r"(https?://[^\s]+)")
PENDING = {}

DB_FILE = "users.db"

# ------------------- Banco de dados -------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS user_downloads (
        user_id INTEGER PRIMARY KEY,
        download_count INTEGER DEFAULT 0,
        last_reset TEXT,
        premium_until TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

def get_download_count(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    month = time.strftime("%Y-%m")
    c.execute("SELECT download_count, last_reset FROM user_downloads WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row:
        count, last_reset = row
        if last_reset != month:
            c.execute("UPDATE user_downloads SET download_count=1, last_reset=? WHERE user_id=?", (month, user_id))
            conn.commit()
            conn.close()
            return 1
        conn.close()
        return count
    else:
        c.execute("INSERT INTO user_downloads (user_id, download_count, last_reset) VALUES (?, ?, ?)", (user_id, 1, month))
        conn.commit()
        conn.close()
        return 1

def increment_download_count(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE user_downloads SET download_count = download_count + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def is_premium(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT premium_until FROM user_downloads WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return time.strftime("%Y-%m-%d") <= row[0]
    return False

def ativar_premium(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    premium_date = time.strftime("%Y-%m-%d", time.localtime(time.time() + 30*24*60*60))
    c.execute("UPDATE user_downloads SET premium_until=? WHERE user_id=?", (premium_date, user_id))
    conn.commit()
    conn.close()

# ------------------- Mercado Pago -------------------
def gerar_pix_qrcode(email_usuario):
    payment_data = {
        "transaction_amount": 9.90,
        "description": "Acesso premium por 30 dias",
        "payment_method_id": "pix",
        "payer": {
            "email": email_usuario
        }
    }
    payment = sdk.payment().create(payment_data)
    qr_code_base64 = payment["response"]["point_of_interaction"]["transaction_data"]["qr_code_base64"]
    payment_id = payment["response"]["id"]
    return qr_code_base64, payment_id

def verificar_pagamento(payment_id):
    status = sdk.payment().get(payment_id)["response"]["status"]
    return status == "approved"

# ------------------- Handlers -------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! Envie um link para baixar vídeos. Limite: 3 downloads grátis/mês. Após isso, R$ 9,90 para acesso ilimitado por 30 dias.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not getattr(update, "message", None) or not update.message.text:
        return
    text = update.message.text.strip()
    url = URL_RE.search(text)
    if not url:
        return
    token = uuid.uuid4().hex
    confirm_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
         InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")]
    ])
    confirm_msg = await update.message.reply_text(f"Você quer baixar este link?\n{url.group(1)}", reply_markup=confirm_keyboard)
    PENDING[token] = {
        "url": url.group(1),
        "chat_id": update.message.chat_id,
        "from_user_id": update.message.from_user.id,
        "confirm_msg_id": confirm_msg.message_id
    }

# ✅ Callback atualizado
async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("dl:"):
        token = data.split("dl:", 1)[1]
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Pedido expirado.")
            return

        user_id = query.from_user.id

        # Verifica premium
        if is_premium(user_id):
            await query.edit_message_text("✅ Você é premium! Download liberado.")
            await start_download(entry["url"], entry["chat_id"])
            return

        # Verifica limite
        count = get_download_count(user_id)
        if count > 3:
            qr_code_base64, payment_id = gerar_pix_qrcode("email_do_usuario@example.com")
            await query.edit_message_text("⚠️ Limite atingido. Pague R$ 9,90 para acesso ilimitado por 30 dias.")
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=qr_code_base64)

            for _ in range(10):
                if verificar_pagamento(payment_id):
                    ativar_premium(user_id)
                    await context.bot.send_message(chat_id=query.message.chat_id, text="✅ Pagamento confirmado! Você tem acesso ilimitado por 30 dias.")
                    await start_download(entry["url"], entry["chat_id"])
                    return
                await asyncio.sleep(15)

            await context.bot.send_message(chat_id=query.message.chat_id, text="⏳ Pagamento não confirmado. Tente novamente.")
            return

        increment_download_count(user_id)
        await query.edit_message_text("Iniciando download...")
        await start_download(entry["url"], entry["chat_id"])

# ------------------- Função de download -------------------
async def start_download(url, chat_id):
    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "best[height<=720]+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "logger": LOG
    }
    try:
        await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))
        arquivos = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))]
        for path in arquivos:
            with open(path, "rb") as fh:
                await application.bot.send_video(chat_id=chat_id, video=fh)
    except Exception as e:
        await application.bot.send_message(chat_id=chat_id, text=f"Erro no download: {e}")

# ------------------- Registro de handlers -------------------
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True)
    update = Update.de_json(update_data, application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    return "ok"

@app.route("/")
def index():
    return "Bot rodando"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
