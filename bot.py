import os
import tempfile
import yt_dlp
import threading
import queue
import re
import uuid
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, CallbackContext, MessageHandler, Filters, CallbackQueryHandler

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = Bot(token=TOKEN)

app = Flask(__name__)
dispatcher = Dispatcher(bot=bot, update_queue=None, use_context=True)

job_queue = queue.Queue()
download_links = {}

# Regex para detectar links do YouTube
youtube_regex = re.compile(r"https?://(www\.)?(youtube\.com|youtu\.be)/[\w\-?=&%]+")

# Comando /start
def start(update: Update, context: CallbackContext):
    update.message.reply_text("Olá! Envie um link do YouTube para baixar o vídeo 🎥")

# Processamento do download
def process_download(update, context, url):
    msg = update.reply_text("📤 Preparando download...")

    with tempfile.TemporaryDirectory() as tmpdir:
        caminho_saida = os.path.join(tmpdir, "%(title)s.%(ext)s")

        ydl_opts = {
            'outtmpl': caminho_saida,
            'quiet': True,
            'format': 'best[height<=720]/best',
            'merge_output_format': 'mp4',
            'concurrent_fragment_downloads': 2,
            'force_ipv4': True,
            'retries': 10,
            'fragment_retries': 10,
            'noplaylist': True,
            'no_check_certificate': True,
            'cookies': 'cookies.txt',
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            update.reply_text(f"🚧 Erro no download: {str(e)}")
            return

        arquivos = os.listdir(tmpdir)
        if not arquivos:
            update.reply_text("🚧 Nenhum vídeo encontrado.")
            return

        arquivo = os.path.join(tmpdir, arquivos[0])
        tamanho = os.path.getsize(arquivo)

        if tamanho > 50 * 1024 * 1024:
            update.reply_text("🚧 O vídeo é muito grande para ser enviado pelo Telegram.")
            return

        with open(arquivo, "rb") as f:
            update.reply_video(video=f)

        update.reply_text("✅ Vídeo enviado com sucesso!")

# Worker da fila
def worker():
    while True:
        update, context, url = job_queue.get()
        try:
            process_download(update, context, url)
        finally:
            job_queue.task_done()

threading.Thread(target=worker, daemon=True).start()

# Mensagem com link
def handle_message(update: Update, context: CallbackContext):
    text = update.message.text
    match = youtube_regex.search(text)
    if match:
        url = match.group(0)
        uid = str(uuid.uuid4())[:8]
        download_links[uid] = url
        keyboard = [[InlineKeyboardButton("Sim, baixar", callback_data=f"download|{uid}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text(f"Você quer baixar este vídeo?\n{url}", reply_markup=reply_markup)

# Callback do botão
def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    if data.startswith("download|"):
        uid = data.split("|", 1)[1]
        url = download_links.get(uid)
        if not url:
            query.edit_message_text("🚫 Link não encontrado ou expirado.")
            return
        query.edit_message_text("⏳ Seu download foi adicionado à fila. Aguarde...")
        job_queue.put((query.message, context, url))

dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
dispatcher.add_handler(CallbackQueryHandler(button_callback))

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "ok"

@app.route("/")
def index():
    return "Bot está rodando com webhook!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
