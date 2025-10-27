
import os
import logging
from flask import Flask, request
import telebot
from pytube import YouTube

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🎬 Envie o link do vídeo do YouTube que você quer baixar.")

@bot.message_handler(func=lambda msg: True)
def baixar_video(message):
    url = message.text
    if "youtube.com" not in url and "youtu.be" not in url:
        bot.reply_to(message, "❗ Por favor, envie um link válido do YouTube.")
        return
    try:
        bot.reply_to(message, "⬇️ Baixando vídeo, aguarde...")
        yt = YouTube(url)
        video = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
        video_path = video.download(filename="video.mp4")
        with open(video_path, 'rb') as f:
            bot.send_video(message.chat.id, f)
        os.remove(video_path)
    except Exception as e:
        logging.error(f"Erro ao baixar vídeo: {e}")
        bot.reply_to(message, "❌ Ocorreu um erro ao tentar baixar o vídeo.")

@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "", 200

@app.route("/")
def index():
    return "Bot está rodando com Webhook!"

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
