
import os
import tempfile
import asyncio
import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

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
            if d['status'] == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                if total != 0:
                    percent = int(downloaded * 100 / total)
                    try:
                        asyncio.run_coroutine_threadsafe(
                            msg.edit_text(f"📥 Baixando vídeo: {percent}%"),
                            context.application.loop
                        )
                    except:
                        pass
            elif d['status'] == 'finished':
                try:
                    asyncio.run_coroutine_threadsafe(
                        msg.edit_text("✅ Download concluído, enviando vídeo..."),
                        context.application.loop
                    )
                except:
                    pass

        ydl_opts = {
            'outtmpl': caminho_saida,
            'progress_hooks': [progress_hook],
            'quiet': True,
            'format': 'best[height<=720]+bestaudio/best',
            'merge_output_format': 'mp4',
            'concurrent_fragment_downloads': 2,
            'force_ipv4': True,
            'retries': 10,
            'fragment_retries': 10
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            await update.message.reply_text(f"⚠️ Erro no download: {str(e)}")
            return

        arquivos = os.listdir(tmpdir)
        if not arquivos:
            await update.message.reply_text("⚠️ Nenhum vídeo encontrado.")
            return

        arquivo = os.path.join(tmpdir, arquivos[0])
        tamanho = os.path.getsize(arquivo)

        if tamanho > 50 * 1024 * 1024:
            partes_dir = os.path.join(tmpdir, "partes")
            os.makedirs(partes_dir, exist_ok=True)
            os.system(f'ffmpeg -i "{arquivo}" -c copy -map 0 -fs 45M "{partes_dir}/part%03d.mp4"')

            partes = sorted(os.listdir(partes_dir))
            for p in partes:
                caminho_parte = os.path.join(partes_dir, p)
                with open(caminho_parte, "rb") as f:
                    await update.message.reply_video(video=f)
            await update.message.reply_text("✅ Todas as partes enviadas com sucesso!")
            return

        with open(arquivo, "rb") as f:
            await update.message.reply_video(video=f)

        await update.message.reply_text("✅ Vídeo enviado com sucesso!")

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("download", download))

print("Bot rodando... ✅")
app.run_polling()
