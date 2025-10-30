#!/usr/bin/env python3
"""
bot_with_cookies.py - Versão Multi-Usuário Otimizada
OTIMIZAÇÕES: Suporte a múltiplos downloads simultâneos + Rate limiting + Qualidade
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
WATCHDOG_TIMEOUT = 300  # 5 minutos timeout por download
MAX_FILE_SIZE = 50 * 1024 * 1024
SPLIT_SIZE = 45 * 1024 * 1024

# NOVO: Controle de concorrência
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))  # Padrão 2 para servidores básicos
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# NOVO: Modo de economia de CPU - evita reprocessamento quando possível
LOW_CPU_MODE = os.getenv("LOW_CPU_MODE", "true").lower() == "true"

# NOVO: Tempo em segundos para mostrar aviso de download lento (padrão: 15s)
SLOW_DOWNLOAD_WARNING_DELAY = int(os.getenv("SLOW_DOWNLOAD_WARNING_DELAY", "15"))

# Estruturas thread-safe
PENDING = OrderedDict()
PENDING_LOCK = threading.Lock()  # NOVO: Lock para PENDING dict
DB_LOCK = threading.Lock()
ACTIVE_DOWNLOADS = {}  # NOVO: Rastreamento de downloads ativos
ACTIVE_DOWNLOADS_LOCK = threading.Lock()

# Qualidades disponíveis
QUALITY_OPTIONS = {
    "360p": {"height": 360, "label": "360p (Rápido)"},
    "480p": {"height": 480, "label": "480p (Bom)"},
    "720p": {"height": 720, "label": "720p HD"},
    "1080p": {"height": 1080, "label": "1080p Full HD"},
}

ERROR_MESSAGES = {
    "timeout": "⏱️ O download demorou muito e foi cancelado.",
    "invalid_url": "⚠️ Esta URL não é válida ou não é suportada.",
    "network_error": "🌐 Erro de conexão. Tente novamente em alguns minutos.",
    "ffmpeg_error": "🎬 Erro ao processar o vídeo.",
    "upload_error": "📤 Erro ao enviar o arquivo.",
    "unknown": "❌ Ocorreu um erro inesperado. Tente novamente.",
    "expired": "⏰ Este pedido expirou. Envie o link novamente.",
    "queue_full": "⏳ Muitos downloads em andamento. Tente novamente em alguns segundos.",
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
    """Resolve links universais e de redirecionamento da Shopee"""
    try:
        # Tipo 1: Link universal com parâmetro redir
        if 'universal-link' in url and 'redir=' in url:
            # Extrai o parâmetro redir
            if '?' in url:
                query = url.split('?', 1)[1]
                params = parse_qs(query)
                if 'redir' in params:
                    real_url = unquote(params['redir'][0])
                    LOG.info("URL Shopee resolvida (universal-link): %s", real_url[:80])
                    return real_url
        
        # Tipo 2: Link encurtado shp.ee
        if 'shp.ee' in url or 'shopee.link' in url:
            LOG.info("Link encurtado Shopee detectado: %s", url[:50])
            # Tenta seguir o redirect
            try:
                import requests
                response = requests.head(url, allow_redirects=True, timeout=5)
                if response.url:
                    LOG.info("Redirect resolvido para: %s", response.url[:80])
                    return response.url
            except:
                pass
        
        return url
    except Exception as e:
        LOG.warning("Erro ao resolver link Shopee: %s", e)
        return url

@contextmanager
def temp_download_dir():
    """Cria diretório temporário único para cada download"""
    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    try:
        yield tmpdir
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception as e:
            LOG.warning("Erro ao limpar %s: %s", tmpdir, e)

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

def is_youtube_url(url: str) -> bool:
    """Verifica se a URL é do YouTube"""
    url_lower = url.lower()
    return 'youtube.com' in url_lower or 'youtu.be' in url_lower

def is_shopee_url(url: str) -> bool:
    """Verifica se a URL é da Shopee (incluindo links universais)"""
    url_lower = url.lower()
    
    # Links diretos da Shopee
    if 'shopee.' in url_lower:
        return True
    
    # Links de vídeo específicos
    if 'sv.shopee' in url_lower or 'share-video' in url_lower:
        return True
    
    # Links universais com redirect para Shopee (URL encoded)
    if 'shopee' in url_lower and ('redir=' in url_lower or 'universal-link' in url_lower):
        return True
    
    # Decodifica URL para verificar conteúdo
    try:
        decoded = unquote(url_lower)
        if 'shopee' in decoded or 'sv.shopee' in decoded:
            return True
    except:
        pass
    
    return False

def get_active_downloads_count() -> int:
    """Retorna número de downloads ativos"""
    with ACTIVE_DOWNLOADS_LOCK:
        return len(ACTIVE_DOWNLOADS)

# Pending Management (thread-safe)
def add_pending(token: str, data: dict):
    with PENDING_LOCK:
        if len(PENDING) >= PENDING_MAX_SIZE:
            oldest = next(iter(PENDING))
            PENDING.pop(oldest)
        data["created_at"] = time.time()
        PENDING[token] = data
    asyncio.run_coroutine_threadsafe(_expire_pending(token), APP_LOOP)

def get_pending(token: str):
    with PENDING_LOCK:
        return PENDING.get(token)

def remove_pending(token: str):
    with PENDING_LOCK:
        return PENDING.pop(token, None)

async def _expire_pending(token: str):
    await asyncio.sleep(PENDING_EXPIRE_SECONDS)
    remove_pending(token)

def register_active_download(token: str, chat_id: int):
    """Registra um download ativo"""
    with ACTIVE_DOWNLOADS_LOCK:
        ACTIVE_DOWNLOADS[token] = {
            "chat_id": chat_id,
            "started_at": time.time()
        }

def unregister_active_download(token: str):
    """Remove um download ativo"""
    with ACTIVE_DOWNLOADS_LOCK:
        ACTIVE_DOWNLOADS.pop(token, None)

# Telegram Handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = get_monthly_users_count()
        active = get_active_downloads_count()
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
            f"Me envie um link de vídeo do YouTube, Shopee ou Instagram.\n\n"
            f"🎬 Para YouTube, você poderá escolher a qualidade!\n\n"
            f"📊 Usuários: {count}\n"
            f"🍪 Cookies: {cookie_text}\n"
            f"⚡ Downloads ativos: {active}/{MAX_CONCURRENT_DOWNLOADS}"
        )
    except Exception as e:
        LOG.error("Erro no /start: %s", e)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = get_monthly_users_count()
        active = get_active_downloads_count()
        cookies_count = sum([1 for c in [COOKIE_YT, COOKIE_SHOPEE, COOKIE_IG] if c])
        
        with PENDING_LOCK:
            pending_count = len(PENDING)
        
        await update.message.reply_text(
            f"📊 Estatísticas\n\n"
            f"👥 Usuários mensais: {count}\n"
            f"⏳ Pendentes: {pending_count}\n"
            f"⚡ Downloads ativos: {active}/{MAX_CONCURRENT_DOWNLOADS}\n"
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
            entry = get_pending(token)
            
            if not entry:
                await query.edit_message_text(ERROR_MESSAGES["expired"])
                return
            
            if query.from_user.id != entry["from_user_id"]:
                return

            url = entry["url"]
            
            # Se for YouTube, mostra opções de qualidade
            if is_youtube_url(url):
                keyboard = [
                    [
                        InlineKeyboardButton("360p 📱", callback_data=f"q:{token}:360p"),
                        InlineKeyboardButton("480p 📺", callback_data=f"q:{token}:480p"),
                    ],
                    [
                        InlineKeyboardButton("720p HD 🎬", callback_data=f"q:{token}:720p"),
                        InlineKeyboardButton("1080p Full HD ⭐", callback_data=f"q:{token}:1080p"),
                    ],
                    [
                        InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
                    ]
                ]
                await query.edit_message_text(
                    "🎬 Escolha a qualidade do vídeo:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                # Para outros sites, baixa direto
                await query.edit_message_text("Iniciando... 🎬")
                
                progress_msg = await context.bot.send_message(
                    chat_id=entry["chat_id"],
                    text="📥 Preparando..."
                )
                entry["progress_msg"] = {
                    "chat_id": progress_msg.chat_id,
                    "message_id": progress_msg.message_id
                }
                
                asyncio.run_coroutine_threadsafe(start_download_task(token, None), APP_LOOP)

        elif data.startswith("q:"):
            # Callback de qualidade: q:token:quality
            parts = data.split(":", 2)
            token = parts[1]
            quality = parts[2]
            
            entry = get_pending(token)
            if not entry:
                await query.edit_message_text(ERROR_MESSAGES["expired"])
                return
            
            if query.from_user.id != entry["from_user_id"]:
                return
            
            quality_label = QUALITY_OPTIONS.get(quality, {}).get("label", quality)
            await query.edit_message_text(f"Iniciando download em {quality_label}... 🎬")
            
            progress_msg = await context.bot.send_message(
                chat_id=entry["chat_id"],
                text=f"📥 Preparando ({quality_label})..."
            )
            entry["progress_msg"] = {
                "chat_id": progress_msg.chat_id,
                "message_id": progress_msg.message_id
            }
            
            asyncio.run_coroutine_threadsafe(start_download_task(token, quality), APP_LOOP)

        elif data.startswith("cancel:"):
            token = data.split(":", 1)[1]
            remove_pending(token)
            await query.edit_message_text("Cancelado ✅")
    except Exception as e:
        LOG.exception("Erro em callback: %s", e)

# Download Task com Semaphore
async def start_download_task(token: str, quality: str = None):
    entry = get_pending(token)
    if not entry:
        return
    
    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry.get("progress_msg")
    if not pm:
        return

    # NOVO: Flag para rastrear se deve mostrar aviso de demora
    show_slow_warning_task = None
    
    # NOVO: Função que mostra aviso após X segundos
    async def show_slow_download_warning():
        await asyncio.sleep(SLOW_DOWNLOAD_WARNING_DELAY)  # Tempo configurável
        # Verifica se ainda está ativo (não terminou)
        if token in [t for t, _ in ACTIVE_DOWNLOADS.items()]:
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text="⏳ Este download pode levar até 10 minutos dependendo do tamanho do vídeo.\n\n"
                         "Você pode continuar usando o bot normalmente enquanto aguarda! 😊"
                )
                LOG.info("Aviso de download demorado enviado [%s]", token[:8])
            except Exception as e:
                LOG.warning("Erro ao enviar aviso de demora: %s", e)

    # NOVO: Controle de concorrência com semaphore
    try:
        # Tenta adquirir slot para download
        acquired = DOWNLOAD_SEMAPHORE.locked()
        if acquired and get_active_downloads_count() >= MAX_CONCURRENT_DOWNLOADS:
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["queue_full"],
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )
            remove_pending(token)
            return
        
        async with DOWNLOAD_SEMAPHORE:
            # Registra download ativo
            register_active_download(token, chat_id)
            LOG.info("Download iniciado [%s] - Ativos: %d/%d", token[:8], get_active_downloads_count(), MAX_CONCURRENT_DOWNLOADS)
            
            # NOVO: Agenda tarefa para mostrar aviso após 15s
            show_slow_warning_task = asyncio.create_task(show_slow_download_warning())
            
            try:
                # Timeout de 5 minutos por download
                async with asyncio.timeout(WATCHDOG_TIMEOUT):
                    with temp_download_dir() as tmpdir:
                        url = resolve_shopee_link(url)
                        
                        # Detecta tipo de site e usa método apropriado
                        if is_shopee_url(url):
                            LOG.info("Detectado como Shopee: %s", url[:80])
                            # Shopee Video - método especial
                            await _download_shopee(url, tmpdir, chat_id, pm)
                        else:
                            LOG.info("Detectado como outro site (yt-dlp): %s", url[:80])
                            # Outros sites - yt-dlp
                            await _download_ytdlp(url, tmpdir, chat_id, pm, token, quality)
            except asyncio.TimeoutError:
                LOG.error("Download timeout [%s]", token[:8])
                await application.bot.edit_message_text(
                    text=ERROR_MESSAGES["timeout"],
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"]
                )
            except Exception as e:
                LOG.exception("Erro no download [%s]: %s", token[:8], e)
                try:
                    await application.bot.edit_message_text(
                        text=ERROR_MESSAGES["unknown"],
                        chat_id=pm["chat_id"],
                        message_id=pm["message_id"]
                    )
                except:
                    pass
            finally:
                # NOVO: Cancela aviso se ainda não foi enviado
                if show_slow_warning_task and not show_slow_warning_task.done():
                    show_slow_warning_task.cancel()
                
                # Remove download ativo
                unregister_active_download(token)
                LOG.info("Download finalizado [%s] - Ativos: %d/%d", token[:8], get_active_downloads_count(), MAX_CONCURRENT_DOWNLOADS)
    finally:
        remove_pending(token)

async def _try_shopee_api(url: str) -> str:
    """Tenta extrair via API interna da Shopee"""
    try:
        # Extrai ID do vídeo da URL se possível
        video_id_match = re.search(r'/video/(\d+)', url)
        if not video_id_match:
            return None
        
        video_id = video_id_match.group(1)
        
        # Tenta API da Shopee (pode variar por região)
        api_urls = [
            f"https://shopee.com.br/api/v4/video/get_video_by_id?video_id={video_id}",
            f"https://shopee.com.br/api/v2/video/get?video_id={video_id}",
        ]
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": url,
            "Accept": "application/json"
        }
        
        for api_url in api_urls:
            try:
                resp = await asyncio.to_thread(
                    lambda: requests.get(api_url, headers=headers, timeout=10)
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    
                    # Busca URL do vídeo no JSON da API
                    video_url = _extract_video_from_json(data)
                    if video_url:
                        LOG.info("Vídeo via API Shopee: %s", video_url[:80])
                        return video_url
            except:
                continue
        
        return None
    except Exception as e:
        LOG.warning("API Shopee falhou: %s", e)
        return None

async def _download_shopee(url: str, tmpdir: str, chat_id: int, pm: dict):
    """Download de Shopee Video - tenta pegar versão SEM marca d'água"""
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
        
        video_url = None
        source = "Desconhecido"
        
        # ESTRATÉGIA 1: API da Shopee (mais confiável para vídeo original)
        await application.bot.edit_message_text(
            text="🛍️ Tentando API Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        video_url = await _try_shopee_api(url)
        if video_url:
            source = "API Shopee (sem marca d'água)"
        
        # ESTRATÉGIA 2: Extração direta da página
        if not video_url:
            await application.bot.edit_message_text(
                text="🛍️ Extraindo da página...",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )
            video_url = await _try_direct_shopee(url)
            if video_url:
                source = "Página HTML (sem marca d'água)"
        
        # ESTRATÉGIA 3: SVXtract (fallback)
        if not video_url:
            await application.bot.edit_message_text(
                text="🛍️ Tentando SVXtract...",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )
            video_url = await _try_svxtract(url)
            if video_url:
                source = "SVXtract"
        
        if not video_url:
            await application.bot.edit_message_text(
                text="⚠️ Não consegui extrair o vídeo da Shopee.\n\n"
                     "Dica: Tente copiar o link diretamente do vídeo no app.",
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
                caption=f"🛍️ Shopee\n💧 {source}"
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
    """Tenta extrair via SVXtract - busca vídeo SEM marca d'água"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*"
        }
        
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
        
        # Busca URL do vídeo - prioriza versões sem marca d'água
        priority_patterns = [
            r'"original_video_url"\s*:\s*"([^"]+)"',
            r'"no_watermark_url"\s*:\s*"([^"]+)"',
            r'"raw_video_url"\s*:\s*"([^"]+)"',
            r'"video_url"\s*:\s*"([^"]+)"',
            r'"url"\s*:\s*"([^"]+\.mp4[^"]*)"',
            r'href="([^"]+\.mp4[^"]*)"',
        ]
        
        for pattern in priority_patterns:
            match = re.search(pattern, resp.text)
            if match:
                video_url = match.group(1)
                # Evita URLs com indicação de marca d'água
                if 'watermark' not in video_url.lower() and 'wm' not in video_url.lower():
                    LOG.info("Vídeo via SVXtract (sem marca d'água): %s", video_url[:80])
                    return video_url
        
        return None
    except Exception as e:
        LOG.warning("SVXtract falhou: %s", e)
        return None

async def _try_direct_shopee(url: str) -> str:
    """Extração direta da página Shopee - busca vídeo SEM marca d'água"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://shopee.com.br/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        
        resp = await asyncio.to_thread(
            lambda: requests.get(url, headers=headers, timeout=20)
        )
        
        # ESTRATÉGIA 1: Buscar URLs específicas SEM marca d'água
        # A Shopee usa diferentes chaves para vídeo original vs download
        priority_patterns = [
            r'"originVideoUrl"\s*:\s*"([^"]+)"',  # URL original
            r'"rawVideoUrl"\s*:\s*"([^"]+)"',     # URL raw
            r'"defaultVideo"\s*:\s*"([^"]+)"',    # URL padrão
            r'"video_url"\s*:\s*"([^"]+)"',       # URL do vídeo
        ]
        
        # Tenta padrões prioritários primeiro (vídeo original)
        for pattern in priority_patterns:
            matches = re.findall(pattern, resp.text)
            for match in matches:
                clean = match.replace('\\/', '/').replace('\\u0026', '&')
                if 'http' in clean and ('.mp4' in clean or 'video' in clean):
                    # Verifica se não é a URL com marca d'água
                    if 'watermark' not in clean.lower() and 'wm' not in clean.lower():
                        LOG.info("Vídeo original Shopee encontrado: %s", clean[:80])
                        return clean
        
        # ESTRATÉGIA 2: Buscar no JSON embutido na página
        # Shopee costuma ter um JSON com dados do vídeo
        json_pattern = r'<script[^>]*>window\.__INITIAL_STATE__\s*=\s*({.+?})</script>'
        json_match = re.search(json_pattern, resp.text, re.DOTALL)
        
        if json_match:
            try:
                json_data = json.loads(json_match.group(1))
                # Navega pelo JSON procurando URLs de vídeo
                video_url = _extract_video_from_json(json_data)
                if video_url:
                    LOG.info("Vídeo original via JSON: %s", video_url[:80])
                    return video_url
            except:
                pass
        
        # ESTRATÉGIA 3: Padrões genéricos (fallback)
        fallback_patterns = [
            r'(https://[^"\s]*video[^"\s]*\.mp4[^"\s]*)',
            r'"videoUrl"\s*:\s*"([^"]+)"',
            r'"playAddr"\s*:\s*"([^"]+)"',
            r'(https://[^"\s]*\.mp4[^"\s]*)',
        ]
        
        for pattern in fallback_patterns:
            matches = re.findall(pattern, resp.text)
            for match in matches:
                clean = match.replace('\\/', '/').replace('\\u0026', '&')
                if 'http' in clean and '.mp4' in clean:
                    # Evita URLs de thumbnail/preview
                    if 'thumbnail' not in clean.lower() and 'preview' not in clean.lower():
                        LOG.info("Vídeo Shopee (fallback): %s", clean[:80])
                        return clean
        
        return None
    except Exception as e:
        LOG.warning("Extração direta Shopee falhou: %s", e)
        return None

def _extract_video_from_json(data, depth=0):
    """
    Extrai URL de vídeo recursivamente de estrutura JSON
    Procura por chaves que contenham 'video', 'url', etc
    """
    if depth > 10:  # Limita profundidade para evitar loops
        return None
    
    if isinstance(data, dict):
        # Procura por chaves prioritárias
        priority_keys = ['originVideoUrl', 'rawVideoUrl', 'defaultVideo', 'videoUrl']
        for key in priority_keys:
            if key in data and isinstance(data[key], str):
                url = data[key]
                if 'http' in url and ('mp4' in url or 'video' in url):
                    if 'watermark' not in url.lower():
                        return url
        
        # Busca recursiva em todos os valores
        for value in data.values():
            result = _extract_video_from_json(value, depth + 1)
            if result:
                return result
    
    elif isinstance(data, list):
        for item in data:
            result = _extract_video_from_json(item, depth + 1)
            if result:
                return result
    
    return None

async def _download_ytdlp(url: str, tmpdir: str, chat_id: int, pm: dict, token: str, quality: str = None):
    """Download via yt-dlp com progresso e qualidade específica"""
    try:
        outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
        
        # Define formato baseado na qualidade escolhida
        if quality and quality in QUALITY_OPTIONS:
            height = QUALITY_OPTIONS[quality]["height"]
            format_str = f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height}]"
        else:
            format_str = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]"
        
        ydl_opts = {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "format": format_str,
            "merge_output_format": "mp4",
            "progress_hooks": [lambda d: _progress_hook(d, token, pm)],
        }
        
        # MODO LOW CPU: Evita recodificação quando possível
        if LOW_CPU_MODE:
            LOG.info("Modo LOW_CPU ativado - evitando recodificação")
            # Apenas mescla streams sem recodificar
            ydl_opts["postprocessor_args"] = {
                "ffmpeg": ["-c", "copy"]  # Copia streams sem recodificar
            }
        else:
            # Modo normal: recodifica para garantir qualidade
            ydl_opts["postprocessor_args"] = {
                "ffmpeg": [
                    "-vf", "scale='min(iw,1920)':'min(ih,1080)':force_original_aspect_ratio=decrease",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",  # Menos CPU, arquivo maior
                    "-crf", "28"  # Compressão mais rápida
                ]
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
        
        quality_label = QUALITY_OPTIONS.get(quality, {}).get("label", "HD") if quality else "HD"
        
        for path in files:
            with open(path, "rb") as fh:
                await application.bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    caption=f"🎬 {quality_label}"
                )
        
        await application.bot.edit_message_text(
            text="✅ Enviado!",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.exception("Erro yt-dlp: %s", e)
        
        # Mensagem específica se for URL não suportada
        error_message = str(e)
        if 'Unsupported URL' in error_message or 'unsupported' in error_message.lower():
            await application.bot.edit_message_text(
                text="⚠️ Este site não é suportado.\n\n"
                     "Sites suportados:\n"
                     "🎬 YouTube\n"
                     "🛍️ Shopee\n"
                     "📸 Instagram\n"
                     "📺 E muitos outros via yt-dlp",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )
        else:
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["network_error"],
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )

def _progress_hook(d, token, pm):
    """Hook de progresso para yt-dlp com rate limiting"""
    try:
        entry = get_pending(token)
        if not entry:
            return
        
        status = d.get('status')
        current_time = time.time()
        
        if status == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            speed = d.get('_speed_str', '?').strip()
            eta = d.get('_eta_str', '?').strip()
            
            message = f"📥 Baixando: {percent}\n⚡ Velocidade: {speed}\n⏱️ Tempo restante: {eta}"
            
            # Rate limiting: atualiza apenas a cada 3 segundos
            last_update = entry.get("last_update_time", 0)
            if current_time - last_update >= 3.0:
                try:
                    asyncio.run_coroutine_threadsafe(
                        application.bot.edit_message_text(
                            text=message,
                            chat_id=pm["chat_id"],
                            message_id=pm["message_id"]
                        ),
                        APP_LOOP
                    )
                    entry["last_update_time"] = current_time
                    entry["last_progress"] = percent
                except Exception as e:
                    # Ignora erros de rate limit silenciosamente
                    if "429" not in str(e):
                        LOG.debug("Erro ao atualizar progresso: %s", e)
        
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
    active = get_active_downloads_count()
    return f"Bot Online ✅<br>Downloads ativos: {active}/{MAX_CONCURRENT_DOWNLOADS}"

@app.route("/health")
def health():
    with PENDING_LOCK:
        pending_count = len(PENDING)
    
    return {
        "status": "ok",
        "pending": pending_count,
        "active_downloads": get_active_downloads_count(),
        "max_downloads": MAX_CONCURRENT_DOWNLOADS,
        "cookies": {
            "youtube": bool(COOKIE_YT),
            "shopee": bool(COOKIE_SHOPEE),
            "instagram": bool(COOKIE_IG)
        }
    }

# Main
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    LOG.info("🚀 Iniciando bot na porta %d", port)
    LOG.info("⚡ Máximo de downloads simultâneos: %d", MAX_CONCURRENT_DOWNLOADS)
    LOG.info("💻 Modo LOW_CPU: %s", "ATIVADO" if LOW_CPU_MODE else "DESATIVADO")
    LOG.info("⏰ Aviso de demora após: %d segundos", SLOW_DOWNLOAD_WARNING_DELAY)
    
    # IMPORTANTE: threaded=True para suportar múltiplas requisições
    app.run(host="0.0.0.0", port=port, threaded=True)
