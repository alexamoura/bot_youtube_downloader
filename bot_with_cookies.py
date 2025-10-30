#!/usr/bin/env python3
"""
bot_with_cookies.py - Versão Melhorada

Telegram bot (webhook) com suporte a múltiplos cookies
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
from collections import OrderedDict
from contextlib import contextmanager
from urllib.parse import urlparse, parse_qs, unquote
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ytbot")

# Token
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido.")
    sys.exit(1)

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# Constantes
URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = "users.db"
PENDING_MAX_SIZE = 1000
PENDING_EXPIRE_SECONDS = 600
WATCHDOG_TIMEOUT = 180
MAX_FILE_SIZE = 50 * 1024 * 1024
SPLIT_SIZE = 45 * 1024 * 1024

# Estado global
PENDING = OrderedDict()
DB_LOCK = threading.Lock()

# Mensagens de erro
ERROR_MESSAGES = {
    "timeout": "⏱️ O download demorou muito e foi cancelado.",
    "invalid_url": "⚠️ Esta URL não é válida ou não é suportada.",
    "file_too_large": "📦 O arquivo é muito grande para processar.",
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
    LOG.info("ApplicationBuilder criado com sucesso.")
except Exception as e:
    LOG.exception("Erro ao construir ApplicationBuilder")
    sys.exit(1)

# Loop asyncio
APP_LOOP = asyncio.new_event_loop()

def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

LOG.info("Iniciando event loop de background...")
loop_thread = threading.Thread(target=_start_loop, args=(APP_LOOP,), daemon=True)
loop_thread.start()

try:
    fut = asyncio.run_coroutine_threadsafe(application.initialize(), APP_LOOP)
    fut.result(timeout=30)
    LOG.info("Application inicializada.")
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
            LOG.info("Banco de dados inicializado.")
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
        except sqlite3.Error as e:
            LOG.error("Erro ao atualizar user: %s", e)

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
        except sqlite3.Error:
            return 0

init_db()

# Cookies
def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("Nenhuma variável %s encontrada.", env_var)
        return None
    
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        LOG.error("Falha ao decodificar %s: %s", env_var, e)
        return None

    try:
        fd, path = tempfile.mkstemp(prefix=f"{env_var.lower()}_", suffix=".txt")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(raw)
        LOG.info("Cookies %s gravados em %s", env_var, path)
        return path
    except Exception as e:
        LOG.error("Falha ao escrever cookies %s: %s", env_var, e)
        return None

# Carrega cookies de diferentes plataformas
COOKIE_YT = prepare_cookies_from_env("YT_COOKIES_B64")
COOKIE_SHOPEE = prepare_cookies_from_env("SHOPEE_COOKIES_B64")
COOKIE_IG = prepare_cookies_from_env("IG_COOKIES_B64")

# Utilities
def is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False

def get_cookie_for_url(url: str):
    """Retorna o arquivo de cookie apropriado baseado na URL."""
    url_lower = url.lower()
    
    if 'shopee' in url_lower:
        if COOKIE_SHOPEE:
            LOG.info("Usando cookies da Shopee")
            return COOKIE_SHOPEE
    elif 'instagram' in url_lower or 'insta' in url_lower:
        if COOKIE_IG:
            LOG.info("Usando cookies do Instagram")
            return COOKIE_IG
    elif 'youtube' in url_lower or 'youtu.be' in url_lower:
        if COOKIE_YT:
            LOG.info("Usando cookies do YouTube")
            return COOKIE_YT
    
    # Fallback
    if COOKIE_YT:
        LOG.info("Usando cookies do YouTube (fallback)")
        return COOKIE_YT
    elif COOKIE_SHOPEE:
        LOG.info("Usando cookies da Shopee (fallback)")
        return COOKIE_SHOPEE
    elif COOKIE_IG:
        LOG.info("Usando cookies do Instagram (fallback)")
        return COOKIE_IG
    
    LOG.info("Nenhum cookie disponível")
    return None

def resolve_shopee_universal_link(url: str) -> str:
    """Resolve universal links da Shopee para URL real."""
    try:
        # Verifica se é um universal link
        if 'universal-link' in url and 'redir=' in url:
            LOG.info("Detectado universal link da Shopee, extraindo URL real...")
            
            # Extrai o parâmetro 'redir'
            from urllib.parse import parse_qs, unquote
            
            # Pega a parte após '?'
            if '?' in url:
                query_string = url.split('?', 1)[1]
                params = parse_qs(query_string)
                
                if 'redir' in params:
                    real_url = unquote(params['redir'][0])
                    LOG.info("URL real da Shopee extraída: %s", real_url[:100])
                    return real_url
        
        return url
    except Exception as e:
        LOG.error("Erro ao resolver universal link: %s", e)
        return url

@contextmanager
def temp_download_dir():
    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    LOG.info("Diretório temporário criado: %s", tmpdir)
    try:
        yield tmpdir
    finally:
        try:
            shutil.rmtree(tmpdir)
            LOG.info("Cleanup: removido %s", tmpdir)
        except Exception as e:
            LOG.error("Falha no cleanup de %s: %s", tmpdir, e)

def split_video_file(input_path: str, output_dir: str) -> list:
    os.makedirs(output_dir, exist_ok=True)
    output_pattern = os.path.join(output_dir, "part%03d.mp4")
    
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c", "copy", "-map", "0",
        "-fs", f"{SPLIT_SIZE}",
        output_pattern
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
        LOG.info("ffmpeg concluído com sucesso.")
        parts = sorted([
            os.path.join(output_dir, f) 
            for f in os.listdir(output_dir)
            if os.path.isfile(os.path.join(output_dir, f))
        ])
        return parts
    except subprocess.TimeoutExpired:
        LOG.error("ffmpeg timeout ao processar %s", input_path)
        raise
    except subprocess.CalledProcessError as e:
        LOG.error("ffmpeg falhou: %s\nStderr: %s", e, e.stderr)
        raise
    except Exception as e:
        LOG.error("Erro inesperado no ffmpeg: %s", e)
        raise

def is_bot_mentioned(update: Update) -> bool:
    try:
        bot_username = application.bot.username
        bot_id = application.bot.id
    except Exception as e:
        LOG.error("Erro ao obter info do bot: %s", e)
        bot_username = None
        bot_id = None

    msg = getattr(update, "message", None)
    if not msg:
        return False

    if bot_username:
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
                    if getattr(ent.user, "id", None) == bot_id:
                        return True
        if msg.text and f"@{bot_username}" in msg.text:
            return True
    return False

# Pending Management
def add_pending(token: str, data: dict):
    if len(PENDING) >= PENDING_MAX_SIZE:
        oldest = next(iter(PENDING))
        PENDING.pop(oldest)
        LOG.warning("PENDING cheio, removido token: %s", oldest)
    
    data["created_at"] = time.time()
    PENDING[token] = data
    
    asyncio.run_coroutine_threadsafe(_expire_pending(token), APP_LOOP)

async def _expire_pending(token: str):
    await asyncio.sleep(PENDING_EXPIRE_SECONDS)
    entry = PENDING.pop(token, None)
    if entry:
        LOG.info("Token expirado: %s", token)
        try:
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["expired"],
                chat_id=entry["chat_id"],
                message_id=entry["confirm_msg_id"]
            )
        except Exception:
            pass

# Telegram Handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = get_monthly_users_count()
        
        # Verifica cookies disponíveis
        cookies_info = []
        if COOKIE_YT:
            cookies_info.append("🎬 YouTube")
        if COOKIE_SHOPEE:
            cookies_info.append("🛍️ Shopee")
        if COOKIE_IG:
            cookies_info.append("📸 Instagram")
        
        cookies_text = ", ".join(cookies_info) if cookies_info else "Nenhum"
        
        await update.message.reply_text(
            f"Olá! 👋\n\n"
            f"Me envie um link de vídeo e eu te pergunto se quer baixar.\n\n"
            f"📊 Usuários mensais: {count}\n"
            f"🍪 Cookies: {cookies_text}"
        )
    except Exception as e:
        LOG.error("Erro no comando /start: %s", e)
        await update.message.reply_text("Erro ao processar comando.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = get_monthly_users_count()
        pending_count = len(PENDING)
        
        cookies_count = sum([1 for c in [COOKIE_YT, COOKIE_SHOPEE, COOKIE_IG] if c])
        
        await update.message.reply_text(
            f"📊 **Estatísticas**\n\n"
            f"👥 Usuários mensais: {count}\n"
            f"⏳ Downloads pendentes: {pending_count}\n"
            f"🍪 Cookies configurados: {cookies_count}/3",
            parse_mode="Markdown"
        )
    except Exception as e:
        LOG.error("Erro no comando /stats: %s", e)
        await update.message.reply_text("Erro ao obter estatísticas.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not getattr(update, "message", None) or not update.message.text:
            return

        try:
            update_user(update.message.from_user.id)
        except Exception as e:
            LOG.error("Erro ao atualizar usuário: %s", e)

        text = update.message.text.strip()
        chat_type = update.message.chat.type
        
        if chat_type != "private" and not is_bot_mentioned(update):
            return

        # Extrai URL
        url = None
        if getattr(update.message, "entities", None):
            for ent in update.message.entities:
                if ent.type in ("url", "text_link"):
                    url = getattr(ent, "url", None) or text[ent.offset:ent.offset+ent.length]
                    break

        if not url:
            m = URL_RE.search(text)
            if m:
                url = m.group(1)
        
        if not url:
            return

        if not is_valid_url(url):
            await update.message.reply_text(ERROR_MESSAGES["invalid_url"])
            return

        token = uuid.uuid4().hex
        confirm_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
            ]
        ])

        confirm_msg = await update.message.reply_text(
            f"Você quer baixar este link?\n{url}", 
            reply_markup=confirm_keyboard
        )
        
        add_pending(token, {
            "url": url,
            "chat_id": update.message.chat_id,
            "from_user_id": update.message.from_user.id,
            "confirm_msg_id": confirm_msg.message_id,
            "progress_msg": None,
        })
        
    except Exception as e:
        LOG.exception("Erro no handle_message: %s", e)
        try:
            await update.message.reply_text(ERROR_MESSAGES["unknown"])
        except Exception:
            pass

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        data = query.data or ""
        
        if data.startswith("dl:"):
            token = data.split("dl:", 1)[1]
            entry = PENDING.get(token)
            
            if not entry:
                await query.edit_message_text(ERROR_MESSAGES["expired"])
                return
            
            if query.from_user.id != entry["from_user_id"]:
                await query.answer("⚠️ Apenas quem solicitou pode confirmar.", show_alert=True)
                return

            try:
                await query.edit_message_text("Iniciando download... 🎬")
            except Exception as e:
                LOG.error("Erro ao editar mensagem: %s", e)

            progress_msg = await context.bot.send_message(
                chat_id=entry["chat_id"], 
                text="📥 Baixando: 0% [────────────────────]"
            )
            entry["progress_msg"] = {
                "chat_id": progress_msg.chat_id, 
                "message_id": progress_msg.message_id
            }
            
            asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)

        elif data.startswith("cancel:"):
            token = data.split("cancel:", 1)[1]
            entry = PENDING.pop(token, None)
            
            if not entry:
                await query.edit_message_text("Cancelamento: pedido já expirou.")
                return
            
            await query.edit_message_text("Cancelado ✅")
            
    except Exception as e:
        LOG.exception("Erro no callback_confirm: %s", e)
        try:
            await query.edit_message_text(ERROR_MESSAGES["unknown"])
        except Exception:
            pass

# Download Task
async def start_download_task(token: str):
    entry = PENDING.get(token)
    if not entry:
        LOG.warning("Token não encontrado: %s", token)
        return
    
    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry["progress_msg"]
    
    if not pm:
        LOG.warning("progress_msg não encontrado para token: %s", token)
        return

    watchdog_task = asyncio.create_task(_watchdog(token, WATCHDOG_TIMEOUT))
    
    try:
        with temp_download_dir() as tmpdir:
            await _do_download(token, url, tmpdir, chat_id, pm)
    except asyncio.CancelledError:
        LOG.info("Download cancelado pelo watchdog: %s", token)
        await _notify_error(pm, "timeout")
    except Exception as e:
        LOG.exception("Erro no download: %s", e)
        await _notify_error(pm, "unknown")
    finally:
        watchdog_task.cancel()
        PENDING.pop(token, None)

async def _watchdog(token: str, timeout: int):
    await asyncio.sleep(timeout)
    entry = PENDING.pop(token, None)
    if entry and entry.get("progress_msg"):
        pm = entry["progress_msg"]
        await _notify_error(pm, "timeout")
    LOG.warning("Watchdog timeout para token: %s", token)

async def _notify_error(pm: dict, error_type: str):
    try:
        message = ERROR_MESSAGES.get(error_type, ERROR_MESSAGES["unknown"])
        await application.bot.edit_message_text(
            text=message,
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.error("Erro ao notificar erro: %s", e)

async def _download_shopee_video(url: str, tmpdir: str, chat_id: int, pm: dict):
    """Download especial para Shopee Video sem usar yt-dlp."""
    try:
        # Atualiza mensagem
        await application.bot.edit_message_text(
            text="🛍️ Processando vídeo da Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        # Tenta usar yt-dlp com configurações especiais para Shopee
        outtmpl = os.path.join(tmpdir, "shopee_video.%(ext)s")
        
        ydl_opts = {
            "outtmpl": outtmpl,
            "quiet": False,
            "logger": LOG,
            "format": "best",
            "nocheckcertificate": True,
            "no_warnings": False,
            # Força extratores genéricos
            "default_search": "auto",
            "extractor_args": {"generic": {"allowed_extractors": ["generic", "html5"]}},
            # Headers personalizados
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://shopee.com.br/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        }
        
        # Adiciona cookies se disponível
        cookie_file = get_cookie_for_url(url)
        if cookie_file:
            ydl_opts["cookiefile"] = cookie_file
        
        LOG.info("Tentando download da Shopee com configurações especiais...")
        
        try:
            await asyncio.to_thread(lambda: _run_ydl(ydl_opts, [url]))
            
            # Verifica se arquivo foi baixado
            arquivos = [
                os.path.join(tmpdir, f) 
                for f in os.listdir(tmpdir) 
                if os.path.isfile(os.path.join(tmpdir, f))
            ]
            
            if arquivos:
                LOG.info("Shopee Video baixado com sucesso!")
                await application.bot.edit_message_text(
                    text="✅ Download concluído, enviando...",
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"]
                )
                
                # Envia o vídeo
                for path in arquivos:
                    with open(path, "rb") as fh:
                        await application.bot.send_video(chat_id=chat_id, video=fh)
                
                await application.bot.edit_message_text(
                    text="✅ Vídeo da Shopee enviado!",
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"]
                )
                return
            
        except Exception as e:
            LOG.warning("Método yt-dlp falhou para Shopee: %s", e)
        
        # Se yt-dlp falhou, informa o usuário
        await application.bot.edit_message_text(
            text="⚠️ Desculpe, não consegui baixar este vídeo da Shopee.\n\n"
                 "A Shopee pode ter proteções que impedem o download automático. "
                 "Tente baixar diretamente pelo app da Shopee.",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
    except Exception as e:
        LOG.exception("Erro no download Shopee: %s", e)
        await _notify_error(pm, "unknown")

async def _do_download(token: str, url: str, tmpdir: str, chat_id: int, pm: dict):
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
    last_percent = -1
    
    # Resolve universal links da Shopee
    if 'shopee' in url.lower() and 'universal-link' in url:
        url = resolve_shopee_universal_link(url)
        LOG.info("Usando URL resolvida para download: %s", url[:100])
    
    # Verifica se é Shopee Video - precisa tratamento especial
    if 'sv.shopee' in url.lower() or 'share-video' in url.lower():
        LOG.info("Detectado Shopee Video, usando método alternativo")
        await _download_shopee_video(url, tmpdir, chat_id, pm)
        return

    def progress_hook(d):
        nonlocal last_percent
        try:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    percent = int(downloaded * 100 / total)
                    if percent != last_percent and percent % 5 == 0:
                        last_percent = percent
                        blocks = int(percent / 5)
                        bar = "█" * blocks + "─" * (20 - blocks)
                        text = f"📥 Baixando: {percent}% [{bar}]"
                        try:
                            asyncio.run_coroutine_threadsafe(
                                application.bot.edit_message_text(
                                    text=text, 
                                    chat_id=pm["chat_id"], 
                                    message_id=pm["message_id"]
                                ),
                                APP_LOOP,
                            )
                        except Exception as e:
                            LOG.debug("Erro ao atualizar progresso: %s", e)
            elif status == "finished":
                try:
                    asyncio.run_coroutine_threadsafe(
                        application.bot.edit_message_text(
                            text="✅ Download concluído, processando o envio...", 
                            chat_id=pm["chat_id"], 
                            message_id=pm["message_id"]
                        ),
                        APP_LOOP,
                    )
                except Exception as e:
                    LOG.debug("Erro ao atualizar status finished: %s", e)
        except Exception as e:
            LOG.error("Erro no progress_hook: %s", e)

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
    }
    
    # Usa o cookie apropriado
    cookie_file = get_cookie_for_url(url)
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    # Download
    try:
        await asyncio.to_thread(lambda: _run_ydl(ydl_opts, [url]))
    except Exception as e:
        LOG.exception("Erro no yt-dlp: %s", e)
        await _notify_error(pm, "network_error")
        return

    # Enviar arquivos
    arquivos = [
        os.path.join(tmpdir, f) 
        for f in os.listdir(tmpdir) 
        if os.path.isfile(os.path.join(tmpdir, f))
    ]
    
    if not arquivos:
        LOG.error("Nenhum arquivo baixado")
        await _notify_error(pm, "unknown")
        return

    for path in arquivos:
        try:
            tamanho = os.path.getsize(path)
            
            if tamanho > MAX_FILE_SIZE:
                partes_dir = os.path.join(tmpdir, "partes")
                try:
                    partes = split_video_file(path, partes_dir)
                    LOG.info("Arquivo dividido em %d partes", len(partes))
                    
                    for idx, ppath in enumerate(partes, 1):
                        with open(ppath, "rb") as fh:
                            await application.bot.send_video(
                                chat_id=chat_id, 
                                video=fh,
                                caption=f"Parte {idx}/{len(partes)}"
                            )
                except Exception as e:
                    LOG.exception("Erro ao dividir/enviar arquivo: %s", e)
                    await _notify_error(pm, "ffmpeg_error")
                    return
            else:
                with open(path, "rb") as fh:
                    await application.bot.send_video(chat_id=chat_id, video=fh)
                    
        except Exception as e:
            LOG.exception("Erro ao enviar arquivo %s: %s", path, e)
            await _notify_error(pm, "upload_error")
            return

    # Sucesso
    try:
        await application.bot.edit_message_text(
            text="✅ Download finalizado e enviado!",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.error("Erro ao enviar mensagem final: %s", e)

def _run_ydl(options, urls):
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

# Handlers Registration
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# Flask Routes
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    try:
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    except Exception as e:
        LOG.exception("Falha ao processar webhook: %s", e)
    return "ok"

@app.route("/")
def index():
    return "Bot rodando ✅"

@app.route("/health")
def health():
    checks = {
        "bot": "ok",
        "db": "ok",
        "pending_count": len(PENDING),
        "cookies": {
            "youtube": bool(COOKIE_YT),
            "shopee": bool(COOKIE_SHOPEE),
            "instagram": bool(COOKIE_IG)
        },
        "timestamp": time.time()
    }
    
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DB_FILE, timeout=5)
            conn.execute("SELECT 1")
            conn.close()
    except Exception as e:
        checks["db"] = f"error: {str(e)}"
        LOG.error("Health check DB falhou: %s", e)
    
    try:
        bot_info = application.bot.get_me()
        checks["bot_username"] = bot_info.username
    except Exception as e:
        checks["bot"] = f"error: {str(e)}"
        LOG.error("Health check bot falhou: %s", e)
    
    status = 200 if checks["bot"] == "ok" and checks["db"] == "ok" else 503
    return checks, status

# Main
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    LOG.info("Iniciando servidor Flask na porta %d", port)
    app.run(host="0.0.0.0", port=port)
