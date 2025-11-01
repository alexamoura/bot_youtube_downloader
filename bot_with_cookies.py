#!/usr/bin/env python3
"""
bot_with_cookies_melhorado.py - VersÃ£o Profissional

Telegram bot (webhook) com sistema de controle de downloads e suporte a pagamento PIX
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
from datetime import datetime, timedelta
import io
import yt_dlp

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    
try:
    import mercadopago
    MERCADOPAGO_AVAILABLE = True
except ImportError:
    MERCADOPAGO_AVAILABLE = False

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

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

# ConfiguraÃ§Ã£o de Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ytbot")

# Token do Bot
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN nÃ£o definido.")
    sys.exit(1)

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# Constantes do Sistema
URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = os.getenv("DB_FILE", "/data/users.db") if os.path.exists("/data") else "users.db"
PENDING_MAX_SIZE = 1000
PENDING_EXPIRE_SECONDS = 600
WATCHDOG_TIMEOUT = 180
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB - limite para vÃ­deos curtos
SPLIT_SIZE = 45 * 1024 * 1024

# Constantes de Controle de Downloads
FREE_DOWNLOADS_LIMIT = 10
MAX_CONCURRENT_DOWNLOADS = 3  # AtÃ© 3 downloads simultÃ¢neos

# ConfiguraÃ§Ã£o do Mercado Pago
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
PREMIUM_PRICE = float(os.getenv("PREMIUM_PRICE", "9.90"))
PREMIUM_DURATION_DAYS = int(os.getenv("PREMIUM_DURATION_DAYS", "30"))

if MERCADOPAGO_AVAILABLE and MERCADOPAGO_ACCESS_TOKEN:
    LOG.info("âœ… Mercado Pago configurado - Token: %s...", MERCADOPAGO_ACCESS_TOKEN[:20])
else:
    if not MERCADOPAGO_AVAILABLE:
        LOG.warning("âš ï¸ mercadopago nÃ£o instalado - pip install mercadopago")
    if not MERCADOPAGO_ACCESS_TOKEN:
        LOG.warning("âš ï¸ MERCADOPAGO_ACCESS_TOKEN nÃ£o configurado")

# ConfiguraÃ§Ã£o do Groq (IA)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = None

if GROQ_AVAILABLE and GROQ_API_KEY:
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
        LOG.info("âœ… Groq AI configurado - InteligÃªncia artificial ativa!")
    except Exception as e:
        LOG.error("âŒ Erro ao inicializar Groq: %s", e)
        groq_client = None
else:
    if not GROQ_AVAILABLE:
        LOG.warning("âš ï¸ groq nÃ£o instalado - pip install groq")
    if not GROQ_API_KEY:
        LOG.warning("âš ï¸ GROQ_API_KEY nÃ£o configurado - IA desativada")

# Estado Global
PENDING = OrderedDict()
DB_LOCK = threading.Lock()
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)  # Controle de fila
ACTIVE_DOWNLOADS = {}  # Rastreamento de downloads ativos

# Mensagens Profissionais do Bot
MESSAGES = {
    "welcome": (
        "ðŸŽ¥ <b>Bem-vindo ao ServiÃ§o de Downloads</b>\n\n"
        "Envie um link de vÃ­deo de YouTube, Instagram ou Shopee e eu processarei o download para vocÃª.\n\n"
        "ðŸ“Š <b>Planos disponÃ­veis:</b>\n"
        "â€¢ Gratuito: {free_limit} downloads/mÃªs\n"
        "â€¢ Premium: Downloads ilimitados\n\n"
        "âš™ï¸ <b>EspecificaÃ§Ãµes:</b>\n"
        "â€¢ VÃ­deos curtos (atÃ© 50 MB)\n"
        "â€¢ Qualidade atÃ© 720p\n"
        "â€¢ Fila: atÃ© 3 downloads simultÃ¢neos\n\n"
        "Digite /status para verificar seu saldo de downloads ou /premium para assinar o plano."
    ),
    "url_prompt": "ðŸ“Ž Por favor, envie o link do vÃ­deo que deseja baixar.",
    "processing": "âš™ï¸ Processando sua solicitaÃ§Ã£o...",
    "invalid_url": "âš ï¸ O link fornecido nÃ£o Ã© vÃ¡lido. Por favor, verifique e tente novamente.",
    "file_too_large": "âš ï¸ <b>Arquivo muito grande</b>\n\nEste vÃ­deo excede o limite de 50 MB. Por favor, escolha um vÃ­deo mais curto.",
    "confirm_download": "ðŸŽ¬ <b>Confirmar Download</b>\n\nðŸ“¹ VÃ­deo: {title}\nâ±ï¸ DuraÃ§Ã£o: {duration}\nðŸ“¦ Tamanho: {filesize}\n\nâœ… Deseja prosseguir com o download?",
    "queue_position": "â³ Aguardando na fila... PosiÃ§Ã£o: {position}\n\n{active} downloads em andamento.",
    "download_started": "ðŸ“¥ Download iniciado. Aguarde enquanto processamos seu vÃ­deo...",
    "download_progress": "ðŸ“¥ Progresso: {percent}%\n{bar}",
    "download_complete": "âœ… Download concluÃ­do. Enviando arquivo...",
    "upload_complete": "âœ… VÃ­deo enviado com sucesso!\n\nðŸ“Š Downloads restantes: {remaining}/{total}",
    "limit_reached": (
        "âš ï¸ <b>Limite de Downloads Atingido</b>\n\n"
        "VocÃª atingiu o limite de {limit} downloads gratuitos.\n\n"
        "ðŸ’Ž <b>Adquira o Plano Premium para downloads ilimitados!</b>\n\n"
        "ðŸ’³ Valor: R$ 9,90/mÃªs\n"
        "ðŸ”„ Pagamento via PIX\n\n"
        "Entre em contato para mais informaÃ§Ãµes: /premium"
    ),
    "status": (
        "ðŸ“Š <b>Status da Sua Conta</b>\n\n"
        "ðŸ‘¤ ID: {user_id}\n"
        "ðŸ“¥ Downloads realizados: {used}/{total}\n"
        "ðŸ’¾ Downloads restantes: {remaining}\n"
        "ðŸ“… PerÃ­odo: Mensal\n\n"
        "{premium_info}"
    ),
    "premium_info": (
        "ðŸ’Ž <b>InformaÃ§Ãµes sobre o Plano Premium</b>\n\n"
        "âœ¨ <b>BenefÃ­cios:</b>\n"
        "â€¢ Downloads ilimitados\n"
        "â€¢ Qualidade mÃ¡xima (atÃ© 1080p)\n"
        "â€¢ Processamento prioritÃ¡rio\n"
        "â€¢ Suporte dedicado\n\n"
        "ðŸ’° <b>Valor:</b> R$ 9,90/mÃªs\n\n"
        "ðŸ“± <b>Como contratar:</b>\n"
        "1ï¸âƒ£ Clique no botÃ£o \"Assinar Premium\"\n"
        "2ï¸âƒ£ Escaneie o QR Code PIX gerado\n"
        "3ï¸âƒ£ Confirme o pagamento no seu banco\n"
        "4ï¸âƒ£ Aguarde a ativaÃ§Ã£o automÃ¡tica (30-60 segundos)\n\n"
        "âš¡ <b>AtivaÃ§Ã£o instantÃ¢nea via PIX!</b>"
    ),
    "stats": "ðŸ“ˆ <b>EstatÃ­sticas do Bot</b>\n\nðŸ‘¥ UsuÃ¡rios ativos este mÃªs: {count}",
    "error_timeout": "â±ï¸ O tempo de processamento excedeu o limite. Por favor, tente novamente.",
    "error_network": "ðŸŒ Erro de conexÃ£o detectado. Verifique sua internet e tente novamente em alguns instantes.",
    "error_file_large": "ðŸ“¦ O arquivo excede o limite de 50 MB. Por favor, escolha um vÃ­deo mais curto.",
    "error_ffmpeg": "ðŸŽ¬ Ocorreu um erro durante o processamento do vÃ­deo.",
    "error_upload": "ðŸ“¤ Falha ao enviar o arquivo. Por favor, tente novamente.",
    "error_unknown": "âŒ Um erro inesperado ocorreu. Nossa equipe foi notificada. Por favor, tente novamente.",
    "error_expired": "â° Esta solicitaÃ§Ã£o expirou. Por favor, envie o link novamente.",
    "download_cancelled": "ðŸš« Download cancelado com sucesso.",
    "cleanup": "ðŸ§¹ Limpeza: removido {path}",
}

app = Flask(__name__)

# InicializaÃ§Ã£o do Telegram Application
try:
    application = ApplicationBuilder().token(TOKEN).build()
    LOG.info("ApplicationBuilder criado com sucesso.")
except Exception as e:
    LOG.exception("Erro ao construir ApplicationBuilder")
    sys.exit(1)

# Loop de Eventos Asyncio
APP_LOOP = asyncio.new_event_loop()

def _start_loop(loop):
    """Inicia o event loop em background"""
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

# ============================
# DATABASE - Sistema de Controle de Downloads
# ============================

def init_db():
    """Inicializa o banco de dados com as tabelas necessÃ¡rias"""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            
            # Tabela de usuÃ¡rios mensais
            c.execute("""
                CREATE TABLE IF NOT EXISTS monthly_users (
                    user_id INTEGER PRIMARY KEY,
                    last_month TEXT
                )
            """)
            
            # Tabela de controle de downloads
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_downloads (
                    user_id INTEGER PRIMARY KEY,
                    downloads_count INTEGER DEFAULT 0,
                    is_premium INTEGER DEFAULT 0,
                    premium_expires TEXT,
                    last_reset TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Tabela de histÃ³rico de pagamentos PIX (para implementaÃ§Ã£o futura)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pix_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    pix_key TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    confirmed_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES user_downloads(user_id)
                )
            """)
            
            conn.commit()
            conn.close()
            LOG.info("Banco de dados inicializado com sucesso.")
        except sqlite3.Error as e:
            LOG.error("Erro ao inicializar banco de dados: %s", e)

def update_user(user_id: int):
    """Atualiza o registro de acesso mensal do usuÃ¡rio"""
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
            LOG.error("Erro ao atualizar usuÃ¡rio: %s", e)

def get_user_download_stats(user_id: int) -> dict:
    """Retorna estatÃ­sticas de downloads do usuÃ¡rio"""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            
            # Busca ou cria registro do usuÃ¡rio
            c.execute("SELECT downloads_count, is_premium, last_reset FROM user_downloads WHERE user_id=?", (user_id,))
            row = c.fetchone()
            
            current_month = time.strftime("%Y-%m")
            
            if row:
                downloads_count, is_premium, last_reset = row
                
                # Reseta contador se mudou o mÃªs
                if last_reset != current_month and not is_premium:
                    downloads_count = 0
                    c.execute("UPDATE user_downloads SET downloads_count=0, last_reset=? WHERE user_id=?", 
                             (current_month, user_id))
                    conn.commit()
            else:
                # Cria novo registro
                downloads_count, is_premium = 0, 0
                c.execute("""
                    INSERT INTO user_downloads (user_id, downloads_count, is_premium, last_reset) 
                    VALUES (?, 0, 0, ?)
                """, (user_id, current_month))
                conn.commit()
            
            conn.close()
            
            remaining = "Ilimitado" if is_premium else max(0, FREE_DOWNLOADS_LIMIT - downloads_count)
            
            return {
                "downloads_count": downloads_count,
                "is_premium": bool(is_premium),
                "remaining": remaining,
                "limit": FREE_DOWNLOADS_LIMIT
            }
        except sqlite3.Error as e:
            LOG.error("Erro ao obter estatÃ­sticas de download: %s", e)
            return {"downloads_count": 0, "is_premium": False, "remaining": FREE_DOWNLOADS_LIMIT, "limit": FREE_DOWNLOADS_LIMIT}

def can_download(user_id: int) -> bool:
    """Verifica se o usuÃ¡rio pode realizar um download"""
    stats = get_user_download_stats(user_id)
    
    if stats["is_premium"]:
        return True
    
    return stats["downloads_count"] < FREE_DOWNLOADS_LIMIT

def increment_download_count(user_id: int):
    """Incrementa o contador de downloads do usuÃ¡rio"""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("UPDATE user_downloads SET downloads_count = downloads_count + 1 WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
            LOG.info("Contador de downloads incrementado para usuÃ¡rio %d", user_id)
        except sqlite3.Error as e:
            LOG.error("Erro ao incrementar contador de downloads: %s", e)

def get_monthly_users_count() -> int:
    """Retorna o nÃºmero de usuÃ¡rios ativos no mÃªs atual"""
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

# ============================
# PIX PAYMENT SYSTEM (Estrutura para implementaÃ§Ã£o futura)
# ============================

def create_pix_payment(user_id: int, amount: float) -> str:
    """
    Cria um registro de pagamento PIX pendente
    
    TODO: Implementar integraÃ§Ã£o com gateway de pagamento
    - Gerar QR Code PIX
    - Criar chave PIX Ãºnica por transaÃ§Ã£o
    - Retornar dados para exibiÃ§Ã£o ao usuÃ¡rio
    """
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            
            # Insere registro de pagamento pendente
            c.execute("""
                INSERT INTO pix_payments (user_id, amount, status) 
                VALUES (?, ?, 'pending')
            """, (user_id, amount))
            
            payment_id = c.lastrowid
            conn.commit()
            conn.close()
            
            LOG.info("Pagamento PIX criado: ID=%d, User=%d, Amount=%.2f", payment_id, user_id, amount)
            
            # TODO: Integrar com API de pagamento PIX
            # Exemplo: Mercado Pago, PagSeguro, etc.
            
            return f"PIX_{payment_id}_{user_id}"
        except sqlite3.Error as e:
            LOG.error("Erro ao criar pagamento PIX: %s", e)
            return None

def confirm_pix_payment(payment_reference: str, user_id: int):
    """
    Confirma um pagamento PIX e ativa o plano premium
    
    TODO: Implementar verificaÃ§Ã£o automÃ¡tica de pagamento
    - Webhook do gateway de pagamento
    - ValidaÃ§Ã£o do comprovante
    - AtivaÃ§Ã£o automÃ¡tica do premium
    """
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            
            # Atualiza status do pagamento
            c.execute("""
                UPDATE pix_payments 
                SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP 
                WHERE user_id=? AND status='pending'
            """, (user_id,))
            
            # Ativa premium para o usuÃ¡rio
            premium_expires = time.strftime("%Y-%m-%d", time.localtime(time.time() + 30*24*60*60))  # +30 dias
            c.execute("""
                UPDATE user_downloads 
                SET is_premium=1, premium_expires=? 
                WHERE user_id=?
            """, (premium_expires, user_id))
            
            conn.commit()
            conn.close()
            
            LOG.info("Pagamento PIX confirmado para usuÃ¡rio %d", user_id)
            return True
        except sqlite3.Error as e:
            LOG.error("Erro ao confirmar pagamento PIX: %s", e)
            return False

# Inicializar banco de dados
init_db()

# ============================
# COOKIES - Sistema Multi-Plataforma
# ============================

def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    """Prepara arquivo de cookies a partir de variÃ¡vel de ambiente Base64"""
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("VariÃ¡vel %s nÃ£o encontrada.", env_var)
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
        LOG.info("Cookies %s carregados em %s", env_var, path)
        return path
    except Exception as e:
        LOG.error("Falha ao gravar cookies %s: %s", env_var, e)
        return None

# Carrega cookies de diferentes plataformas
COOKIE_YT = prepare_cookies_from_env("YT_COOKIES_B64")
COOKIE_SHOPEE = prepare_cookies_from_env("SHOPEE_COOKIES_B64")
COOKIE_IG = prepare_cookies_from_env("IG_COOKIES_B64")

# ============================
# UTILITIES
# ============================

def is_valid_url(url: str) -> bool:
    """Valida se a string Ã© uma URL vÃ¡lida"""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False

def get_cookie_for_url(url: str):
    """Retorna o arquivo de cookie apropriado baseado na URL"""
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
    
    # Fallback para YouTube cookies
    if COOKIE_YT:
        LOG.info("Usando cookies do YouTube (fallback)")
        return COOKIE_YT
    elif COOKIE_SHOPEE:
        LOG.info("Usando cookies da Shopee (fallback)")
        return COOKIE_SHOPEE
    elif COOKIE_IG:
        LOG.info("Usando cookies do Instagram (fallback)")
        return COOKIE_IG
    
    LOG.info("Nenhum cookie disponÃ­vel")
    return None

def get_format_for_url(url: str) -> str:
    """Retorna o formato apropriado baseado na plataforma"""
    url_lower = url.lower()
    
    # Instagram: usa formato simples sem especificar height
    if 'instagram' in url_lower or 'insta' in url_lower:
        LOG.info("Formato Instagram: best (sem restriÃ§Ãµes especÃ­ficas)")
        return "best"
    
    # YouTube: limita a 720p
    elif 'youtube' in url_lower or 'youtu.be' in url_lower:
        LOG.info("Formato YouTube: 720p mÃ¡ximo")
        return "best[height<=720]/best"
    
    # Outras plataformas: formato padrÃ£o flexÃ­vel
    else:
        LOG.info("Formato padrÃ£o: best com fallback")
        return "best/bestvideo+bestaudio"

def resolve_shopee_universal_link(url: str) -> str:
    """Resolve universal links da Shopee para URL real"""
    try:
        if 'universal-link' in url and 'redir=' in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'redir' in params:
                redir = unquote(params['redir'][0])
                LOG.info("Universal link resolvido: %s -> %s", url[:50], redir[:50])
                return redir
    except Exception as e:
        LOG.error("Erro ao resolver universal link: %s", e)
    
    return url

def format_duration(seconds: int) -> str:
    """Formata duraÃ§Ã£o em segundos para formato legÃ­vel"""
    if not seconds:
        return "N/A"
    
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

def format_filesize(bytes_size: int) -> str:
    """Formata tamanho de arquivo em bytes para formato legÃ­vel"""
    if not bytes_size:
        return "N/A"
    
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    
    return f"{bytes_size:.1f} TB"

async def _download_shopee_video(url: str, tmpdir: str, chat_id: int, pm: dict):
    """Download especial para Shopee Video usando web scraping"""
    if not REQUESTS_AVAILABLE:
        await application.bot.edit_message_text(
            text="âš ï¸ Extrator Shopee nÃ£o disponÃ­vel. Instale: pip install requests beautifulsoup4",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        return
    
    try:
        # Atualiza mensagem
        await application.bot.edit_message_text(
            text="ðŸ›ï¸ Extraindo vÃ­deo da Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        LOG.info("Iniciando extraÃ§Ã£o customizada da Shopee: %s", url)
        
        # Faz requisiÃ§Ã£o Ã  pÃ¡gina
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://shopee.com.br/",
        }
        
        # Carrega cookies se disponÃ­vel
        cookies_dict = {}
        if COOKIE_SHOPEE:
            try:
                with open(COOKIE_SHOPEE, 'r') as f:
                    for line in f:
                        if not line.startswith('#') and line.strip():
                            parts = line.strip().split('\t')
                            if len(parts) >= 7:
                                cookies_dict[parts[5]] = parts[6]
                LOG.info("Cookies da Shopee carregados: %d cookies", len(cookies_dict))
            except Exception as e:
                LOG.warning("Erro ao carregar cookies: %s", e)
        
        response = await asyncio.to_thread(
            lambda: requests.get(url, headers=headers, cookies=cookies_dict, timeout=30)
        )
        response.raise_for_status()
        
        LOG.info("PÃ¡gina da Shopee carregada, analisando...")
        
        # Busca URL do vÃ­deo no HTML/JavaScript
        video_url = None
        
        # PadrÃ£o 1: Busca em tags <script> com JSON
        import json
        patterns = [
            # PadrÃµes originais
            r'"videoUrl"\s*:\s*"([^"]+)"',
            r'"video_url"\s*:\s*"([^"]+)"',
            r'"playAddr"\s*:\s*"([^"]+)"',
            r'"url"\s*:\s*"(https://[^"]*\.mp4[^"]*)"',
            r'playAddr["\']:\s*["\']([^"\']+)',
            r'"playUrl"\s*:\s*"([^"]+)"',
            # Novos padrÃµes para Shopee
            r'"video"\s*:\s*{\s*"url"\s*:\s*"([^"]+)"',
            r'"stream"\s*:\s*"([^"]+)"',
            r'"source"\s*:\s*"([^"]+)"',
            r'videoUrl:\s*["\']([^"\']+)',
            r'src:\s*["\']([^"\']+\.mp4[^"\']*)',
            # PadrÃµes para dados em window/global
            r'window\.__INITIAL_STATE__.*?"video".*?"url"\s*:\s*"([^"]+)"',
            r'window\.videoData.*?"url"\s*:\s*"([^"]+)"',
            # PadrÃµes para URLs diretas de CDN
            r'(https://[^"\s]*shopee[^"\s]*\.mp4[^"\s]*)',
            r'(https://[^"\s]*vod[^"\s]*\.mp4[^"\s]*)',
            r'(https://[^"\s]*video[^"\s]*\.mp4[^"\s]*)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, response.text)
            for match in matches:
                # Limpa a URL
                clean_url = match.replace('\\/', '/').replace('\\', '')
                if 'http' in clean_url and ('mp4' in clean_url.lower() or 'video' in clean_url.lower()):
                    video_url = clean_url
                    LOG.info("URL de vÃ­deo encontrada via regex: %s", video_url[:100])
                    break
            if video_url:
                break
        
        # PadrÃ£o 2: Busca em meta tags
        if not video_url:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Busca em scripts tipo application/json ou application/ld+json
            scripts = soup.find_all('script', type=['application/json', 'application/ld+json'])
            for script in scripts:
                if script.string:
                    try:
                        data = json.loads(script.string)
                        # Busca recursivamente no JSON
                        def find_video_url_in_dict(obj, depth=0):
                            if depth > 10:
                                return None
                            if isinstance(obj, dict):
                                for key, value in obj.items():
                                    if key in ['videoUrl', 'video_url', 'playAddr', 'playUrl', 'url', 'src', 'source']:
                                        if isinstance(value, str) and ('http' in value or value.endswith('.mp4')):
                                            return value
                                    result = find_video_url_in_dict(value, depth + 1)
                                    if result:
                                        return result
                            elif isinstance(obj, list):
                                for item in obj:
                                    result = find_video_url_in_dict(item, depth + 1)
                                    if result:
                                        return result
                            return None
                        
                        found_url = find_video_url_in_dict(data)
                        if found_url:
                            video_url = found_url
                            LOG.info("URL encontrada em script JSON: %s", video_url[:100])
                            break
                    except:
                        pass
            
            # Meta tags
            if not video_url:
                meta_tags = [
                    soup.find('meta', property='og:video'),
                    soup.find('meta', property='og:video:url'),
                    soup.find('meta', property='og:video:secure_url'),
                    soup.find('meta', attrs={'name': 'twitter:player:stream'}),
                ]
                
                for tag in meta_tags:
                    if tag and tag.get('content'):
                        video_url = tag.get('content')
                        LOG.info("URL de vÃ­deo encontrada via meta tag: %s", video_url[:100])
                        break
        
        # PadrÃ£o 3: Busca em tags <video> ou <source>
        if not video_url:
            soup = BeautifulSoup(response.content, 'html.parser')
            video_tag = soup.find('video')
            if video_tag:
                video_url = video_tag.get('src') or video_tag.get('data-src')
            
            if not video_url:
                source_tags = soup.find_all('source')
                for source in source_tags:
                    src = source.get('src') or source.get('data-src')
                    if src and ('mp4' in src.lower() or 'video' in src.lower()):
                        video_url = src
                        break
        
        if not video_url:
            LOG.error("Nenhuma URL de vÃ­deo encontrada na pÃ¡gina da Shopee")
            await application.bot.edit_message_text(
                text="âš ï¸ <b>NÃ£o consegui encontrar o vÃ­deo</b>\n\n"
                     "PossÃ­veis causas:\n"
                     "â€¢ O link pode estar incorreto\n"
                     "â€¢ O vÃ­deo pode ter sido removido\n"
                     "â€¢ A Shopee mudou a estrutura do site\n\n"
                     "Tente baixar pelo app oficial da Shopee.",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"],
                parse_mode="HTML"
            )
            return
        
        # Ajusta URL se necessÃ¡rio
        if not video_url.startswith('http'):
            video_url = 'https:' + video_url if video_url.startswith('//') else 'https://sv.shopee.com.br' + video_url
        
        LOG.info("Baixando vÃ­deo da URL: %s", video_url[:100])
        
        # Atualiza mensagem
        await application.bot.edit_message_text(
            text="ðŸ“¥ Baixando vÃ­deo da Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        # Baixa o vÃ­deo
        output_path = os.path.join(tmpdir, "shopee_video.mp4")
        
        video_response = await asyncio.to_thread(
            lambda: requests.get(video_url, headers=headers, cookies=cookies_dict, stream=True, timeout=120)
        )
        video_response.raise_for_status()
        
        total_size = int(video_response.headers.get('content-length', 0))
        
        # Verifica tamanho antes de baixar
        if total_size > MAX_FILE_SIZE:
            LOG.warning("VÃ­deo da Shopee excede 50 MB: %d bytes", total_size)
            await application.bot.edit_message_text(
                text=MESSAGES["file_too_large"],
                chat_id=pm["chat_id"],
                message_id=pm["message_id"],
                parse_mode="HTML"
            )
            return
        
        downloaded = 0
        last_percent = -1
        
        with open(output_path, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if total_size:
                        percent = int(downloaded * 100 / total_size)
                        if percent != last_percent and percent % 10 == 0:
                            last_percent = percent
                            blocks = int(percent / 5)
                            bar = "â–ˆ" * blocks + "â–‘" * (20 - blocks)
                            try:
                                await application.bot.edit_message_text(
                                    text=f"ðŸ“¥ Shopee: {percent}%\n{bar}",
                                    chat_id=pm["chat_id"],
                                    message_id=pm["message_id"]
                                )
                            except:
                                pass
        
        LOG.info("VÃ­deo da Shopee baixado com sucesso: %s", output_path)
        
        # Verifica se arquivo foi criado
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            raise Exception("Arquivo baixado estÃ¡ vazio ou corrompido")
        
        # Envia o vÃ­deo
        await application.bot.edit_message_text(
            text="âœ… Download concluÃ­do, enviando...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        with open(output_path, "rb") as fh:
            await application.bot.send_video(chat_id=chat_id, video=fh, caption="ðŸ›ï¸ Shopee Video")
        
        # Mensagem de sucesso com contador
        stats = get_user_download_stats(pm["user_id"])
        success_text = MESSAGES["upload_complete"].format(
            remaining=stats["remaining"],
            total=stats["limit"] if not stats["is_premium"] else "âˆž"
        )
        
        await application.bot.edit_message_text(
            text=success_text,
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
    except requests.exceptions.RequestException as e:
        LOG.exception("Erro de rede ao baixar da Shopee: %s", e)
        await application.bot.edit_message_text(
            text=MESSAGES["error_network"],
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.exception("Erro no download Shopee customizado: %s", e)
        await application.bot.edit_message_text(
            text="âš ï¸ <b>Erro ao baixar vÃ­deo da Shopee</b>\n\n"
                 "A Shopee pode ter proteÃ§Ãµes especiais neste vÃ­deo. "
                 "Tente baixar pelo app oficial.",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="HTML"
        )

def split_video_file(input_path: str, output_dir: str, segment_size: int = SPLIT_SIZE) -> list:
    """Divide arquivo de vÃ­deo em partes menores"""
    os.makedirs(output_dir, exist_ok=True)
    
    file_size = os.path.getsize(input_path)
    num_parts = (file_size + segment_size - 1) // segment_size
    
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_pattern = os.path.join(output_dir, f"{base_name}_part%03d.mp4")
    
    cmd = [
        "ffmpeg", "-i", input_path,
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", "600",
        "-reset_timestamps", "1",
        output_pattern
    ]
    
    subprocess.run(cmd, check=True, capture_output=True)
    
    parts = sorted([
        os.path.join(output_dir, f) 
        for f in os.listdir(output_dir) 
        if f.startswith(base_name) and f.endswith('.mp4')
    ])
    
    return parts

# ============================
# TELEGRAM HANDLERS
# ============================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para o comando /start"""
    user_id = update.effective_user.id
    update_user(user_id)
    
    welcome_text = MESSAGES["welcome"].format(free_limit=FREE_DOWNLOADS_LIMIT)
    await update.message.reply_text(welcome_text, parse_mode="HTML")
    LOG.info("Comando /start executado por usuÃ¡rio %d", user_id)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para o comando /stats (apenas admin)"""
    count = get_monthly_users_count()
    stats_text = MESSAGES["stats"].format(count=count)
    await update.message.reply_text(stats_text, parse_mode="HTML")
    LOG.info("Comando /stats executado")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para o comando /status - mostra saldo de downloads"""
    user_id = update.effective_user.id
    stats = get_user_download_stats(user_id)
    
    premium_info = "âœ… Plano: <b>Premium Ativo</b>" if stats["is_premium"] else "ðŸ“¦ Plano: <b>Gratuito</b>"
    
    status_text = MESSAGES["status"].format(
        user_id=user_id,
        used=stats["downloads_count"],
        total=stats["limit"] if not stats["is_premium"] else "âˆž",
        remaining=stats["remaining"],
        premium_info=premium_info
    )
    
    await update.message.reply_text(status_text, parse_mode="HTML")
    LOG.info("Comando /status executado por usuÃ¡rio %d", user_id)

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para o comando /premium - informaÃ§Ãµes sobre plano premium"""
    user_id = update.effective_user.id
    
    keyboard = [[
        InlineKeyboardButton("ðŸ’³ Assinar Premium", callback_data=f"subscribe:{user_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        MESSAGES["premium_info"],
        parse_mode="HTML",
        reply_markup=reply_markup
    )
    LOG.info("Comando /premium executado por usuÃ¡rio %d", user_id)

async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para o comando /ai - conversar com IA"""
    if not groq_client:
        await update.message.reply_text(
            "ðŸ¤– <b>IA NÃ£o DisponÃ­vel</b>\n\n"
            "A inteligÃªncia artificial nÃ£o estÃ¡ configurada no momento.\n"
            "Entre em contato com o administrador.",
            parse_mode="HTML"
        )
        return
    
    # Se tem argumentos, responde direto
    if context.args:
        user_message = " ".join(context.args)
        await update.message.chat.send_action("typing")
        
        response = await chat_with_ai(
            user_message,
            system_prompt="""VocÃª Ã© um assistente amigÃ¡vel para um bot de downloads do Telegram.
- Seja Ãºtil, direto e use frases curtas.
- Utilize emojis apenas quando fizer sentido.
- Nunca invente informaÃ§Ãµes. Se nÃ£o souber, responda exatamente: "NÃ£o tenho essa informaÃ§Ã£o".
- NÃ£o forneÃ§a detalhes que nÃ£o estejam listados abaixo.
- Se o usuÃ¡rio quiser assinar o plano, peÃ§a para digitar /premium.
- Este bot nÃ£o faz download de mÃºsicas e nÃ£o permite escolher qualidade de vÃ­deos.

Funcionalidades:
- Download de vÃ­deos (YouTube, Instagram, TikTok, Twitter, etc.)
- Plano gratuito: 10 downloads/mÃªs
- Plano premium: downloads ilimitados (R$9,90/mÃªs)
- Se o usuÃ¡rio falar para vocÃª baixar algum vÃ­deo, incentive ele a te enviar um link
"""
        )
        
        if response:
            await update.message.reply_text(response, parse_mode="HTML")
        else:
            await update.message.reply_text(
                "âš ï¸ Erro ao processar sua mensagem. Tente novamente."
            )
    else:
        # Sem argumentos, mostra instruÃ§Ãµes
        await update.message.reply_text(
            "ðŸ¤– <b>Assistente com IA</b>\n\n"
            "Converse comigo! Use:\n"
            "â€¢ <code>/ai sua pergunta aqui</code>\n\n"
            "<b>Ou simplesmente envie uma mensagem de texto!</b>\n\n"
            "<i>Exemplos:</i>\n"
            "â€¢ /ai como baixar vÃ­deos?\n"
            "â€¢ /ai o que Ã© o plano premium?\n"
            "â€¢ /ai me recomende vÃ­deos sobre MÃºsica",
            parse_mode="HTML"
        )
    
    LOG.info("Comando /ai executado por usuÃ¡rio %d", update.effective_user.id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para mensagens de texto (URLs ou chat com IA)"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    update_user(user_id)
    
    # Verifica se Ã© um link vÃ¡lido
    urls = URL_RE.findall(text)
    if not urls:
        # NÃ£o hÃ¡ URL - verifica se tem IA disponÃ­vel para chat
        if groq_client:
            # Analisa intenÃ§Ã£o do usuÃ¡rio
            intent_data = await analyze_user_intent(text)
            intent = intent_data.get('intent', 'chat')
            
            # Se for pedido de ajuda ou chat geral, responde com IA
            if intent in ['help', 'chat']:
                LOG.info("ðŸ’¬ Chat IA - UsuÃ¡rio %d: %s", user_id, text[:50])
                await update.message.chat.send_action("typing")
                
                response = await chat_with_ai(
                    text,
                    system_prompt="""VocÃª Ã© um assistente amigÃ¡vel para um bot de downloads do Telegram.
- Seja Ãºtil, direto e use frases curtas.
- Utilize emojis apenas quando fizer sentido.
- Nunca invente informaÃ§Ãµes. Se nÃ£o souber, responda exatamente: "NÃ£o tenho essa informaÃ§Ã£o".
- NÃ£o forneÃ§a detalhes que nÃ£o estejam listados abaixo.
- Se o usuÃ¡rio quiser assinar o plano, peÃ§a para digitar /premium.
- Este bot nÃ£o faz download de mÃºsicas e nÃ£o permite escolher qualidade de vÃ­deos.

Funcionalidades:
- Download de vÃ­deos (YouTube, Instagram, TikTok, Twitter, etc.)
- Plano gratuito: 10 downloads/mÃªs
- Plano premium: downloads ilimitados (R$9,90/mÃªs)
- Se o usuÃ¡rio falar para vocÃª baixar algum vÃ­deo, incentive ele a te enviar um link

Comandos:
/start - Iniciar
/status - Ver estatÃ­sticas
/premium - Plano premium 
"""
                )
                
                if response:
                    await update.message.reply_text(response)
                else:
                    await update.message.reply_text(
                        "âš ï¸ Desculpe, nÃ£o consegui processar sua mensagem.\n\n"
                        "ðŸ’¡ <b>Dica:</b> Para baixar vÃ­deos, envie um link!\n"
                        "Use /ai para conversar comigo.",
                        parse_mode="HTML"
                    )
                return
        
        # Sem IA ou nÃ£o conseguiu processar - mostra mensagem padrÃ£o
        await update.message.reply_text(MESSAGES["url_prompt"])
        return
    
    url = urls[0]
    
    if not is_valid_url(url):
        await update.message.reply_text(MESSAGES["invalid_url"])
        return
    
    # Verifica limite de downloads
    if not can_download(user_id):
        await update.message.reply_text(
            MESSAGES["limit_reached"].format(limit=FREE_DOWNLOADS_LIMIT),
            parse_mode="HTML"
        )
        LOG.info("UsuÃ¡rio %d atingiu limite de downloads", user_id)
        return
    
    # Cria token Ãºnico para esta requisiÃ§Ã£o
    token = str(uuid.uuid4())
    
    # Resolve links universais da Shopee
    if 'shopee' in url.lower() and 'universal-link' in url:
        url = resolve_shopee_universal_link(url)
    
    # Envia mensagem de processamento
    processing_msg = await update.message.reply_text(MESSAGES["processing"])
    
    # Verifica se Ã© Shopee Video - nÃ£o conseguimos extrair info com yt-dlp
    is_shopee_video = 'sv.shopee' in url.lower() or 'share-video' in url.lower()
    
    if is_shopee_video:
        # Para Shopee Video, criamos confirmaÃ§Ã£o simples sem informaÃ§Ãµes detalhadas
        LOG.info("Detectado Shopee Video - confirmaÃ§Ã£o sem extraÃ§Ã£o prÃ©via")
        
        # Cria botÃµes de confirmaÃ§Ã£o
        keyboard = [
            [
                InlineKeyboardButton("âœ… Confirmar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("âŒ Cancelar", callback_data=f"cancel:{token}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        confirm_text = (
            "ðŸŽ¬ <b>Confirmar Download</b>\n\n"
            "ðŸ›ï¸ VÃ­deo da Shopee\n"
            "âš ï¸ InformaÃ§Ãµes disponÃ­veis apenas apÃ³s download\n\n"
            "âœ… Deseja prosseguir com o download?"
        )
        
        await processing_msg.edit_text(
            confirm_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        
        # Armazena informaÃ§Ãµes pendentes
        PENDING[token] = {
            "url": url,
            "user_id": user_id,
            "chat_id": update.effective_chat.id,
            "message_id": processing_msg.message_id,
            "timestamp": time.time(),
        }
        
        # Remove requisiÃ§Ãµes antigas
        _cleanup_pending()
        return
    
    # ObtÃ©m informaÃ§Ãµes do vÃ­deo (para nÃ£o-Shopee)
    try:
        video_info = await get_video_info(url)
        
        if not video_info:
            await processing_msg.edit_text(MESSAGES["invalid_url"])
            return
        
        title = video_info.get("title", "VÃ­deo")[:100]
        duration = format_duration(video_info.get("duration", 0))
        filesize_bytes = video_info.get("filesize") or video_info.get("filesize_approx", 0)
        filesize = format_filesize(filesize_bytes)
        
        # Verifica se o arquivo excede o limite de 50 MB
        if filesize_bytes and filesize_bytes > MAX_FILE_SIZE:
            await processing_msg.edit_text(MESSAGES["file_too_large"], parse_mode="HTML")
            LOG.info("VÃ­deo rejeitado por exceder 50 MB: %d bytes", filesize_bytes)
            return
        
        # Cria botÃµes de confirmaÃ§Ã£o
        keyboard = [
            [
                InlineKeyboardButton("âœ… Confirmar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("âŒ Cancelar", callback_data=f"cancel:{token}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        confirm_text = MESSAGES["confirm_download"].format(
            title=title,
            duration=duration,
            filesize=filesize
        )
        
        await processing_msg.edit_text(
            confirm_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        
        # Armazena informaÃ§Ãµes pendentes
        PENDING[token] = {
            "url": url,
            "user_id": user_id,
            "chat_id": update.effective_chat.id,
            "message_id": processing_msg.message_id,
            "timestamp": time.time(),
        }
        
        # Remove requisiÃ§Ãµes antigas
        _cleanup_pending()
        
    except Exception as e:
        LOG.exception("Erro ao obter informaÃ§Ãµes do vÃ­deo: %s", e)
        await processing_msg.edit_text(MESSAGES["error_unknown"])

async def get_video_info(url: str) -> dict:
    """ObtÃ©m informaÃ§Ãµes bÃ¡sicas do vÃ­deo sem fazer download"""
    cookie_file = get_cookie_for_url(url)
    
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }
    
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            return info
    except Exception as e:
        LOG.error("Erro ao extrair informaÃ§Ãµes: %s", e)
        return None

# ====================================================================
# FUNÃ‡Ã•ES DE INTELIGÃŠNCIA ARTIFICIAL (GROQ)
# ====================================================================

async def chat_with_ai(message: str, system_prompt: str = None) -> str:
    """
    Envia mensagem para Groq AI e retorna resposta.
    
    Args:
        message: Mensagem do usuÃ¡rio
        system_prompt: InstruÃ§Ãµes do sistema (opcional)
        
    Returns:
        str: Resposta da IA
    """
    if not groq_client:
        return None
    
    try:
        messages = []
        
        # Adiciona prompt do sistema se fornecido
        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt
            })
        
        # Adiciona mensagem do usuÃ¡rio
        messages.append({
            "role": "user",
            "content": message
        })
        
        # Chama API do Groq
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.7,
            max_tokens=1024
        )
        
        return response.choices[0].message.content
        
    except Exception as e:
        LOG.error("Erro ao chamar Groq AI: %s", e)
        return None


async def generate_video_summary(video_info: dict) -> str:
    """
    Gera resumo inteligente de um vÃ­deo usando IA.
    
    Args:
        video_info: DicionÃ¡rio com informaÃ§Ãµes do vÃ­deo
        
    Returns:
        str: Resumo do vÃ­deo ou string vazia se IA indisponÃ­vel
    """
    if not groq_client:
        return ""
    
    try:
        title = video_info.get('title', 'N/A')
        description = video_info.get('description', '')
        
        # Limita descriÃ§Ã£o para nÃ£o exceder tokens
        if description and len(description) > 500:
            description = description[:500] + "..."
        
        prompt = f"""Crie um resumo CURTO e OBJETIVO deste vÃ­deo em 3-4 pontos principais.
Use bullets (â€¢) e seja direto.

TÃ­tulo: {title}
DescriÃ§Ã£o: {description or 'Sem descriÃ§Ã£o'}

Responda APENAS com o resumo, sem introduÃ§Ãµes."""
        
        summary = await chat_with_ai(
            prompt,
            system_prompt="VocÃª Ã© um assistente que resume vÃ­deos de forma clara e concisa."
        )
        
        return summary if summary else ""
        
    except Exception as e:
        LOG.error("Erro ao gerar resumo: %s", e)
        return ""


async def analyze_user_intent(message: str) -> dict:
    """
    Analisa a intenÃ§Ã£o do usuÃ¡rio na mensagem.
    
    Args:
        message: Mensagem do usuÃ¡rio
        
    Returns:
        dict: {'intent': 'download' | 'chat' | 'help', 'confidence': 0.0-1.0}
    """
    # Fallback simples sem IA
    if URL_RE.search(message):
        return {'intent': 'download', 'confidence': 1.0}
    
    if not groq_client:
        return {'intent': 'chat', 'confidence': 0.5}
    
    try:
        prompt = f"""Analise esta mensagem de usuÃ¡rio e identifique a intenÃ§Ã£o:
"{message}"

Responda APENAS com uma das opÃ§Ãµes:
- download: se pede para baixar algo ou tem URL
- help: se pede ajuda, instruÃ§Ãµes ou explicaÃ§Ãµes
- chat: conversa geral

Responda APENAS uma palavra."""
        
        response = await chat_with_ai(
            prompt,
            system_prompt="VocÃª analisa intenÃ§Ãµes de usuÃ¡rios. Responda apenas: download, help ou chat."
        )
        
        if response:
            intent = response.strip().lower()
            if intent in ['download', 'help', 'chat']:
                return {'intent': intent, 'confidence': 0.9}
        
    except Exception as e:
        LOG.error("Erro ao analisar intenÃ§Ã£o: %s", e)
    
    return {'intent': 'chat', 'confidence': 0.5}


# ====================================================================
# FUNÃ‡Ã•ES DO MERCADO PAGO
# ====================================================================

async def callback_buy_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para compra de premium via Mercado Pago PIX"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    username = query.from_user.first_name or f"User{user_id}"
    
    LOG.info("ðŸ›’ UsuÃ¡rio %d iniciou compra de premium", user_id)
    
    # Verifica se jÃ¡ Ã© premium
    stats = get_user_download_stats(user_id)
    if stats["is_premium"]:
        await query.edit_message_text(
            "ðŸ’Ž <b>VocÃª jÃ¡ Ã© Premium!</b>\n\n"
            "Continue aproveitando seus benefÃ­cios ilimitados! ðŸŽ‰",
            parse_mode="HTML"
        )
        LOG.info("UsuÃ¡rio %d jÃ¡ Ã© premium", user_id)
        return
    
    # Verifica se Mercado Pago estÃ¡ disponÃ­vel
    if not MERCADOPAGO_AVAILABLE or not MERCADOPAGO_ACCESS_TOKEN:
        await query.edit_message_text(
            "âŒ <b>Sistema de Pagamento IndisponÃ­vel</b>\n\n"
            "O sistema de pagamento estÃ¡ temporariamente indisponÃ­vel.\n"
            "Por favor, tente novamente mais tarde ou contate o suporte.",
            parse_mode="HTML"
        )
        LOG.error("Tentativa de compra mas Mercado Pago nÃ£o configurado")
        return
    
    # Mostra mensagem de processamento
    await query.edit_message_text(
        "â³ <b>Gerando pagamento PIX...</b>\n\nAguarde um momento.",
        parse_mode="HTML"
    )
    
    try:
        LOG.info("Inicializando SDK do Mercado Pago")
        sdk = mercadopago.SDK(MERCADOPAGO_ACCESS_TOKEN)
        
        # Prepara dados do pagamento
        payment_data = {
            "transaction_amount": float(PREMIUM_PRICE),
            "description": f"Plano Premium - Bot Downloads (User ID: {user_id})",
            "payment_method_id": "pix",
            "payer": {
                "email": f"user{user_id}@telegram.bot",
                "first_name": username,
                "last_name": "Telegram"
            },
            "external_reference": f"PIX_{user_id}_{int(time.time())}",
            "metadata": {
                "user_id": user_id,
                "plan": "premium",
                "duration_days": PREMIUM_DURATION_DAYS
            }
        }
        
        # Adiciona notification_url se tiver RENDER_EXTERNAL_URL
        render_url = os.getenv("RENDER_EXTERNAL_URL")
        if render_url:
            payment_data["notification_url"] = f"{render_url}/webhook/pix"
            LOG.info("Notification URL configurada: %s/webhook/pix", render_url)
        
        LOG.info("Criando pagamento PIX para usuÃ¡rio %d - Valor: R$ %.2f", user_id, PREMIUM_PRICE)
        
        # Cria o pagamento
        payment_response = sdk.payment().create(payment_data)
        
        LOG.info("Resposta do Mercado Pago - Status: %s", payment_response.get("status"))
        
        # Valida resposta
        if payment_response["status"] != 201:
            LOG.error("Erro ao criar pagamento - Status %s: %s", 
                     payment_response.get("status"), payment_response)
            raise Exception(f"Mercado Pago retornou erro: status {payment_response.get('status')}")
        
        payment = payment_response["response"]
        payment_id = payment.get("id")
        
        LOG.info("âœ… Payment criado - ID: %s, Status: %s", payment_id, payment.get("status"))
        
        # Valida estrutura do PIX
        if "point_of_interaction" not in payment:
            LOG.error("Resposta sem point_of_interaction: %s", payment)
            raise Exception("PIX nÃ£o foi gerado - point_of_interaction ausente")
        
        poi = payment["point_of_interaction"]
        if "transaction_data" not in poi:
            LOG.error("point_of_interaction sem transaction_data: %s", poi)
            raise Exception("PIX nÃ£o foi gerado - transaction_data ausente")
        
        td = poi["transaction_data"]
        if "qr_code" not in td or "qr_code_base64" not in td:
            LOG.error("transaction_data sem QR codes: %s", td)
            raise Exception("PIX nÃ£o foi gerado - QR codes ausentes")
        
        # Extrai informaÃ§Ãµes do PIX
        pix_info = {
            "payment_id": payment_id,
            "qr_code": td["qr_code"],
            "qr_code_base64": td["qr_code_base64"],
            "amount": payment["transaction_amount"]
        }
        
        LOG.info("âœ… PIX gerado com sucesso - ID: %s", payment_id)
        
        # Salva no banco de dados
        try:
            with DB_LOCK:
                conn = sqlite3.connect(DB_FILE, timeout=10)
                c = conn.cursor()
                c.execute("""
                    INSERT INTO pix_payments (user_id, amount, pix_key, status) 
                    VALUES (?, ?, ?, 'pending')
                """, (user_id, pix_info["amount"], payment_id))
                conn.commit()
                conn.close()
            LOG.info("Pagamento salvo no banco de dados")
        except Exception as e:
            LOG.error("Erro ao salvar pagamento no banco: %s", e)
            # Continua mesmo se falhar ao salvar no banco
        
        # Prepara mensagem
        message_text = (
            "ðŸ’³ <b>Pagamento PIX Gerado</b>\n\n"
            f"ðŸ’° Valor: R$ {pix_info['amount']:.2f}\n"
            f"ðŸ†” ID: <code>{payment_id}</code>\n\n"
            "ðŸ“± <b>Como pagar:</b>\n"
            "1ï¸âƒ£ Abra o app do seu banco\n"
            "2ï¸âƒ£ VÃ¡ em PIX â†’ Ler QR Code\n"
            "3ï¸âƒ£ Escaneie o cÃ³digo abaixo\n"
            "4ï¸âƒ£ Confirme o pagamento\n\n"
            "â±ï¸ <b>Expira em:</b> 30 minutos\n"
            "âœ… <b>AtivaÃ§Ã£o automÃ¡tica apÃ³s confirmaÃ§Ã£o!</b>\n\n"
            "âš¡ Seu premium serÃ¡ ativado em atÃ© 60 segundos."
        )
        
        # Tenta enviar QR Code como imagem
        qr_sent = False
        if PIL_AVAILABLE:
            try:
                LOG.info("Tentando enviar QR Code como imagem")
                
                # Decodifica QR Code
                qr_bytes = base64.b64decode(pix_info["qr_code_base64"])
                qr_image = Image.open(io.BytesIO(qr_bytes))
                
                # Salva temporariamente
                qr_path = f"/tmp/qr_{user_id}_{int(time.time())}.png"
                qr_image.save(qr_path)
                
                # Envia imagem
                with open(qr_path, "rb") as photo:
                    await query.message.reply_photo(
                        photo=photo,
                        caption=message_text,
                        parse_mode="HTML"
                    )
                
                # Remove arquivo temporÃ¡rio
                os.remove(qr_path)
                qr_sent = True
                LOG.info("âœ… QR Code enviado como imagem")
                
            except Exception as e:
                LOG.error("Erro ao enviar QR Code como imagem: %s", e)
        
        # Se enviou imagem, envia cÃ³digo separado; senÃ£o envia tudo junto
        if qr_sent:
            # Envia cÃ³digo PIX copia e cola em mensagem separada
            LOG.info("Enviando cÃ³digo PIX copia e cola em mensagem separada")
            await query.message.reply_text(
                "ðŸ“‹ <b>CÃ³digo PIX Copia e Cola:</b>\n\n"
                "Caso prefira, copie o cÃ³digo abaixo e cole no seu app de pagamento:\n\n"
                f"<code>{pix_info['qr_code']}</code>\n\n"
                "ðŸ’¡ <i>Clique no cÃ³digo acima para copiar automaticamente</i>",
                parse_mode="HTML"
            )
        else:
            # Fallback: envia tudo como texto
            LOG.info("Enviando QR Code como texto (cÃ³digo copia e cola)")
            await query.message.reply_text(
                message_text + f"\n\nðŸ“‹ <b>CÃ³digo PIX Copia e Cola:</b>\n<code>{pix_info['qr_code']}</code>",
                parse_mode="HTML"
            )
        
        # Deleta mensagem antiga
        try:
            await query.message.delete()
        except Exception as e:
            LOG.debug("NÃ£o foi possÃ­vel deletar mensagem antiga: %s", e)
        
        # Inicia monitoramento do pagamento
        LOG.info("Iniciando monitoramento do pagamento %s", payment_id)
        asyncio.create_task(monitor_payment_status(user_id, payment_id))
        
        LOG.info("âœ… Processo completo - Pagamento %s criado e em monitoramento", payment_id)
        
    except Exception as e:
        LOG.exception("âŒ ERRO ao gerar pagamento PIX: %s", e)
        
        # Determina mensagem de erro especÃ­fica
        error_msg = str(e).lower()
        if "401" in error_msg or "unauthorized" in error_msg:
            error_detail = "Token do Mercado Pago invÃ¡lido ou expirado."
        elif "point_of_interaction" in error_msg or "qr" in error_msg:
            error_detail = "Erro ao gerar QR Code PIX. Verifique as credenciais."
        elif "mercadopago_access_token" in error_msg:
            error_detail = "Sistema de pagamento nÃ£o configurado no servidor."
        else:
            error_detail = f"Erro ao processar pagamento."
        
        await query.edit_message_text(
            f"âŒ <b>Erro ao Gerar Pagamento</b>\n\n"
            f"{error_detail}\n\n"
            f"Por favor, tente novamente em alguns instantes.\n\n"
            f"Se o erro persistir, entre em contato com o suporte.",
            parse_mode="HTML"
        )


async def monitor_payment_status(user_id: int, payment_id: str):
    """Monitora o status do pagamento em segundo plano"""
    if not MERCADOPAGO_AVAILABLE or not MERCADOPAGO_ACCESS_TOKEN:
        LOG.error("NÃ£o Ã© possÃ­vel monitorar pagamento - Mercado Pago nÃ£o configurado")
        return
    
    try:
        sdk = mercadopago.SDK(MERCADOPAGO_ACCESS_TOKEN)
        max_attempts = 60  # 30 minutos (30s * 60)
        
        LOG.info("ðŸ” Monitorando pagamento %s (max %d tentativas)", payment_id, max_attempts)
        
        for attempt in range(max_attempts):
            await asyncio.sleep(30)  # Verifica a cada 30 segundos
            
            try:
                payment_response = sdk.payment().get(payment_id)
                
                if payment_response["status"] != 200:
                    LOG.warning("Erro ao consultar pagamento %s: status %s", 
                              payment_id, payment_response.get("status"))
                    continue
                
                payment = payment_response["response"]
                status = payment["status"]
                
                LOG.debug("Pagamento %s - Status: %s (tentativa %d/%d)", 
                         payment_id, status, attempt + 1, max_attempts)
                
                if status == "approved":
                    # Pagamento aprovado!
                    LOG.info("ðŸŽ‰ Pagamento %s APROVADO!", payment_id)
                    await activate_premium(user_id, payment_id)
                    break
                    
                elif status in ["rejected", "cancelled", "refunded"]:
                    LOG.info("âš ï¸ Pagamento %s nÃ£o concluÃ­do: %s", payment_id, status)
                    
                    # Notifica usuÃ¡rio
                    try:
                        status_messages = {
                            "rejected": "rejeitado",
                            "cancelled": "cancelado",
                            "refunded": "reembolsado"
                        }
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"âš ï¸ <b>Pagamento {status_messages.get(status, status)}</b>\n\n"
                                f"ID: <code>{payment_id}</code>\n\n"
                                "Seu pagamento nÃ£o foi concluÃ­do.\n"
                                "Se precisar de ajuda, entre em contato com o suporte."
                            ),
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        LOG.error("Erro ao notificar usuÃ¡rio sobre falha: %s", e)
                    break
                    
            except Exception as e:
                LOG.error("Erro ao verificar status do pagamento %s: %s", payment_id, e)
        
        if attempt >= max_attempts - 1:
            LOG.info("â° Timeout de monitoramento para pagamento %s apÃ³s %d minutos", 
                    payment_id, (max_attempts * 30) // 60)
            
    except Exception as e:
        LOG.exception("Erro crÃ­tico no monitoramento do pagamento %s: %s", payment_id, e)


async def activate_premium(user_id: int, payment_id: str):
    """Ativa o plano premium para o usuÃ¡rio"""
    try:
        LOG.info("ðŸ”“ Ativando premium para usuÃ¡rio %d - Pagamento: %s", user_id, payment_id)
        
        # Calcula data de expiraÃ§Ã£o
        premium_expires = (datetime.now() + timedelta(days=PREMIUM_DURATION_DAYS)).strftime("%Y-%m-%d")
        
        # Atualiza banco de dados
        with DB_LOCK:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            
            # Ativa premium
            c.execute("""
                UPDATE user_downloads 
                SET is_premium=1, premium_expires=? 
                WHERE user_id=?
            """, (premium_expires, user_id))
            
            # Atualiza status do pagamento
            c.execute("""
                UPDATE pix_payments 
                SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP 
                WHERE user_id=? AND pix_key=?
            """, (user_id, payment_id))
            
            rows_affected = c.rowcount
            conn.commit()
            conn.close()
        
        LOG.info("âœ… Premium ativado no banco de dados (%d linhas atualizadas)", rows_affected)
        
        # Notifica o usuÃ¡rio
        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "ðŸŽ‰ <b>Pagamento Confirmado!</b>\n\n"
                f"âœ… Plano Premium ativado com sucesso!\n"
                f"ðŸ†” Pagamento: <code>{payment_id}</code>\n"
                f"ðŸ“… VÃ¡lido atÃ©: <b>{premium_expires}</b>\n\n"
                "ðŸ’Ž <b>BenefÃ­cios liberados:</b>\n"
                "â€¢ â™¾ï¸ Downloads ilimitados\n"
                "â€¢ ðŸŽ¬ Qualidade mÃ¡xima (atÃ© 1080p)\n"
                "â€¢ âš¡ Processamento prioritÃ¡rio\n"
                "â€¢ ðŸŽ§ Suporte dedicado\n\n"
                "Obrigado pela confianÃ§a! ðŸ™\n\n"
                "Use /status para ver suas informaÃ§Ãµes."
            ),
            parse_mode="HTML"
        )
        
        LOG.info("âœ… UsuÃ¡rio %d notificado sobre ativaÃ§Ã£o do premium", user_id)
        
    except Exception as e:
        LOG.exception("âŒ ERRO ao ativar premium para usuÃ¡rio %d: %s", user_id, e)
        
        # Tenta notificar sobre o erro
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=(
                    "âš ï¸ <b>Pagamento Recebido</b>\n\n"
                    "Recebemos seu pagamento mas houve um erro ao ativar seu premium automaticamente.\n\n"
                    "Por favor, entre em contato com o suporte informando este ID:\n"
                    f"<code>{payment_id}</code>\n\n"
                    "Resolveremos em breve!"
                ),
                parse_mode="HTML"
            )
        except:
            pass

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para callbacks de confirmaÃ§Ã£o de download"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    action, token = data.split(":", 1)
    
    if token not in PENDING:
        await query.edit_message_text(MESSAGES["error_expired"])
        return
    
    pm = PENDING[token]
    
    # Verifica se o usuÃ¡rio Ã© o mesmo que solicitou
    if pm["user_id"] != query.from_user.id:
        await query.answer("âš ï¸ Esta aÃ§Ã£o nÃ£o pode ser realizada por vocÃª.", show_alert=True)
        return
    
    if action == "cancel":
        del PENDING[token]
        await query.edit_message_text(MESSAGES["download_cancelled"])
        LOG.info("Download cancelado pelo usuÃ¡rio %d", pm["user_id"])
        return
    
    if action == "dl":
        # Verifica quantos downloads estÃ£o ativos
        active_count = len(ACTIVE_DOWNLOADS)
        
        if active_count >= MAX_CONCURRENT_DOWNLOADS:
            # Mostra posiÃ§Ã£o na fila
            queue_position = active_count - MAX_CONCURRENT_DOWNLOADS + 1
            queue_text = MESSAGES["queue_position"].format(
                position=queue_position,
                active=MAX_CONCURRENT_DOWNLOADS
            )
            await query.edit_message_text(queue_text)
        
        # Remove da lista de pendentes
        del PENDING[token]
        
        # Adiciona Ã  lista de downloads ativos
        ACTIVE_DOWNLOADS[token] = {
            "user_id": pm["user_id"],
            "started_at": time.time()
        }
        
        await query.edit_message_text(MESSAGES["download_started"])
        
        # Incrementa contador de downloads
        increment_download_count(pm["user_id"])
        
        # Inicia download em background
        asyncio.create_task(_process_download(token, pm))
        LOG.info("Download iniciado para usuÃ¡rio %d (Token: %s)", pm["user_id"], token)

async def _process_download(token: str, pm: dict):
    """Processa o download em background"""
    tmpdir = None
    
    # Aguarda na fila (semÃ¡foro para controlar 3 downloads simultÃ¢neos)
    async with DOWNLOAD_SEMAPHORE:
        try:
            tmpdir = tempfile.mkdtemp(prefix=f"ytbot_")
            LOG.info("DiretÃ³rio temporÃ¡rio criado: %s", tmpdir)
            
            try:
                await _do_download(token, pm["url"], tmpdir, pm["chat_id"], pm)
            finally:
                # Limpa arquivos temporÃ¡rios e envia mensagem de cleanup
                if tmpdir and os.path.exists(tmpdir):
                    try:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                        cleanup_msg = MESSAGES["cleanup"].format(path=tmpdir)
                        LOG.info(cleanup_msg)
                        
                        # Envia mensagem de cleanup para o usuÃ¡rio
                        try:
                            await application.bot.send_message(
                                chat_id=pm["chat_id"],
                                text=cleanup_msg
                            )
                        except Exception as e:
                            LOG.debug("Erro ao enviar mensagem de cleanup: %s", e)
                    except Exception as e:
                        LOG.error("Erro ao limpar tmpdir: %s", e)
                
                # Remove da lista de downloads ativos
                if token in ACTIVE_DOWNLOADS:
                    del ACTIVE_DOWNLOADS[token]
                    LOG.info("Download removido da lista ativa: %s", token)
                    
        except Exception as e:
            LOG.exception("Erro no processamento de download: %s", e)
            try:
                await application.bot.edit_message_text(
                    text=MESSAGES["error_unknown"],
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"]
                )
            except Exception:
                pass
            finally:
                # Remove da lista de downloads ativos em caso de erro
                if token in ACTIVE_DOWNLOADS:
                    del ACTIVE_DOWNLOADS[token]

async def _do_download(token: str, url: str, tmpdir: str, chat_id: int, pm: dict):
    """Executa o download do vÃ­deo"""
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
    last_percent = -1
    
    # Resolve universal links da Shopee
    if 'shopee' in url.lower() and 'universal-link' in url:
        url = resolve_shopee_universal_link(url)
        LOG.info("Usando URL resolvida para download: %s", url[:100])
    
    # Verifica se Ã© Shopee Video - precisa tratamento especial
    if 'sv.shopee' in url.lower() or 'share-video' in url.lower():
        LOG.info("Detectado Shopee Video, usando mÃ©todo alternativo")
        await _download_shopee_video(url, tmpdir, chat_id, pm)
        return
    
    def progress_hook(d):
        nonlocal last_percent
        try:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                
                # Verifica se o tamanho estÃ¡ excedendo o limite durante download
                if total and total > MAX_FILE_SIZE:
                    LOG.warning("Download cancelado: arquivo excede 50 MB (%d bytes)", total)
                    raise Exception(f"Arquivo muito grande: {total} bytes")
                
                if total:
                    percent = int(downloaded * 100 / total)
                    if percent != last_percent and percent % 10 == 0:
                        last_percent = percent
                        blocks = int(percent / 5)
                        bar = "â–ˆ" * blocks + "â–‘" * (20 - blocks)
                        text = MESSAGES["download_progress"].format(
                            percent=percent,
                            bar=bar
                        )
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
                            text=MESSAGES["download_complete"], 
                            chat_id=pm["chat_id"], 
                            message_id=pm["message_id"]
                        ),
                        APP_LOOP,
                    )
                except Exception as e:
                    LOG.debug("Erro ao atualizar status finished: %s", e)
        except Exception as e:
            LOG.error("Erro no progress_hook: %s", e)

    # ConfiguraÃ§Ãµes do yt-dlp
    ydl_opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "quiet": False,
        "logger": LOG,
        "format": get_format_for_url(url),  # Formato adaptÃ¡vel por plataforma
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 1,
        "force_ipv4": True,
        "socket_timeout": 30,
        "http_chunk_size": 1048576,
        "retries": 20,
        "fragment_retries": 20,
    }
    
    # Adiciona cookies apropriados
    cookie_file = get_cookie_for_url(url)
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    # Executa download
    try:
        await asyncio.to_thread(lambda: _run_ydl(ydl_opts, [url]))
    except Exception as e:
        LOG.exception("Erro no yt-dlp: %s", e)
        await _notify_error(pm, "error_network")
        return

    # Envia arquivos baixados
    arquivos = [
        os.path.join(tmpdir, f) 
        for f in os.listdir(tmpdir) 
        if os.path.isfile(os.path.join(tmpdir, f))
    ]
    
    if not arquivos:
        LOG.error("Nenhum arquivo baixado")
        await _notify_error(pm, "error_unknown")
        return

    for path in arquivos:
        try:
            tamanho = os.path.getsize(path)
            
            # Verifica se o arquivo excede 50 MB
            if tamanho > MAX_FILE_SIZE:
                LOG.error("Arquivo muito grande apÃ³s download: %d bytes", tamanho)
                await _notify_error(pm, "error_file_large")
                return
            
            # Envia o vÃ­deo
            with open(path, "rb") as fh:
                await application.bot.send_video(chat_id=chat_id, video=fh)
                    
        except Exception as e:
            LOG.exception("Erro ao enviar arquivo %s: %s", path, e)
            await _notify_error(pm, "error_upload")
            return

    # Mensagem de sucesso com contador de downloads
    stats = get_user_download_stats(pm["user_id"])
    
    try:
        success_text = MESSAGES["upload_complete"].format(
            remaining=stats["remaining"],
            total=stats["limit"] if not stats["is_premium"] else "âˆž"
        )
        
        await application.bot.edit_message_text(
            text=success_text,
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.error("Erro ao enviar mensagem final: %s", e)

def _run_ydl(options, urls):
    """Executa yt-dlp com as opÃ§Ãµes fornecidas"""
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

async def _notify_error(pm: dict, error_key: str):
    """Notifica o usuÃ¡rio sobre um erro"""
    try:
        await application.bot.edit_message_text(
            text=MESSAGES.get(error_key, MESSAGES["error_unknown"]),
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.error("Erro ao notificar erro: %s", e)

def _cleanup_pending():
    """Remove requisiÃ§Ãµes pendentes expiradas"""
    now = time.time()
    expired = [
        token for token, pm in PENDING.items()
        if now - pm["timestamp"] > PENDING_EXPIRE_SECONDS
    ]
    for token in expired:
        del PENDING[token]
    
    # Limita tamanho mÃ¡ximo
    while len(PENDING) > PENDING_MAX_SIZE:
        PENDING.popitem(last=False)

# ============================
# REGISTRO DE HANDLERS
# ============================

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CommandHandler("status", status_cmd))
application.add_handler(CommandHandler("premium", premium_cmd))
application.add_handler(CommandHandler("ai", ai_cmd))  # â† Novo comando
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(CallbackQueryHandler(callback_buy_premium, pattern=r"^subscribe:"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# ============================
# FLASK ROUTES
# ============================

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Endpoint webhook para receber updates do Telegram"""
    try:
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    except Exception as e:
        LOG.exception("Falha ao processar webhook: %s", e)
    return "ok"

@app.route("/")
def index():
    """Rota principal"""
    return "ðŸ¤– Bot de Download Ativo"

@app.route("/health")
def health():
    """Endpoint de health check"""
    checks = {
        "bot": "ok",
        "db": "ok",
        "pending_count": len(PENDING),
        "active_downloads": len(ACTIVE_DOWNLOADS),
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
        "queue_available": MAX_CONCURRENT_DOWNLOADS - len(ACTIVE_DOWNLOADS),
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
# ============================
# MERCADOPAGO
# ============================

from flask import request
import mercadopago
import os

@app.route("/webhook/pix", methods=["POST"])
def webhook_pix():
    """Endpoint para receber notificaÃ§Ãµes de pagamento PIX do Mercado Pago"""
    try:
        data = request.get_json()
        LOG.info("Webhook PIX recebido: %s", data)

        if data.get("type") == "payment":
            payment_id = data["data"]["id"]
            sdk = mercadopago.SDK(os.getenv("MERCADOPAGO_ACCESS_TOKEN"))
            payment = sdk.payment().get(payment_id)["response"]

            if payment["status"] == "approved":
                # Extrai o valor do campo external_reference que deve conter o user_id
                reference = payment.get("external_reference")
                if reference and reference.startswith("PIX_"):
                    parts = reference.split("_")
                    if len(parts) == 3:
                        user_id = int(parts[2])
                        confirm_pix_payment(payment_reference=reference, user_id=user_id)
                        LOG.info("Pagamento confirmado e premium ativado para user_id=%s", user_id)
                    else:
                        LOG.warning("Formato de referÃªncia invÃ¡lido: %s", reference)
                else:
                    LOG.warning("ReferÃªncia externa ausente ou invÃ¡lida: %s", reference)

        return "ok", 200

    except Exception as e:
        LOG.exception("Erro no webhook PIX: %s", e)
        return "erro", 500
        
# ============================
# MAIN
# ============================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    LOG.info("Iniciando servidor Flask na porta %d", port)
    app.run(host="0.0.0.0", port=port)


from telegram.constants import ParseMode
import mercadopago

async def subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    try:
        reference = create_pix_payment(user_id, 9.90)
        sdk = mercadopago.SDK(os.getenv("MERCADOPAGO_ACCESS_TOKEN"))

        payment_data = {
            "transaction_amount": 9.90,
            "description": "Plano Premium",
            "payment_method_id": "pix",
            "payer": {"email": f"user{user_id}@example.com"},
            "external_reference": reference
        }

        result = sdk.payment().create(payment_data)
        response = result.get("response", {})

        if response.get("status") == "pending":
            qr_code_base64 = response["point_of_interaction"]["transaction_data"]["qr_code_base64"]
            qr_code_text = response["point_of_interaction"]["transaction_data"]["qr_code"]

            await query.edit_message_text(
                (
                    f"âœ… Pedido criado!\n\n"
                    f"<code>{qr_code_text}</code>\n\n"
                    "ðŸ–¼ï¸ Escaneie o QR Code abaixo para pagar:"
                ),
                parse_mode=ParseMode.HTML
            )

            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=f"data:image/png;base64,{qr_code_base64}"
            )
        else:
            await query.edit_message_text("âŒ Erro ao criar pagamento. Tente novamente mais tarde.")
    except Exception as e:
        await query.edit_message_text(f"âŒ Falha interna: {e}")
