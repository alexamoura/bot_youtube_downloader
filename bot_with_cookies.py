#!/usr/bin/env python3
"""
Telegram bot (webhook) que:
- detecta links enviados diretamente pelo usuário,
- pergunta "quer baixar?" com botão,
- ao confirmar, inicia o download e mostra uma barra de progresso atualizada,
- envia partes se necessário (ffmpeg) e mostra mensagem final.

Requisitos/variáveis de ambiente:
- TELEGRAM_BOT_TOKEN  (obrigatório)
- YT_COOKIES_B64      (opcional; base64 do cookies.txt)
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

# Map temporário para callbacks: token -> {url, chat_id, from_user_id, confirm_msg_id, progress_msg}
PENDING = {}

URL_RE = re.compile(
    r"(https?://[^\s]+)"
)  # simplificação: pega qualquer substring começando com http(s):// até espaço

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


# ---------- Handlers ----------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! Me envie um link do YouTube e eu te pergunto se quer baixar.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta links na mensagem e envia confirmação com botão."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    # tenta extrair url por entidades primeiro (mais confiável)
    url = None
    if update.message.entities:
        for ent in update.message.entities:
            if ent.type in ("url", "text_link"):
                if ent.url:
                    url = ent.url
                else:
                    # entidade 'url' tem offset/length - extrai substring
                    url = update.message.text[ent.offset : ent.offset + ent.length]
                break

    # se não encontrou por entidades, usa regex simples
    if not url:
        m = URL_RE.search(text)
        if m:
            url = m.group(1)

    if not url:
        # nada pra fazer
        return

    # cria token temporário para callback
    token = uuid.uuid4().hex
    confirm_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
            ]
        ]
    )
    # envia mensagem de confirmação
    confirm_msg = await update.message.reply_text(
        f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard
    )

    # armazena no PENDING com dados relevantes
    PENDING[token] = {
        "url": url,
        "chat_id": update.message.chat_id,
        "from_user_id": update.message.from_user.id,
        "confirm_msg_id": confirm_msg.message_id,
        "progress_msg": None,  # preencher depois
    }
    # opcional: expirar depois de N minutos (não implementado aqui)


async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trata confirmações de download e cancelamentos."""
    query = update.callback_query
    await query.answer()  # remove "loading" no client
    data = query.data or ""
    if data.startswith("dl:"):
        token = data.split("dl:", 1)[1]
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Esse pedido expirou ou é inválido.")
            return
        # confirma usuário (apenas quem pediu pode confirmar)
        if query.from_user.id != entry["from_user_id"]:
            await query.edit_message_text("Apenas quem solicitou pode confirmar o download.")
            return

        # edita mensagem de confirmação para indicar início
        try:
            await query.edit_message_text("Iniciando download... 🎬")
        except Exception:
            pass

        # envia mensagem de progresso (será editada)
        progress_msg = await context.bot.send_message(chat_id=entry["chat_id"], text="📥 Baixando: 0% [----------]")
        entry["progress_msg"] = {"chat_id": progress_msg.chat_id, "message_id": progress_msg.message_id}

        # iniciar download em background (não bloquear o loop)
        asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)

    elif data.startswith("cancel:"):
        token = data.split("cancel:", 1)[1]
        entry = PENDING.pop(token, None)
        if not entry:
            await query.edit_message_text("Cancelamento: pedido já expirou.")
            return
        await query.edit_message_text("Cancelado ✅")


async def start_download_task(token: str):
    """Executa o download (em background thread) e atualiza a mensagem de progresso via APP_LOOP."""
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

    # prepara diretório temporário pro download
    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

        def progress_hook(d):
            try:
                status = d.get("status")
                if status == "downloading":
                    downloaded = d.get("downloaded_bytes", 0) or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    if total:
                        percent = int(downloaded * 100 / total)
                        # monta barra de progresso (20 blocos)
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
            "quiet": True,
            "format": "best[height<=720]+bestaudio/best",
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 2,
            "force_ipv4": True,
            "retries": 10,
            "fragment_retries": 10,
            **({"cookiefile": COOKIE_PATH} if COOKIE_PATH else {}),
        }

        try:
            # roda o download em thread (para não bloquear event loop)
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
            # remove pending
            PENDING.pop(token, None)
            return

        # procurar arquivo(s) gerados
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
            return

        # envia arquivos (se >50MB, divide)
        sent_any = False
        for f in arquivos:
            path = os.path.join(tmpdir, f)
            tamanho = os.path.getsize(path)
            if tamanho > 50 * 1024 * 1024:
                # split com ffmpeg em partes de ~45MB
                partes_dir = os.path.join(tmpdir, "partes")
                os.makedirs(partes_dir, exist_ok=True)
                cmd = f'ffmpeg -y -i "{path}" -c copy -map 0 -fs 45M "{partes_dir}/part%03d.mp4"'
                LOG.info("Split: %s", cmd)
                os.system(cmd)
                partes = sorted(os.listdir(partes_dir))
                for p in partes:
                    ppath = os.path.join(partes_dir, p)
                    try:
                        await asyncio.run_coroutine_threadsafe(
                            application.bot.send_video(chat_id=chat_id, video=open(ppath, "rb")), APP_LOOP
                        ).result()
                        sent_any = True
                    except Exception:
                        LOG.exception("Erro ao enviar parte %s", ppath)
            else:
                try:
                    # enviar vídeo
                    await asyncio.run_coroutine_threadsafe(
                        application.bot.send_video(chat_id=chat_id, video=open(path, "rb")), APP_LOOP
                    ).result()
                    sent_any = True
                except Exception:
                    LOG.exception("Erro ao enviar arquivo %s", path)

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

        # cleanup
        PENDING.pop(token, None)


def _run_ydl(options, urls):
    """Função blocking que roda yt_dlp (executada via asyncio.to_thread)."""
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)


# Handlers registration
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
# Mensagens de texto que não sejam comandos
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))


# Webhook endpoint (Render envia POST aqui)
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True)
    update = Update.de_json(update_data, application.bot)
    try:
        # agendar process_update no loop de background
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
