#!/usr/bin/env python3
"""
bot_with_cookies.py

Telegram bot (webhook) que:
- detecta links enviados diretamente ou em grupo quando mencionado (@SeuBot + link),
- pergunta "quer baixar?" com botão,
- ao confirmar, inicia o download e mostra uma barra de progresso atualizada,
- envia partes se necessário (ffmpeg) e mostra mensagem final.

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
    LOG.error("TELEGRAM_BOT_TOKEN não definido. Defina o secret TELEGRAM_BOT_TOKEN e redeploy.")
    sys.exit(1)

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# Flask app
app = Flask(__name__)

# Construir a aplicação do telegram
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

# ---------- Helpers for mention detection ----------

def is_bot_mentioned(update: Update) -> bool:
    """
    Retorna True se a mensagem mencionar o bot (por @username) ou usar text_mention
    que aponte para o próprio bot.
    """
    try:
        bot_username = application.bot.username  # disponível após initialize()
        bot_id = application.bot.id
    except Exception:
        bot_username = None
        bot_id = None

    msg = getattr(update, "message", None)
    if not msg:
        return False

    if bot_username:
        # verifica entidades 'mention' e 'text_mention'
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
                    # entidade que inclui o usuário em si
                    if getattr(ent, "user", None) and getattr(ent.user, "id", None) == bot_id:
                        return True

        # fallback: checar se @username aparece no texto
        if msg.text and f"@{bot_username}" in msg.text:
            return True

    # também aceitar se houver text_mention direcionado ao bot sem username
    if getattr(msg, "entities", None):
        for ent in msg.entities:
            if getattr(ent, "type", "") == "text_mention":
                if getattr(ent, "user", None) and getattr(ent.user, "id", None) == bot_id:
                    return True
    return False

# ---------- Handlers ----------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start handler. Se vier com payload (deep link), tentamos iniciar o fluxo de confirmação
    automaticamente com o link contido no payload (base64 urlsafe).
    """
    # trata payload (context.args) vindo do deep link /start <payload>
    if context.args:
        payload = context.args[0]
        try:
            padding = "=" * (-len(payload) % 4)
            url = base64.urlsafe_b64decode(payload + padding).decode()
        except Exception:
            await update.message.reply_text("Payload inválido.")
            return

        # cria token e fluxo de confirmação igual ao do handle_message
        token = uuid.uuid4().hex
        confirm_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
                    InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
                ]
            ]
        )
        if chat_type in ['group', 'supergroup']:
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f'Você quer baixar este link?
{url}',
            reply_markup=confirm_keyboard
        )
        await update.message.reply_text('✅ Link recebido! Verifique sua conversa privada comigo para confirmar o download.')
    except Exception:
        await update.message.reply_text('⚠️ Não consegui enviar mensagem privada. Verifique se você iniciou uma conversa comigo.')
else:
    confirm_msg = await update.message.reply_text(f'Você quer baixar este link?
{url}', reply_markup=confirm_keyboard)
        PENDING[token] = {
            "url": url,
            "chat_id": update.message.chat_id,
            "from_user_id": update.message.from_user.id,
            "confirm_msg_id": confirm_msg.message_id,
            "progress_msg": None,
        }
        return

    # comportamento padrão
    await update.message.reply_text("Olá! Me envie um link do YouTube (ou mencione-me com @seubot + link) e eu te pergunto se quer baixar.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Detecta links na mensagem e envia confirmação com botão.
    Processa quando:
     - chat privado (qualquer link enviado por DM), ou
     - chat de grupo quando o bot for mencionado (ex: @MeuBot <link>)
    """
    if not getattr(update, "message", None) or not update.message.text:
        return

    text = update.message.text.strip()
    
    user_id = update.message.from_user.id
    if chat_type == 'private' and user_id != 6766920288:
        return
    if chat_type != 'private' and not is_bot_mentioned(update):
        return
chat_type = update.message.chat.type  # 'private', 'group', 'supergroup', 'channel'

    # se não for privado, só processa quando o bot for mencionado
    if chat_type != "private":
        if not is_bot_mentioned(update):
            return

    # extrair URL por entidades primeiro
    url = None
    if getattr(update.message, "entities", None):
        for ent in update.message.entities:
            if ent.type in ("url", "text_link"):
                if getattr(ent, "url", None):
                    url = ent.url
                else:
                    try:
                        url = update.message.text[ent.offset : ent.offset + ent.length]
                    except Exception:
                        url = None
                break

    if not url:
        m = URL_RE.search(text)
        if m:
            url = m.group(1)

    if not url:
        # se mencionou o bot sem link, orientar
        if chat_type != "private" and is_bot_mentioned(update):
            try:
                await update.message.reply_text("Envie o link do vídeo junto com a menção, por exemplo: @MeuBot https://...")
            except Exception:
                pass
        return

    token = uuid.uuid4().hex
    confirm_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
            ]
        ]
    )

    try:
        if chat_type in ['group', 'supergroup']:
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f'Você quer baixar este link?
{url}',
            reply_markup=confirm_keyboard
        )
        await update.message.reply_text('✅ Link recebido! Verifique sua conversa privada comigo para confirmar o download.')
    except Exception:
        await update.message.reply_text('⚠️ Não consegui enviar mensagem privada. Verifique se você iniciou uma conversa comigo.')
else:
    confirm_msg = await update.message.reply_text(f'Você quer baixar este link?
{url}', reply_markup=confirm_keyboard)
    except Exception:
        confirm_msg = await context.bot.send_message(chat_id=update.message.chat_id, text=f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard)

    PENDING[token] = {
        "url": url,
        "chat_id": update.message.chat_id,
        "from_user_id": update.message.from_user.id,
        "confirm_msg_id": confirm_msg.message_id,
        "progress_msg": None,
    }

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trata confirmações de download e cancelamentos."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("dl:"):
        token = data.split("dl:", 1)[1]
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Esse pedido expirou ou é inválido.")
            return
        # Proteção: apenas quem originou pode confirmar
        if query.from_user.id != entry["from_user_id"]:
            await query.edit_message_text("Apenas quem solicitou pode confirmar o download.")
            return

        try:
            await query.edit_message_text("Iniciando download... 🎬")
        except Exception:
            pass

        progress_msg = await context.bot.send_message(chat_id=entry["chat_id"], text="📥 Baixando: 0% [────────────────────]")
        entry["progress_msg"] = {"chat_id": progress_msg.chat_id, "message_id": progress_msg.message_id}

        # iniciar download em background no APP_LOOP
        asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)

    elif data.startswith("cancel:"):
        token = data.split("cancel:", 1)[1]
        entry = PENDING.pop(token, None)
        if not entry:
            await query.edit_message_text("Cancelamento: pedido já expirou.")
            return
        await query.edit_message_text("Cancelado ✅")

# ---------- Download task & helpers ----------

async def start_download_task(token: str):
    """Executa o download e atualiza a mensagem de progresso."""
    entry = PENDING.get(token)
    if not entry:
        LOG.info("start_download_task: token não encontrado")
        return

    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry["progress_msg"]
    if not pm:
        LOG.info("start_download_task: progress_msg não encontrado")
        return

    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

    # estado local do progresso
    last_percent = -1
    last_update_ts = time.time()
    WATCHDOG_TIMEOUT = 180  # segundos sem progresso para notificar

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
        # roda em thread para não bloquear o event loop; o progresso é repassado via progress_hook
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

    # watchdog: se não houve progresso por WATCHDOG_TIMEOUT, notificar
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

    sent_any = False
    try:
        for f in arquivos:
            path = os.path.join(tmpdir, f)
            tamanho = os.path.getsize(path)
            if tamanho > 50 * 1024 * 1024:
                partes_dir = os.path.join(tmpdir, "partes")
                os.makedirs(partes_dir, exist_ok=True)
                cmd = f'ffmpeg -y -i "{path}" -c copy -map 0 -fs 45M "{partes_dir}/part%03d.mp4"'
                LOG.info("Split: %s", cmd)
                os.system(cmd)
                partes = sorted(os.listdir(partes_dir))
                for p in partes:
                    ppath = os.path.join(partes_dir, p)
                    try:
                        with open(ppath, "rb") as fh:
                            await application.bot.send_video(chat_id=chat_id, video=fh)
                        sent_any = True
                    except Exception:
                        LOG.exception("Erro ao enviar parte %s", ppath)
            else:
                try:
                    with open(path, "rb") as fh:
                        await application.bot.send_video(chat_id=chat_id, video=fh)
                    sent_any = True
                except Exception:
                    LOG.exception("Erro ao enviar arquivo %s", path)
    finally:
        # cleanup arquivos temporários
        try:
            for root, dirs, files in os.walk(tmpdir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(tmpdir)
        except Exception:
            pass

    # atualizar mensagem de progresso para finalizado
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


def _run_ydl(options, urls):
    """Função blocking que roda yt_dlp (executada via asyncio.to_thread)."""
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)


# Handlers registration
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))


# Webhook endpoint (Render envia POST aqui)
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
