#!/usr/bin/env python3
"""
bot_with_cookies_railway.py

Bot Telegram via webhook para Railway, totalmente senior-ready:
- Detecta links no privado ou quando mencionado em grupo.
- Pergunta "quer baixar?" com botão.
- Inicia download via yt-dlp com barra de progresso.
- Envia vídeos, dividindo em partes se >50MB.
- Configura webhook automaticamente usando BASE_URL.
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
import requests
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

# ------------------- Logging -------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger("ytbot")

# ------------------- Token -------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido. Defina o secret TELEGRAM_BOT_TOKEN e redeploy.")
    sys.exit(1)
LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# ------------------- Flask app -------------------
app = Flask(__name__)

# ------------------- Telegram Application -------------------
application = ApplicationBuilder().token(TOKEN).build()
APP_LOOP = asyncio.new_event_loop()

def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

loop_thread = threading.Thread(target=_start_loop, args=(APP_LOOP,), daemon=True)
loop_thread.start()

# Inicializa Application no loop
try:
    fut = asyncio.run_coroutine_threadsafe(application.initialize(), APP_LOOP)
    fut.result(timeout=30)
    LOG.info("Application inicializada no loop de background.")
except Exception:
    LOG.exception("Falha ao inicializar Application no loop de background.")
    sys.exit(1)

# ------------------- Cookies -------------------
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
URL_RE = re.compile(r"(https?://[^\s]+)")
PENDING = {}  # token -> metadata (in-memory)

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
                if ent.type == "mention":
                    ent_text = msg.text[ent.offset : ent.offset + ent.length]
                    if ent_text.lower() == f"@{bot_username.lower()}":
                        return True
                elif ent.type == "text_mention" and getattr(ent.user, "id", None) == bot_id:
                    return True
        if msg.text and f"@{bot_username}" in msg.text:
            return True
    return False

# ------------------- Handlers -------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        payload = context.args[0]
        try:
            padding = "=" * (-len(payload) % 4)
            url = base64.urlsafe_b64decode(payload + padding).decode()
        except Exception:
            await update.message.reply_text("Payload inválido.")
            return
        token = uuid.uuid4().hex
        confirm_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
              InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")]]
        )
        confirm_msg = await update.message.reply_text(f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard)
        PENDING[token] = {"url": url, "chat_id": update.message.chat_id,
                          "from_user_id": update.message.from_user.id,
                          "confirm_msg_id": confirm_msg.message_id, "progress_msg": None}
        return
    await update.message.reply_text("Olá! Me envie um link do YouTube ou mencione-me com @bot + link.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not getattr(update, "message", None) or not update.message.text:
        return
    text = update.message.text.strip()
    chat_type = update.message.chat.type
    if chat_type != "private" and not is_bot_mentioned(update):
        return
    url = None
    if getattr(update.message, "entities", None):
        for ent in update.message.entities:
            if ent.type in ("url", "text_link"):
                url = getattr(ent, "url", None) or update.message.text[ent.offset:ent.offset+ent.length]
                break
    if not url:
        m = URL_RE.search(text)
        if m:
            url = m.group(1)
    if not url:
        if chat_type != "private" and is_bot_mentioned(update):
            try:
                await update.message.reply_text("Envie o link junto com a menção, ex: @MeuBot https://...")
            except Exception:
                pass
        return
    token = uuid.uuid4().hex
    confirm_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
          InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")]]
    )
    try:
        confirm_msg = await update.message.reply_text(f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard)
    except Exception:
        confirm_msg = await context.bot.send_message(chat_id=update.message.chat_id, text=f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard)
    PENDING[token] = {"url": url, "chat_id": update.message.chat_id,
                      "from_user_id": update.message.from_user.id,
                      "confirm_msg_id": confirm_msg.message_id, "progress_msg": None}

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("dl:"):
        token = data.split("dl:",1)[1]
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Pedido expirou ou inválido.")
            return
        if query.from_user.id != entry["from_user_id"]:
            await query.edit_message_text("Apenas quem solicitou pode confirmar.")
            return
        try:
            await query.edit_message_text("Iniciando download... 🎬")
        except Exception:
            pass
        progress_msg = await context.bot.send_message(chat_id=entry["chat_id"], text="📥 Baixando: 0% [────────────────────]")
        entry["progress_msg"] = {"chat_id": progress_msg.chat_id, "message_id": progress_msg.message_id}
        asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)
    elif data.startswith("cancel:"):
        token = data.split("cancel:",1)[1]
        entry = PENDING.pop(token, None)
        if not entry:
            await query.edit_message_text("Cancelamento expirou.")
            return
        await query.edit_message_text("Cancelado ✅")

# ------------------- Download -------------------
async def start_download_task(token: str):
    entry = PENDING.get(token)
    if not entry or not entry["progress_msg"]:
        return
    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry["progress_msg"]
    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
    last_percent = -1
    last_update_ts = time.time()
    WATCHDOG_TIMEOUT = 180

    def progress_hook(d):
        nonlocal last_percent,last_update_ts
        try:
            if d.get("status")=="downloading":
                downloaded = d.get("downloaded_bytes",0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    percent=int(downloaded*100/total)
                    if percent!=last_percent:
                        last_percent=percent
                        last_update_ts=time.time()
                        blocks=int(percent/5)
                        bar="█"*blocks+"─"*(20-blocks)
                        text=f"📥 Baixando: {percent}% [{bar}]"
                        try:
                            asyncio.run_coroutine_threadsafe(application.bot.edit_message_text(
                                text=text, chat_id=pm["chat_id"], message_id=pm["message_id"]
                            ), APP_LOOP)
                        except Exception:
                            pass
            elif d.get("status")=="finished":
                last_update_ts=time.time()
                try:
                    asyncio.run_coroutine_threadsafe(application.bot.edit_message_text(
                        text="✅ Download concluído, processando...", chat_id=pm["chat_id"], message_id=pm["message_id"]
                    ), APP_LOOP)
                except Exception:
                    pass
        except Exception:
            LOG.exception("Erro no progress_hook")

    ydl_opts={
        "outtmpl":outtmpl,"progress_hooks":[progress_hook],"quiet":False,"logger":LOG,
        "format":"best[height<=720]+bestaudio/best","merge_output_format":"mp4",
        "concurrent_fragment_downloads":1,"force_ipv4":True,"socket_timeout":30,"http_chunk_size":1048576,
        "retries":20,"fragment_retries":20,
        **({"cookiefile":COOKIE_PATH} if COOKIE_PATH else {})
    }
    try:
        await asyncio.to_thread(lambda: _run_ydl(ydl_opts,[url]))
    except Exception as e:
        LOG.exception("Erro no yt-dlp: %s", e)
        try:
            asyncio.run_coroutine_threadsafe(application.bot.edit_message_text(
                text=f"⚠️ Erro no download: {str(e)}", chat_id=pm["chat_id"], message_id=pm["message_id"]
            ), APP_LOOP)
        except Exception:
            pass
        PENDING.pop(token,None)
        return
    # envia arquivos
    arquivos=[f for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir,f))]
    if not arquivos:
        asyncio.run_coroutine_threadsafe(application.bot.edit_message_text(
            text="⚠️ Nenhum arquivo gerado.", chat_id=pm["chat_id"], message_id=pm["message_id"]
        ), APP_LOOP)
        PENDING.pop(token,None)
        return
    sent_any=False
    try:
        for f in arquivos:
            path=os.path.join(tmpdir,f)
            tamanho=os.path.getsize(path)
            if tamanho>50*1024*1024:
                partes_dir=os.path.join(tmpdir,"partes")
                os.makedirs(partes_dir,exist_ok=True)
                os.system(f'ffmpeg -y -i "{path}" -c copy -map 0 -fs 45M "{partes_dir}/part%03d.mp4"')
                for p in sorted(os.listdir(partes_dir)):
                    ppath=os.path.join(partes_dir,p)
                    try:
                        with open(ppath,"rb") as fh:
                            asyncio.run_coroutine_threadsafe(application.bot.send_video(chat_id=chat_id,video=fh),APP_LOOP)
                        sent_any=True
                    except Exception:
                        LOG.exception("Erro ao enviar parte %s",ppath)
            else:
                try:
                    with open(path,"rb") as fh:
                        asyncio.run_coroutine_threadsafe(application.bot.send_video(chat_id=chat_id,video=fh),APP_LOOP)
                    sent_any=True
                except Exception:
                    LOG.exception("Erro ao enviar arquivo %s",path)
    finally:
        # cleanup
        try:
            for root,dirs,files in os.walk(tmpdir,topdown=False):
                for name in files: os.remove(os.path.join(root,name))
                for name in dirs: os.rmdir(os.path.join(root,name))
            os.rmdir(tmpdir)
        except Exception:
            pass
    # update final
    try:
        text="✅ Download finalizado e enviado!" if sent_any else "⚠️ Falha ao enviar arquivo."
        asyncio.run_coroutine_threadsafe(application.bot.edit_message_text(
            text=text, chat_id=pm["chat_id"], message_id=pm["message_id"]
        ), APP_LOOP)
    except Exception:
        pass
    PENDING.pop(token,None)

def _run_ydl(options, urls):
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

# ------------------- Register Handlers -------------------
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# ------------------- Webhook -------------------
@app.route(f"/{TOKEN}",methods=["POST"])
def webhook():
    update_data=request.get_json(force=True)
    update=Update.de_json(update_data,application.bot)
    try:
        asyncio.run_coroutine_threadsafe(application.process_update(update),APP_LOOP)
    except Exception:
        LOG.exception("Falha ao process_update")
    return "ok"

@app.route("/")
def index():
    return "Bot rodando ✅"

# ------------------- Auto Set Webhook -------------------
BASE_URL=os.environ.get("BASE_URL")  # ex: https://meuapp.up.railway.app
if BASE_URL:
    try:
        r=requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={BASE_URL}/{TOKEN}")
        LOG.info("Webhook configurado: %s", r.text)
    except Exception:
        LOG.exception("Falha ao configurar webhook")

# ------------------- Run Flask -------------------
if __name__=="__main__":
    port=int(os.environ.get("PORT",10000))
    LOG.info("Iniciando Flask no port %d",port)
    app.run(host="0.0.0.0",port=port,debug=False,threaded=True)
