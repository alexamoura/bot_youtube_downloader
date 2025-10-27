import os
import telebot
from pytube import YouTube

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🎬 Envie o link do vídeo do YouTube que você quer baixar.")

@bot.message_handler(func=lambda msg: True)
def baixar_video(message):
    url = message.text
    try:
        bot.reply_to(message, "⬇️ Baixando vídeo, aguarde...")
        yt = YouTube(url)
        video = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
        video_path = video.download(filename="video.mp4")
        with open(video_path, 'rb') as f:
            bot.send_video(message.chat.id, f)
        os.remove(video_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Erro: {e}")

bot.polling()
