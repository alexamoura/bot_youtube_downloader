#!/usr/bin/env python3
import os
import tempfile
import asyncio
import base64
import logging
import yt_dlp
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Logging básico
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger("ytbot")

# Telegram token (defina TELEGRAM_BOT_TOKEN no Render)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.warning("TELEGRAM_BOT_TOKEN não definido. O bot não funcionará sem ele.")

app = Flask(__name__)
application = ApplicationBuilder().token(TOKEN).build()

# Lê cookies em base64 da variável YT_COOKIES_B64 e grava em arquivo temporário
def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("Nenhuma variável %s encontrada — rodando sem cookies.", env_var)
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        LOG.exception("Falha ao decodificar %s: %s", env_var, e)
        return None

    fd, path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except Exception as e:
        LOG.exception("Falha ao escrever cookies em %s: %s", path, e)
        return None

    LOG.info("Cookies gravados em %s", path)
    return path

COOKIE_PATH = prepare_cookies_from_env()

# Handlers do Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! Envie /download <link> para baixar um vídeo permitido 🎥")

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso correto: /download <link>")
        return

    url = context.args[0]
    msg = await update.message.reply_text("📥 Preparando download...")

    with tempfile.TemporaryDirectory() as tmpdir:
        caminho_saida = os.path.join(tmpdir, "%(title)s.%(ext)s")

        def progress_hook(d):
            # d pode conter 'status' = downloading, finished, etc.
            try:
                if d.get('status') == 'downloading':
                    downloaded = d.get('downloaded_bytes', 0) or 0
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    if total:
                        percent = int(downloaded * 100 / total)
                        try:
                            asyncio.run_coroutine_threadsafe(
                                msg.edit_text(f"📥 Baixando vídeo: {percent}%"),
                                context.application.loop
                            )
                        except Exception:
                            pass
                elif d.get('status') == 'finished':
                    try:
                        asyncio.run_coroutine_threadsafe(
                            msg.edit_text("✅ Download concluído, enviando vídeo..."),
                            context.application.loop
                        )
                    except Exception:
                        pass
            except Exception:
                LOG.exception("Erro no progress_hook")

        ydl_opts = {
            'outtmpl': caminho_saida,
            'progress_hooks': [progress_hook],
            'quiet': True,
            'format': 'best[height<=720]+bestaudio/best',
            'merge_output_format': 'mp4',
            'concurrent_fragment_downloads': 2,
            'force_ipv4': True,
            'retries': 10,
            'fragment_retries': 10,
            # use cookiefile se COOKIE_PATH estiver definido:
            **({'cookiefile': COOKIE_PATH} if COOKIE_PATH else {}),
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            LOG.exception("Erro no yt-dlp")
            await update.message.reply_text(f"⚠️ Erro no download: {str(e)}")
            return

        arquivos = os.listdir(tmpdir)
        if not arquivos:
            await update.message.reply_text("⚠️ Nenhum vídeo encontrado.")
            return

        # pega o primeiro arquivo - dependendo do template podem existir arquivos auxiliares
        arquivo = None
        for f in arquivos:
            if f.lower().endswith((".mp4", ".mkv", ".webm")):
                arquivo = os.path.join(tmpdir, f)
                break
        if not arquivo:
            arquivo = os.path.join(tmpdir, arquivos[0])

        tamanho = os.path.getsize(arquivo)

        # Se maior que 50MB, divide em partes (usa ffmpeg)
        if tamanho > 50 * 1024 * 1024:
            partes_dir = os.path.join(tmpdir, "partes")
            os.makedirs(partes_dir, exist_ok=True)
            # Atenção: depende de ffmpeg instalado no ambiente
            cmd = f'ffmpeg -y -i "{arquivo}" -c copy -map 0 -fs 45M "{partes_dir}/part%03d.mp4"'
            LOG.info("Executando split: %s", cmd)
            os.system(cmd)

            partes = sorted(os.listdir(partes_dir))
            for p in partes:
                caminho_parte = os.path.join(partes_dir, p)
                with open(caminho_parte, "rb") as f:
                    await update.message.reply_video(video=f)
            await update.message.reply_text("✅ Todas as partes enviadas com sucesso!")
            return

        # arquivo pequeno: envia direto
        with open(arquivo, "rb") as f:
            await update.message.reply_video(video=f)

        await update.message.reply_text("✅ Vídeo enviado com sucesso!")

# registra handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("download", download))

# Webhook endpoint para o Telegram (use: https://<seu-servico>/{TOKEN})
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True)
    update = Update.de_json(update_data, application.bot)

    async def process():
        await application.process_update(update)

    # processa o update na loop do asyncio
    asyncio.run(process())
    return "ok"

@app.route("/")
def index():
    return "Bot está rodando com webhook!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # rodar Flask apenas para desenvolvimento; em produção o Render usa gunicorn
    app.run(host="0.0.0.0", port=port)
