import os
import logging
import telebot
from pytube import YouTube

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("TOKEN_KEY")
bot = telebot.TeleBot(BOT_TOKEN)

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

bot.infinity_polling()
