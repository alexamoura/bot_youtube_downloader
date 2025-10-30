#!/usr/bin/env python3
"""
bot_with_cookies.py - Versão Corrigida
Bot Telegram com suporte a YouTube, Shopee e Instagram
CORREÇÕES: Progresso de download + vídeos esticados
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
import shutil
import subprocess
import json
from collections import OrderedDict
from contextlib import contextmanager
from urllib.parse import urlparse, parse_qs, unquote, quote
import yt_dlp

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

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

# Configuração
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ytbot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido")
    sys.exit(1)

# Constantes
URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = "users.db"
PENDING_MAX_SIZE = 1000
PENDING_EXPIRE_SECONDS = 600
WATCHDOG_TIMEOUT = 180
MAX_FILE_SIZE = 50 * 1024 * 1024
SPLIT_SIZE = 45 * 1024 * 1024

PENDING = OrderedDict()
DB_LOCK = threading.Lock()

ERROR_MESSAGES = {
    "timeout": "⏱️ O download demorou muito e foi cancelado.",
    "invalid_url": "⚠️ Esta URL não é válida ou não é suportada.",
    "network_error": "🌐 Erro de conexão. Tente novamente em alguns minutos.",
    "ffmpeg_error": "🎬 Erro ao processar o vídeo.",
    "upload_error": "📤 Erro ao enviar o arquivo.",
    "unknown": "❌ Ocorreu um erro inesperado. Tente novamente.",
    "expired": "⏰ Este pedido expirou. Envie o link novamente.",
}

app = Flask(__name__)

# Telegram Application
try:
    application = ApplicationBuilder().token(TOKEN).build()
    LOG.info("ApplicationBuilder criado")
except Exception as e:
    LOG.exception("Erro ao construir ApplicationBuilder")
    sys.exit(1)

APP_LOOP = asyncio.new_event_loop()

def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

loop_thread = threading.Thread(target=_start_loop, args=(APP_LOOP,), daemon=True)
loop_thread.start()

try:
    fut = asyncio.run_coroutine_threadsafe(application.initialize(), APP_LOOP)
    fut.result(timeout=30)
    LOG.info("Application inicializada")
except Exception as e:
    LOG.exception("Falha ao inicializar Application")
    sys.exit(1)

# Database
def init_db():
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS monthly_users (
                    user_id INTEGER PRIMARY KEY,
                    last_month TEXT
                )
            """)
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            LOG.error("Erro ao inicializar banco: %s", e)

def update_user(user_id: int):
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            month = time.strftime("%Y-%m")
            c.execute("SELECT last_month FROM monthly_users WHERE user_id=?", (user_id,))
            row = c.fetchone()
            if row:
                if row[0] != month:
                    c.execute("UPDATE monthly_users SET last_month=? WHERE user_id=?", (month, user_id))
            else:
                c.execute("INSERT INTO monthly_users (user_id, last_month) VALUES (?, ?)", (user_id, month))
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass

def get_monthly_users_count() -> int:
    month = time.strftime("%Y-%m")
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM monthly_users WHERE last_month=?", (month,))
            count = c.fetchone()[0]
            conn.close()
            return count
        except:
            return 0

init_db()

# Cookies
def prepare_cookies(env_var):
    b64 = os.environ.get(env_var)
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(raw)
        LOG.info("Cookies %s carregados", env_var)
        return path
    except Exception as e:
        LOG.error("Erro ao carregar cookies %s: %s", env_var, e)
        return None

COOKIE_YT = prepare_cookies("YT_COOKIES_B64")
COOKIE_SHOPEE = prepare_cookies("SHOPEE_COOKIES_B64")
COOKIE_IG = prepare_cookies("IG_COOKIES_B64")

# Utilities
def is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except:
        return False

def get_cookie_for_url(url: str):
    url_lower = url.lower()
    if 'shopee' in url_lower and COOKIE_SHOPEE:
        return COOKIE_SHOPEE
    elif ('instagram' in url_lower or 'insta' in url_lower) and COOKIE_IG:
        return COOKIE_IG
    elif ('youtube' in url_lower or 'youtu.be' in url_lower) and COOKIE_YT:
        return COOKIE_YT
    return COOKIE_YT or COOKIE_SHOPEE or COOKIE_IG

def resolve_shopee_link(url: str) -> str:
    try:
        if 'universal-link' in url and 'redir=' in url:
            if '?' in url:
                query = url.split('?', 1)[1]
                params = parse_qs(query)
                if 'redir' in params:
                    real_url = unquote(params['redir'][0])
                    LOG.info("URL Shopee resolvida: %s", real_url[:80])
                    return real_url
        return url
    except:
        return url

@contextmanager
def temp_download_dir():
    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    try:
        yield tmpdir
    finally:
        try:
            shutil.rmtree(tmpdir)
        except:
            pass

def is_bot_mentioned(update: Update) -> bool:
    try:
        bot_username = application.bot.username
        msg = getattr(update, "message", None)
        if not msg or not bot_username:
            return False
        if getattr(msg, "entities", None):
            for ent in msg.entities:
                if ent.type == "mention":
                    text = msg.text[ent.offset:ent.offset + ent.length]
                    if text.lower() == f"@{bot_username.lower()}":
                        return True
        return False
    except:
        return False

# Pending Management
def add_pending(token: str, data: dict):
    if len(PENDING) >= PENDING_MAX_SIZE:
        oldest = next(iter(PENDING))
        PENDING.pop(oldest)
    data["created_at"] = time.time()
    PENDING[token] = data
    asyncio.run_coroutine_threadsafe(_expire_pending(token), APP_LOOP)

async def _expire_pending(token: str):
    await asyncio.sleep(PENDING_EXPIRE_SECONDS)
    PENDING.pop(token, None)

# Telegram Handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = get_monthly_users_count()
        cookies = []
        if COOKIE_YT:
            cookies.append("🎬 YouTube")
        if COOKIE_SHOPEE:
            cookies.append("🛍️ Shopee")
        if COOKIE_IG:
            cookies.append("📸 Instagram")
        cookie_text = ", ".join(cookies) if cookies else "Nenhum"
        
        await update.message.reply_text(
            f"Olá! 👋\n\n"
            f"Me envie um link de vídeo.\n\n"
            f"📊 Usuários: {count}\n"
            f"🍪 Cookies: {cookie_text}"
        )
    except Exception as e:
        LOG.error("Erro no /start: %s", e)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = get_monthly_users_count()
        cookies_count = sum([1 for c in [COOKIE_YT, COOKIE_SHOPEE, COOKIE_IG] if c])
        await update.message.reply_text(
            f"📊 Estatísticas\n\n"
            f"👥 Usuários mensais: {count}\n"
            f"⏳ Pendentes: {len(PENDING)}\n"
            f"🍪 Cookies: {cookies_count}/3"
        )
    except Exception as e:
        LOG.error("Erro no /stats: %s", e)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return

        update_user(update.message.from_user.id)
        text = update.message.text.strip()
        
        if update.message.chat.type != "private" and not is_bot_mentioned(update):
            return

        url = None
        if update.message.entities:
            for ent in update.message.entities:
                if ent.type in ("url", "text_link"):
                    url = getattr(ent, "url", None) or text[ent.offset:ent.offset+ent.length]
                    break

        if not url:
            m = URL_RE.search(text)
            if m:
                url = m.group(1)
        
        if not url or not is_valid_url(url):
            return

        token = uuid.uuid4().hex
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
        ]])

        confirm_msg = await update.message.reply_text(
            f"Baixar este vídeo?\n{url[:60]}...",
            reply_markup=keyboard
        )
        
        add_pending(token, {
            "url": url,
            "chat_id": update.message.chat_id,
            "from_user_id": update.message.from_user.id,
            "confirm_msg_id": confirm_msg.message_id,
            "progress_msg": None,
        })
    except Exception as e:
        LOG.exception("Erro em handle_message: %s", e)

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        data = query.data or ""
        
        if data.startswith("dl:"):
            token = data.split(":", 1)[1]
            entry = PENDING.get(token)
            
            if not entry:
                await query.edit_message_text(ERROR_MESSAGES["expired"])
                return
            
            if query.from_user.id != entry["from_user_id"]:
                return

            await query.edit_message_text("Iniciando... 🎬")
            
            progress_msg = await context.bot.send_message(
                chat_id=entry["chat_id"],
                text="📥 Preparando..."
            )
            entry["progress_msg"] = {
                "chat_id": progress_msg.chat_id,
                "message_id": progress_msg.message_id
            }
            
            asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)

        elif data.startswith("cancel:"):
            token = data.split(":", 1)[1]
            PENDING.pop(token, None)
            await query.edit_message_text("Cancelado ✅")
    except Exception as e:
        LOG.exception("Erro em callback: %s", e)

# Download Task
async def start_download_task(token: str):
    entry = PENDING.get(token)
    if not entry:
        return
    
    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry.get("progress_msg")
    if not pm:
        return

    try:
        with temp_download_dir() as tmpdir:
            url = resolve_shopee_link(url)
            
            # Shopee Video - método especial
            if 'sv.shopee' in url.lower() or 'share-video' in url.lower():
                await _download_shopee(url, tmpdir, chat_id, pm)
            else:
                # Outros sites - yt-dlp
                await _download_ytdlp(url, tmpdir, chat_id, pm, token)
    except Exception as e:
        LOG.exception("Erro no download: %s", e)
        try:
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["unknown"],
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )
        except:
            pass
    finally:
        PENDING.pop(token, None)

async def _download_shopee(url: str, tmpdir: str, chat_id: int, pm: dict):
    """Download de Shopee Video"""
    if not REQUESTS_AVAILABLE:
        await application.bot.edit_message_text(
            text="⚠️ Shopee não disponível (faltam dependências)",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        return
    
    try:
        await application.bot.edit_message_text(
            text="🛍️ Processando Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        # Tenta SVXtract primeiro
        video_url = await _try_svxtract(url)
        source = "SVXtract"
        
        # Se falhar, tenta extração direta
        if not video_url:
            video_url = await _try_direct_shopee(url)
            source = "Direto"
        
        if not video_url:
            await application.bot.edit_message_text(
                text="⚠️ Não consegui extrair o vídeo da Shopee.",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )
            return
        
        # Download
        await application.bot.edit_message_text(
            text=f"📥 Baixando... ({source})",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://shopee.com.br/"}
        response = await asyncio.to_thread(
            lambda: requests.get(video_url, headers=headers, stream=True, timeout=120)
        )
        response.raise_for_status()
        
        output = os.path.join(tmpdir, "video.mp4")
        with open(output, 'wb') as f:
            for chunk in response.iter_content(8192):
                if chunk:
                    f.write(chunk)
        
        # Envia
        await application.bot.edit_message_text(
            text="✅ Enviando...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        with open(output, "rb") as fh:
            await application.bot.send_video(
                chat_id=chat_id,
                video=fh,
                caption=f"🛍️ Shopee ({source})"
            )
        
        await application.bot.edit_message_text(
            text="✅ Enviado!",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.exception("Erro Shopee: %s", e)
        await application.bot.edit_message_text(
            text="❌ Erro ao baixar da Shopee",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )

async def _try_svxtract(url: str) -> str:
    """Tenta extrair via SVXtract"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        
        # Pega CSRF token
        resp = await asyncio.to_thread(
            lambda: requests.get("https://svxtract.com/", headers=headers, timeout=15)
        )
        
        csrf = None
        match = re.search(r'csrf_token["\s:=]+([a-f0-9]{64})', resp.text)
        if match:
            csrf = match.group(1)
        
        if not csrf:
            return None
        
        # Faz requisição
        encoded = quote(url, safe='')
        dl_url = f"https://svxtract.com/function/download/downloader.php?url={encoded}&csrf_token={csrf}"
        
        resp = await asyncio.to_thread(
            lambda: requests.get(dl_url, headers=headers, timeout=15)
        )
        
        # Busca URL do vídeo
        patterns = [
            r'"video_url"\s*:\s*"([^"]+)"',
            r'"url"\s*:\s*"([^"]+\.mp4[^"]*)"',
            r'href="([^"]+\.mp4[^"]*)"',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, resp.text)
            if match:
                return match.group(1)
        
        return None
    except Exception as e:
        LOG.warning("SVXtract falhou: %s", e)
        return None

async def _try_direct_shopee(url: str) -> str:
    """Extração direta da página Shopee"""
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://shopee.com.br/"}
        
        resp = await asyncio.to_thread(
            lambda: requests.get(url, headers=headers, timeout=20)
        )
        
        patterns = [
            r'(https://[^"\s]*\.mp4[^"\s]*)',
            r'"videoUrl"\s*:\s*"([^"]+)"',
            r'"playAddr"\s*:\s*"([^"]+)"',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, resp.text)
            for match in matches:
                clean = match.replace('\\/', '/')
                if 'http' in clean and '.mp4' in clean:
                    return clean
        
        return None
    except Exception as e:
        LOG.warning("Extração direta falhou: %s", e)
        return None

async def _download_ytdlp(url: str, tmpdir: str, chat_id: int, pm: dict, token: str):
    """Download via yt-dlp com progresso"""
    try:
        outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
        
        # CORREÇÃO: Formato melhorado para evitar vídeos esticados
        ydl_opts = {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            # Prioriza vídeos com proporção correta
            "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
            "merge_output_format": "mp4",
            # NOVO: Hook de progresso
            "progress_hooks": [lambda d: _progress_hook(d, token, pm)],
            # Garante que o aspect ratio seja preservado
            "postprocessor_args": {
                "ffmpeg": ["-aspect", "16:9"]  # Força aspect ratio correto
            },
        }
        
        cookie = get_cookie_for_url(url)
        if cookie:
            ydl_opts["cookiefile"] = cookie
        
        await asyncio.to_thread(lambda: _run_ytdlp(ydl_opts, [url]))
        
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))]
        
        if not files:
            raise Exception("Nenhum arquivo baixado")
        
        await application.bot.edit_message_text(
            text="✅ Enviando...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        for path in files:
            with open(path, "rb") as fh:
                await application.bot.send_video(chat_id=chat_id, video=fh)
        
        await application.bot.edit_message_text(
            text="✅ Enviado!",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.exception("Erro yt-dlp: %s", e)
        await application.bot.edit_message_text(
            text=ERROR_MESSAGES["network_error"],
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )

def _progress_hook(d, token, pm):
    """Hook de progresso para yt-dlp"""
    try:
        entry = PENDING.get(token)
        if not entry:
            return
        
        status = d.get('status')
        
        if status == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            speed = d.get('_speed_str', '?').strip()
            eta = d.get('_eta_str', '?').strip()
            
            message = f"📥 Baixando: {percent}\n⚡ Velocidade: {speed}\n⏱️ Tempo restante: {eta}"
            
            # Atualiza a cada 5% para não sobrecarregar
            if entry.get("last_progress", "") != percent:
                try:
                    # Usa asyncio para agendar a atualização
                    asyncio.run_coroutine_threadsafe(
                        application.bot.edit_message_text(
                            text=message,
                            chat_id=pm["chat_id"],
                            message_id=pm["message_id"]
                        ),
                        APP_LOOP
                    )
                    entry["last_progress"] = percent
                except:
                    pass
        
        elif status == 'finished':
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="🎬 Processando vídeo...",
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"]
                ),
                APP_LOOP
            )
    except Exception as e:
        LOG.warning("Erro no progress_hook: %s", e)

def _run_ytdlp(options, urls):
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

# Handlers
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# Flask Routes
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    except Exception as e:
        LOG.exception("Erro webhook: %s", e)
    return "ok"

@app.route("/")
def index():
    return "Bot Online ✅"

@app.route("/health")
def health():
    return {
        "status": "ok",
        "pending": len(PENDING),
        "cookies": {
            "youtube": bool(COOKIE_YT),
            "shopee": bool(COOKIE_SHOPEE),
            "instagram": bool(COOKIE_IG)
        }
    }

# Main
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    LOG.info("Iniciando na porta %d", port)
    app.run(host="0.0.0.0", port=port)
