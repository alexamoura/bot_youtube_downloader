#!/usr/bin/env python3
"""
bot_with_cookies_melhorado.py - Versão Profissional

Telegram bot IA (webhook) com sistema de controle de downloads e suporte a pagamento PIX - ATUALIZADO EM 15/11/2025 - LOGS OTIMIZADOS
"""
import os
import sys
import tempfile
import asyncio
import base64
import logging
import logging.handlers
import threading
import uuid
import re
import time
import sqlite3
import shutil
import subprocess
import gc
import glob
from collections import OrderedDict, deque
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

# ════════════════════════════════════════════════════════════════
# 🔄 SISTEMA DE AUTO-RECUPERAÇÃO E KEEPALIVE
# ════════════════════════════════════════════════════════════════

from datetime import datetime

# Configurações do sistema de keepalive
KEEPALIVE_ENABLED = os.getenv("KEEPALIVE_ENABLED", "true").lower() == "true"
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "300"))  # 5 minutos
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # URL do seu bot no Render
LAST_ACTIVITY = {"telegram": time.time(), "flask": time.time()}
INACTIVITY_THRESHOLD = 1800  # 30 minutos sem atividade = aviso

class BotHealthMonitor:
    """Monitor de saúde do bot com auto-recuperação"""
    
    def __init__(self):
        self.last_telegram_update = time.time()
        self.last_health_check = time.time()
        self.webhook_errors = 0
        self.consecutive_errors = 0
        self.max_errors_before_restart = 3
        self.max_consecutive_errors = 5
        self.is_healthy = True
        
    def record_activity(self, source: str = "telegram"):
        """Registra atividade do bot"""
        LAST_ACTIVITY[source] = time.time()
        if source == "telegram":
            self.last_telegram_update = time.time()
            self.webhook_errors = 0  # Reset contador de erros
            self.consecutive_errors = 0  # Reset erros consecutivos
    
    def check_health(self) -> dict:
        """Verifica saúde do bot e gera logs visuais"""
        now = time.time()
        telegram_inactive = now - LAST_ACTIVITY["telegram"]
        flask_inactive = now - LAST_ACTIVITY["flask"]

        status = {
            "healthy": True,
            "telegram_inactive_seconds": int(telegram_inactive),
            "flask_inactive_seconds": int(flask_inactive),
            "webhook_errors": self.webhook_errors,
            "uptime": int(now - self.last_health_check),
            "timestamp": datetime.now().isoformat()
        }

        # 🟢 Estado inicial
        health_emoji = "🟢"
        health_msg = "Tudo OK"

        # Verifica inatividade do Telegram
        if telegram_inactive > INACTIVITY_THRESHOLD:
            status["healthy"] = False
            status["issue"] = "telegram_inactive"
            health_emoji = "🟡"
            health_msg = f"Inativo há {int(telegram_inactive)}s"
            LOG.warning("⚠️ %s Bot inativo há %d segundos", health_emoji, telegram_inactive)

        # Verifica erros acumulados de webhook
        elif self.webhook_errors >= self.max_errors_before_restart:
            status["healthy"] = False
            status["issue"] = "webhook_errors"
            health_emoji = "🔴"
            health_msg = f"{self.webhook_errors} erros de webhook"
            LOG.error("🔴 Muitos erros de webhook: %d", self.webhook_errors)

        # Caso normal — tudo saudável
        else:
            # REMOVIDO: Log de "bot saudável" para não poluir
            pass

        self.is_healthy = status["healthy"]

        return status
    
    def record_error(self):
        """Registra erro no webhook"""
        self.webhook_errors += 1
        self.consecutive_errors += 1
        LOG.warning("⚠️ Erro no webhook registrado (consecutivos: %d, total: %d)", 
                    self.consecutive_errors, self.webhook_errors)
    
    def should_reconnect_webhook(self) -> bool:
        """Verifica se deve reconectar o webhook"""
        return self.consecutive_errors >= self.max_consecutive_errors

# Instância global do monitor
health_monitor = BotHealthMonitor()

async def reconnect_webhook():
    """Reconecta o webhook do Telegram quando trava"""
    if not WEBHOOK_URL:
        LOG.error("❌ WEBHOOK_URL não configurado!")
        return False
    
    try:
        webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
        LOG.info("🔧 Reconectando webhook...")
        
        # Remove webhook antigo
        await application.bot.delete_webhook(drop_pending_updates=True)
        await asyncio.sleep(2)
        
        # Configura novo webhook
        result = await application.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=False,
            max_connections=100,
            allowed_updates=["message", "callback_query"]
        )
        
        if result:
            LOG.info("✅ Webhook reconectado com sucesso!")
            health_monitor.consecutive_errors = 0
            return True
        else:
            LOG.error("❌ Falha ao reconectar webhook")
            return False
            
    except Exception as e:
        LOG.error("❌ Erro ao reconectar webhook: %s", e)
        return False

def reconnect_webhook_sync():
    """Versão síncrona para chamar de threads"""
    try:
        future = asyncio.run_coroutine_threadsafe(reconnect_webhook(), APP_LOOP)
        return future.result(timeout=15)
    except Exception as e:
        LOG.error("❌ Erro na reconexão síncrona: %s", e)
        return False

def keepalive_routine():
    """
    Rotina de keepalive que:
    1. Faz ping no próprio bot a cada 5 minutos
    2. Verifica saúde do webhook
    3. Tenta reconfigurar webhook se necessário
    """
    if not KEEPALIVE_ENABLED:
        LOG.info("⚠️ Keepalive desabilitado")
        return
    
    while True:
        try:
            time.sleep(KEEPALIVE_INTERVAL)
            
            # 1. Verifica saúde
            health = health_monitor.check_health()
            
            # Verifica se deve reconectar (após muitos erros consecutivos)
            if health_monitor.should_reconnect_webhook():
                LOG.error("🔴 Muitos erros consecutivos detectados!")
                if WEBHOOK_URL:
                    try:
                        LOG.info("🔧 Tentando reconectar webhook...")
                        if reconnect_webhook_sync():
                            LOG.info("✅ Webhook reconectado com sucesso!")
                        else:
                            LOG.error("❌ Falha na reconexão do webhook")
                    except Exception as e:
                        LOG.error("❌ Erro ao tentar reconectar: %s", e)
            
            # 2. Keepalive ping (apenas se o webhook estiver configurado)
            if WEBHOOK_URL and health.get("healthy", True):
                try:
                    # Ping básico na rota /health
                    response = requests.get(f"{WEBHOOK_URL}/health", timeout=10)
                    # REMOVIDO: Log de keepalive bem-sucedido para não poluir
                except requests.Timeout:
                    LOG.warning("⚠️ Timeout no keepalive ping")
                except Exception as e:
                    LOG.debug("Erro no keepalive ping (normal se local): %s", e)
                    
        except Exception as e:
            LOG.error("❌ Erro na rotina de keepalive: %s", e)

def webhook_watchdog():
    """
    Thread que monitora o webhook e tenta reconectar se detectar problemas
    """
    consecutive_failures = 0
    max_failures = 3
    
    while True:
        try:
            time.sleep(60)  # Verifica a cada 1 minuto
            
            # Verifica se há inatividade suspeita
            inactive_time = time.time() - LAST_ACTIVITY["telegram"]
            
            if inactive_time > 600:  # 10 minutos sem updates
                consecutive_failures += 1
                LOG.warning("⚠️ Webhook inativo há %d segundos (falhas: %d/%d)", 
                           int(inactive_time), consecutive_failures, max_failures)
                
                if consecutive_failures >= max_failures:
                    LOG.error("🔴 Webhook pode estar travado! Tentando reconectar...")
                    if reconnect_webhook_sync():
                        LOG.info("✅ Reconexão bem-sucedida!")
                        consecutive_failures = 0
                    else:
                        LOG.error("❌ Reconexão falhou")
            else:
                consecutive_failures = 0  # Reset se recebeu updates
                
        except Exception as e:
            LOG.error("❌ Erro no watchdog: %s", e)

# ════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO DE LOGGING
# ════════════════════════════════════════════════════════════════

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)  # Nível INFO para logs profissionais

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Formato profissional: timestamp + level + mensagem
formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler.setFormatter(formatter)
LOG.addHandler(console_handler)

# ════════════════════════════════════════════════════════════════
# VARIÁVEIS DE AMBIENTE E CONSTANTES
# ════════════════════════════════════════════════════════════════

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    LOG.error("❌ TELEGRAM_TOKEN não definido!")
    sys.exit(1)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
PREMIUM_PRICE = float(os.getenv("PREMIUM_PRICE", "4.99"))

# Configurações de download
MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", "100"))
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
FILE_SIZE_WARNING_THRESHOLD = 50 * 1024 * 1024

# Configurações de rate limiting
ENABLE_RATE_LIMITING = os.getenv("ENABLE_RATE_LIMITING", "true").lower() == "true"
MAX_REQUESTS_PER_MINUTE = int(os.getenv("MAX_REQUESTS_PER_MINUTE", "10"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

# Controle de concorrência
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))

# ════════════════════════════════════════════════════════════════
# BANCO DE DADOS
# ════════════════════════════════════════════════════════════════

DB_PATH = os.getenv("DB_PATH", "/tmp/bot_database.db")

def init_db():
    """Inicializa o banco de dados SQLite"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_premium INTEGER DEFAULT 0,
            premium_until TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            reference TEXT UNIQUE,
            amount REAL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)
    
    conn.commit()
    conn.close()
    LOG.info("✅ Banco de dados inicializado")

init_db()

def get_user_from_db(user_id: int) -> dict:
    """Busca dados do usuário no banco"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            "user_id": row[0],
            "username": row[1],
            "first_name": row[2],
            "is_premium": bool(row[3]),
            "premium_until": row[4],
            "created_at": row[5]
        }
    return None

def create_or_update_user(user_id: int, username: str = None, first_name: str = None):
    """Cria ou atualiza usuário no banco"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO users (user_id, username, first_name) 
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
    """, (user_id, username, first_name))
    
    conn.commit()
    conn.close()

def is_premium_user(user_id: int) -> bool:
    """Verifica se o usuário é premium"""
    user = get_user_from_db(user_id)
    if not user or not user["is_premium"]:
        return False
    
    if user["premium_until"]:
        premium_until = datetime.fromisoformat(user["premium_until"])
        return datetime.now() < premium_until
    
    return False

def set_premium_status(user_id: int, days: int = 30):
    """Define status premium do usuário"""
    premium_until = (datetime.now() + timedelta(days=days)).isoformat()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET is_premium = 1, premium_until = ?
        WHERE user_id = ?
    """, (premium_until, user_id))
    conn.commit()
    conn.close()
    LOG.info("✅ Usuário %d agora é premium até %s", user_id, premium_until)

# ════════════════════════════════════════════════════════════════
# SISTEMA DE PAGAMENTOS PIX (MERCADO PAGO)
# ════════════════════════════════════════════════════════════════

def create_pix_payment(user_id: int, amount: float) -> str:
    """Cria uma referência de pagamento PIX"""
    reference = f"USER{user_id}_{uuid.uuid4().hex[:8]}"
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO payments (user_id, reference, amount, status)
        VALUES (?, ?, ?, 'pending')
    """, (user_id, reference, amount))
    conn.commit()
    conn.close()
    
    return reference

def update_payment_status(reference: str, status: str):
    """Atualiza status do pagamento"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE payments 
        SET status = ?
        WHERE reference = ?
    """, (status, reference))
    conn.commit()
    conn.close()

def get_payment_by_reference(reference: str):
    """Busca pagamento pela referência"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM payments WHERE reference = ?", (reference,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            "id": row[0],
            "user_id": row[1],
            "reference": row[2],
            "amount": row[3],
            "status": row[4],
            "created_at": row[5]
        }
    return None

# ════════════════════════════════════════════════════════════════
# RATE LIMITING
# ════════════════════════════════════════════════════════════════

class RateLimiter:
    """Sistema de rate limiting por usuário"""
    
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = {}  # {user_id: deque of timestamps}
        self.lock = threading.Lock()
    
    def is_allowed(self, user_id: int) -> bool:
        """Verifica se o usuário pode fazer uma requisição"""
        if not ENABLE_RATE_LIMITING:
            return True
        
        with self.lock:
            now = time.time()
            
            if user_id not in self.requests:
                self.requests[user_id] = deque()
            
            # Remove requisições antigas
            user_requests = self.requests[user_id]
            while user_requests and user_requests[0] < now - self.window_seconds:
                user_requests.popleft()
            
            # Verifica se excedeu o limite
            if len(user_requests) >= self.max_requests:
                return False
            
            # Adiciona nova requisição
            user_requests.append(now)
            return True
    
    def get_wait_time(self, user_id: int) -> int:
        """Retorna tempo de espera em segundos"""
        with self.lock:
            if user_id not in self.requests or not self.requests[user_id]:
                return 0
            
            oldest_request = self.requests[user_id][0]
            wait_time = int(self.window_seconds - (time.time() - oldest_request))
            return max(0, wait_time)

rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE, RATE_LIMIT_WINDOW)

# ════════════════════════════════════════════════════════════════
# CONTROLE DE CONCORRÊNCIA
# ════════════════════════════════════════════════════════════════

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
active_downloads = {}  # {user_id: {"url": str, "start_time": float}}

@contextmanager
def track_download(user_id: int, url: str):
    """Context manager para rastrear downloads ativos"""
    active_downloads[user_id] = {
        "url": url,
        "start_time": time.time()
    }
    try:
        yield
    finally:
        if user_id in active_downloads:
            del active_downloads[user_id]

# ════════════════════════════════════════════════════════════════
# LIMPEZA AUTOMÁTICA DE ARQUIVOS TEMPORÁRIOS
# ════════════════════════════════════════════════════════════════

def cleanup_temp_files(max_age_minutes: int = 30):
    """Remove arquivos temporários antigos"""
    temp_dir = tempfile.gettempdir()
    now = time.time()
    max_age_seconds = max_age_minutes * 60
    removed_count = 0
    
    patterns = ["video_*", "audio_*", "*.jpg", "*.mp4", "*.webm", "*.m4a"]
    
    for pattern in patterns:
        for file_path in glob.glob(os.path.join(temp_dir, pattern)):
            try:
                if os.path.isfile(file_path):
                    file_age = now - os.path.getmtime(file_path)
                    if file_age > max_age_seconds:
                        os.remove(file_path)
                        removed_count += 1
            except Exception as e:
                LOG.debug("Erro ao remover %s: %s", file_path, e)
    
    if removed_count > 0:
        LOG.info("🧹 Limpeza: %d arquivos temporários removidos", removed_count)

def cleanup_and_gc_routine():
    """Rotina de limpeza automática e garbage collection"""
    while True:
        try:
            time.sleep(300)  # A cada 5 minutos
            
            # Limpeza de arquivos temporários
            cleanup_temp_files(max_age_minutes=30)
            
            # Garbage collection
            collected = gc.collect()
            if collected > 0:
                LOG.debug("🗑️ Garbage collector: %d objetos coletados", collected)
                
        except Exception as e:
            LOG.error("❌ Erro na rotina de limpeza: %s", e)

# ════════════════════════════════════════════════════════════════
# YT-DLP PROGRESS HOOK
# ════════════════════════════════════════════════════════════════

def yt_dlp_progress_hook(d):
    """Hook de progresso do yt-dlp - APENAS PARA LOGS CRÍTICOS"""
    if d['status'] == 'error':
        LOG.error("❌ Erro no download: %s", d.get('error', 'Desconhecido'))

# ════════════════════════════════════════════════════════════════
# DETECÇÃO E DOWNLOAD DE VÍDEOS
# ════════════════════════════════════════════════════════════════

def is_valid_video_url(url: str) -> bool:
    """Verifica se a URL é válida para download"""
    if not url or not url.startswith(('http://', 'https://')):
        return False
    
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    valid_domains = [
        'youtube.com', 'youtu.be', 'm.youtube.com',
        'instagram.com', 'www.instagram.com',
        'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com',
        'facebook.com', 'www.facebook.com', 'fb.watch', 'm.facebook.com',
        'twitter.com', 'x.com', 't.co',
        'shopee.com.br', 'shopee.com', 'shp.ee'
    ]
    
    return any(valid_domain in domain for valid_domain in valid_domains)

def detect_platform(url: str) -> str:
    """Detecta a plataforma do vídeo"""
    domain = urlparse(url).netloc.lower()
    
    if 'youtube' in domain or 'youtu.be' in domain:
        return 'YouTube'
    elif 'instagram' in domain:
        return 'Instagram'
    elif 'tiktok' in domain:
        return 'TikTok'
    elif 'facebook' in domain or 'fb.watch' in domain:
        return 'Facebook'
    elif 'twitter' in domain or 'x.com' in domain:
        return 'Twitter/X'
    elif 'shopee' in domain or 'shp.ee' in domain:
        return 'Shopee'
    
    return 'Desconhecida'

async def check_video_size(url: str) -> dict:
    """Verifica o tamanho do vídeo antes de baixar"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
    }
    
    try:
        loop = asyncio.get_event_loop()
        
        def get_info():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        
        info = await loop.run_in_executor(None, get_info)
        
        if info:
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            duration = info.get('duration', 0)
            title = info.get('title', 'Vídeo')
            
            return {
                'success': True,
                'size': filesize,
                'duration': duration,
                'title': title,
                'too_large': filesize > MAX_VIDEO_SIZE_BYTES if filesize else False
            }
    except Exception as e:
        LOG.debug("Erro ao verificar tamanho: %s", e)
    
    return {'success': False}

async def download_video(url: str, user_id: int) -> dict:
    """
    Baixa vídeo e retorna informações
    OTIMIZADO: Log único e conciso por download
    """
    platform = detect_platform(url)
    start_time = time.time()
    
    # Log inicial - APENAS UMA LINHA
    LOG.info("📥 Download iniciado | User: %d | Platform: %s | URL: %s", 
             user_id, platform, url[:60])
    
    temp_dir = tempfile.gettempdir()
    output_template = os.path.join(temp_dir, f"video_{user_id}_{uuid.uuid4().hex[:8]}.%(ext)s")
    
    # Configuração do yt-dlp SEM logs verbosos
    ydl_opts = {
        'format': 'best[filesize<100M]/best',
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'quiet': True,  # Sem logs do yt-dlp
        'no_warnings': True,
        'progress_hooks': [yt_dlp_progress_hook],
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    }
    
    # Configurações específicas por plataforma
    if 'shopee' in url.lower():
        ydl_opts.update({
            'format': 'best',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)',
                'Referer': 'https://shopee.com.br/'
            }
        })
    
    try:
        async with download_semaphore:
            with track_download(user_id, url):
                loop = asyncio.get_event_loop()
                
                def download():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        filename = ydl.prepare_filename(info)
                        return filename, info
                
                filename, info = await loop.run_in_executor(None, download)
                
                if not os.path.exists(filename):
                    raise FileNotFoundError("Arquivo não encontrado após download")
                
                file_size = os.path.getsize(filename)
                duration = time.time() - start_time
                
                # Log final - APENAS UMA LINHA
                LOG.info("✅ Download concluído | User: %d | Platform: %s | Size: %.1fMB | Time: %.1fs", 
                         user_id, platform, file_size / (1024*1024), duration)
                
                return {
                    'success': True,
                    'filepath': filename,
                    'title': info.get('title', 'Vídeo'),
                    'size': file_size,
                    'duration': info.get('duration', 0),
                    'platform': platform
                }
                
    except Exception as e:
        duration = time.time() - start_time
        # Log de erro - APENAS UMA LINHA
        LOG.error("❌ Download falhou | User: %d | Platform: %s | Error: %s | Time: %.1fs", 
                  user_id, platform, str(e)[:100], duration)
        return {
            'success': False,
            'error': str(e)
        }

# ════════════════════════════════════════════════════════════════
# TELEGRAM APPLICATION
# ════════════════════════════════════════════════════════════════

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# Cria o event loop global para o bot
APP_LOOP = asyncio.new_event_loop()

def run_async_in_thread(coro):
    """Executa coroutine no loop da thread do bot"""
    return asyncio.run_coroutine_threadsafe(coro, APP_LOOP)

# Inicializa a aplicação
application = Application.builder().token(TOKEN).build()

# ════════════════════════════════════════════════════════════════
# HANDLERS DO TELEGRAM
# ════════════════════════════════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler do comando /start"""
    user = update.effective_user
    create_or_update_user(user.id, user.username, user.first_name)
    
    welcome_message = (
        f"👋 Olá, {user.first_name}!\n\n"
        "🎬 **Video Downloader Pro**\n\n"
        "Envie o link de um vídeo das seguintes plataformas:\n"
        "• YouTube\n"
        "• Instagram\n"
        "• TikTok\n"
        "• Facebook\n"
        "• Twitter/X\n"
        "• Shopee\n\n"
        "📌 **Comandos disponíveis:**\n"
        "/start - Iniciar bot\n"
        "/help - Ajuda\n"
        "/status - Ver status\n"
        "/ai <mensagem> - Conversar com IA\n\n"
        "💎 /premium - Assinar plano premium"
    )
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler do comando /help"""
    help_text = (
        "📚 **Ajuda - Video Downloader Pro**\n\n"
        "**Como usar:**\n"
        "1. Copie o link do vídeo\n"
        "2. Cole aqui no chat\n"
        "3. Aguarde o download\n"
        "4. Receba o vídeo!\n\n"
        "**Plataformas suportadas:**\n"
        "• YouTube (vídeos até 100MB)\n"
        "• Instagram (posts e reels)\n"
        "• TikTok (vídeos)\n"
        "• Facebook (vídeos públicos)\n"
        "• Twitter/X (vídeos)\n"
        "• Shopee (vídeos de produtos)\n\n"
        "**Limites:**\n"
        f"• Tamanho máximo: {MAX_VIDEO_SIZE_MB}MB\n"
        f"• Downloads simultâneos: {MAX_CONCURRENT_DOWNLOADS}\n"
        f"• Requisições por minuto: {MAX_REQUESTS_PER_MINUTE}\n\n"
        "💡 **Dica:** Links mais curtos funcionam melhor!"
    )
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler do comando /status"""
    user_id = update.effective_user.id
    is_premium = is_premium_user(user_id)
    
    status_text = (
        "📊 **Seu Status**\n\n"
        f"👤 ID: `{user_id}`\n"
        f"💎 Premium: {'✅ Sim' if is_premium else '❌ Não'}\n\n"
    )
    
    if user_id in active_downloads:
        download_info = active_downloads[user_id]
        elapsed = int(time.time() - download_info['start_time'])
        status_text += (
            "📥 **Download Ativo:**\n"
            f"⏱️ Tempo: {elapsed}s\n"
            f"🔗 URL: `{download_info['url'][:50]}...`\n"
        )
    else:
        status_text += "✅ Nenhum download ativo\n"
    
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler do comando /premium"""
    user_id = update.effective_user.id
    
    if is_premium_user(user_id):
        await update.message.reply_text(
            "✅ Você já é um usuário **Premium**!\n\n"
            "Aproveite todos os benefícios! 🎉",
            parse_mode='Markdown'
        )
        return
    
    premium_text = (
        "💎 **Plano Premium**\n\n"
        "**Benefícios:**\n"
        "• Downloads ilimitados\n"
        "• Sem limites de tamanho\n"
        "• Prioridade na fila\n"
        "• Suporte prioritário\n\n"
        f"**Preço:** R$ {PREMIUM_PRICE:.2f}/mês\n\n"
        "Clique no botão abaixo para assinar:"
    )
    
    keyboard = [[InlineKeyboardButton("💳 Assinar com PIX", callback_data="subscribe_premium")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        premium_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler do comando /ai"""
    if not GROQ_AVAILABLE or not GROQ_API_KEY:
        await update.message.reply_text(
            "❌ IA não está disponível no momento.\n"
            "Configure GROQ_API_KEY para usar este recurso."
        )
        return
    
    user_message = ' '.join(context.args)
    
    if not user_message:
        await update.message.reply_text(
            "💬 **Como usar a IA:**\n\n"
            "Digite: `/ai sua mensagem aqui`\n\n"
            "**Exemplos:**\n"
            "• `/ai Explique inteligência artificial`\n"
            "• `/ai Como fazer bolo de chocolate?`\n"
            "• `/ai Qual a capital do Brasil?`",
            parse_mode='Markdown'
        )
        return
    
    try:
        processing_msg = await update.message.reply_text("🤔 Pensando...")
        
        client = Groq(api_key=GROQ_API_KEY)
        
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="llama3-8b-8192",
            messages=[
                {
                    "role": "system",
                    "content": "Você é um assistente útil e amigável. Responda de forma clara e concisa em português do Brasil."
                },
                {
                    "role": "user",
                    "content": user_message
                }
            ],
            max_tokens=500,
            temperature=0.7
        )
        
        ai_response = response.choices[0].message.content
        
        await processing_msg.edit_text(
            f"🤖 **Resposta da IA:**\n\n{ai_response}",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        LOG.error("Erro na IA: %s", e)
        await processing_msg.edit_text(
            f"❌ Erro ao processar sua mensagem.\n\nDetalhes: {str(e)[:100]}"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para mensagens de texto (URLs de vídeo)"""
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    
    # Verifica rate limiting
    if not rate_limiter.is_allowed(user_id):
        wait_time = rate_limiter.get_wait_time(user_id)
        await update.message.reply_text(
            f"⏳ **Limite de requisições atingido!**\n\n"
            f"Aguarde {wait_time} segundos antes de fazer um novo download.\n\n"
            f"💡 Dica: Assine o /premium para downloads ilimitados!",
            parse_mode='Markdown'
        )
        return
    
    # Verifica se é URL válida
    if not is_valid_video_url(message_text):
        await update.message.reply_text(
            "❌ **URL inválida!**\n\n"
            "Envie um link válido de:\n"
            "• YouTube\n"
            "• Instagram\n"
            "• TikTok\n"
            "• Facebook\n"
            "• Twitter/X\n"
            "• Shopee\n\n"
            "Digite /help para mais informações."
        )
        return
    
    platform = detect_platform(message_text)
    
    # Mensagem inicial
    processing_msg = await update.message.reply_text(
        f"🔍 **Processando vídeo...**\n\n"
        f"📱 Plataforma: {platform}\n"
        f"⏳ Aguarde...",
        parse_mode='Markdown'
    )
    
    try:
        # Verifica tamanho do vídeo
        size_info = await check_video_size(message_text)
        
        if size_info.get('success'):
            size_mb = size_info['size'] / (1024 * 1024) if size_info['size'] else 0
            
            if size_info.get('too_large'):
                await processing_msg.edit_text(
                    f"❌ **Vídeo muito grande!**\n\n"
                    f"📊 Tamanho: {size_mb:.1f}MB\n"
                    f"📏 Limite: {MAX_VIDEO_SIZE_MB}MB\n\n"
                    f"💎 Assine o /premium para downloads sem limite!",
                    parse_mode='Markdown'
                )
                return
            
            if size_mb > 50:
                await processing_msg.edit_text(
                    f"⚠️ **Vídeo grande detectado**\n\n"
                    f"📊 Tamanho: {size_mb:.1f}MB\n"
                    f"⏳ Isso pode demorar alguns minutos...\n\n"
                    f"🔄 Processando...",
                    parse_mode='Markdown'
                )
        
        # Faz o download
        result = await download_video(message_text, user_id)
        
        if result['success']:
            filepath = result['filepath']
            file_size_mb = result['size'] / (1024 * 1024)
            
            await processing_msg.edit_text(
                f"📤 **Enviando vídeo...**\n\n"
                f"📁 Tamanho: {file_size_mb:.1f}MB\n"
                f"⏳ Aguarde o upload...",
                parse_mode='Markdown'
            )
            
            # Envia o vídeo
            with open(filepath, 'rb') as video_file:
                await update.message.reply_video(
                    video=video_file,
                    caption=f"🎬 {result['title']}\n\n📱 {platform} | 📊 {file_size_mb:.1f}MB",
                    supports_streaming=True
                )
            
            await processing_msg.delete()
            
            # Remove arquivo temporário
            try:
                os.remove(filepath)
            except:
                pass
            
        else:
            error_msg = result.get('error', 'Erro desconhecido')
            await processing_msg.edit_text(
                f"❌ **Falha no download**\n\n"
                f"📱 Plataforma: {platform}\n"
                f"⚠️ Erro: {error_msg[:200]}\n\n"
                f"💡 Tente novamente ou entre em contato com o suporte.",
                parse_mode='Markdown'
            )
    
    except Exception as e:
        LOG.error("Erro ao processar mensagem: %s", e)
        await processing_msg.edit_text(
            f"❌ **Erro inesperado**\n\n"
            f"Detalhes: {str(e)[:200]}\n\n"
            f"Tente novamente em alguns instantes.",
            parse_mode='Markdown'
        )

# Registra os handlers
application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("status", status_command))
application.add_handler(CommandHandler("premium", premium_command))
application.add_handler(CommandHandler("ai", ai_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ════════════════════════════════════════════════════════════════
# FLASK WEB SERVER
# ════════════════════════════════════════════════════════════════

from flask import Flask, request, jsonify
from datetime import timezone

app = Flask(__name__)

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint de health check"""
    health_monitor.record_activity("flask")
    
    return jsonify({
        "status": "healthy",
        "bot": "Video Downloader Pro",
        "uptime": int(time.time() - LAST_ACTIVITY["telegram"]),
        "active_downloads": len(active_downloads)
    }), 200

@app.route(f'/{TOKEN}', methods=['POST'])
async def webhook():
    """Endpoint do webhook do Telegram"""
    try:
        health_monitor.record_activity("telegram")
        
        data = request.get_json()
        
        if data:
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
            return '', 200
        
        return '', 400
        
    except Exception as e:
        LOG.error("Erro no webhook: %s", e)
        health_monitor.record_error()
        return '', 500

@app.route('/mercadopago-webhook', methods=['POST'])
def mercadopago_webhook():
    """Webhook para notificações do Mercado Pago"""
    try:
        data = request.get_json()
        
        if not data:
            return '', 400
        
        # Processa notificação de pagamento
        if data.get('type') == 'payment':
            payment_id = data.get('data', {}).get('id')
            
            if payment_id and MERCADOPAGO_AVAILABLE and MERCADOPAGO_ACCESS_TOKEN:
                sdk = mercadopago.SDK(MERCADOPAGO_ACCESS_TOKEN)
                payment_info = sdk.payment().get(payment_id)
                
                if payment_info.get('status') == 200:
                    payment = payment_info.get('response', {})
                    reference = payment.get('external_reference')
                    status = payment.get('status')
                    
                    if reference and status == 'approved':
                        payment_data = get_payment_by_reference(reference)
                        
                        if payment_data and payment_data['status'] == 'pending':
                            user_id = payment_data['user_id']
                            update_payment_status(reference, 'approved')
                            set_premium_status(user_id, days=30)
                            
                            LOG.info("✅ Pagamento aprovado para user %d", user_id)
                            
                            # Envia mensagem ao usuário
                            async def notify_user():
                                await application.bot.send_message(
                                    chat_id=user_id,
                                    text=(
                                        "✅ **Pagamento aprovado!**\n\n"
                                        "🎉 Você agora é um usuário **Premium**!\n\n"
                                        "Aproveite todos os benefícios por 30 dias!\n\n"
                                        "Digite /status para ver seu status."
                                    ),
                                    parse_mode='Markdown'
                                )
                            
                            run_async_in_thread(notify_user())
        
        return '', 200
        
    except Exception as e:
        LOG.error("Erro no webhook do Mercado Pago: %s", e)
        return '', 500

@app.route('/render-webhook', methods=['POST'])
def render_webhook():
    """Webhook para eventos do Render (deploy, crashes, etc)"""
    try:
        data = request.get_json()
        
        if not data:
            return {"error": "No data provided"}, 400

        # === 🔹 Extrai informações do evento ===
        event_type = data.get("type")
        service_name = data.get("service", {}).get("name", "Desconhecido")
        status = data.get("status", "unknown")
        timestamp_utc = data.get("timestamp")

        # === 🔹 Filtra eventos relevantes ===
        eventos_relevantes = [
            "deploy_started",
            "deploy_ended",
            "service_unhealthy",
            "server_unhealthy",
            "service_started",
            "server_started"
        ]
        
        if event_type not in eventos_relevantes:
            # Retorna OK sem processar
            return {"message": f"Evento ignorado: {event_type}"}, 200

        # === 🔹 Converte UTC → Horário de Brasília ===
        if timestamp_utc:
            try:
                dt_utc = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
                brasil_tz = timezone(timedelta(hours=-3))
                dt_brasil = dt_utc.astimezone(brasil_tz)
                timestamp = dt_brasil.strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                timestamp = timestamp_utc
        else:
            timestamp = "Hora não informada"

        # === 🔹 Define mensagem conforme o tipo de evento ===
        if event_type == "deploy_started":
            event_emoji = "🚀"
            status_text = "Deploy iniciado"
            status_emoji = "🔄"
        elif event_type == "deploy_ended":
            event_emoji = "🚀"
            status_text = "Deploy finalizado"
            if status == "succeeded":
                status_emoji = "✅"
            elif status == "failed":
                status_emoji = "❌"
            else:
                status_emoji = "⚠️"
        elif event_type in ["service_unhealthy", "server_unhealthy"]:
            event_emoji = "🔴"
            status_text = "Serviço ficou instável ou caiu"
            status_emoji = "🔴"
        elif event_type in ["service_started", "server_started"]:
            event_emoji = "🔄"
            status_text = "Serviço reiniciado"
            status_emoji = "🔄"
        else:
            event_emoji = "⚠️"
            status_text = f"Evento: {event_type}"
            status_emoji = "⚠️"

        # === 🔹 Monta mensagem para Discord ===
        message = (
            f"{event_emoji} **Render Alert**\n"
            f"📌 **Evento:** {event_type}\n"
            f"🖥️ **Serviço:** {service_name}\n"
            f"{status_emoji} **{status_text}**\n"
            f"⏰ **Hora (Brasília):** {timestamp}\n"
            f"🔗 https://dashboard.render.com"
        )

        if not DISCORD_WEBHOOK_URL:
            return {"error": "Webhook do Discord não configurado"}, 200  # Retorna 200 para não causar erro

        # === 🔹 Envia mensagem pro Discord em background (não bloqueia) ===
        try:
            # Timeout curto para não travar o webhook
            response = requests.post(
                DISCORD_WEBHOOK_URL, 
                json={"content": message},
                timeout=3  # 3 segundos máximo
            )
            if response.status_code == 204:
                LOG.info("✅ Alerta de Render enviado para Discord: %s", event_type)
            else:
                LOG.warning("⚠️ Discord retornou status %d", response.status_code)
        except requests.Timeout:
            LOG.warning("⚠️ Timeout ao enviar para Discord")
        except Exception as e:
            LOG.error("❌ Erro ao enviar para Discord: %s", e)
        
        # Sempre retorna 200 OK para o Render
        return {"status": "received", "event": event_type}, 200
    
    except Exception as e:
        # Log do erro mas retorna 200 para não causar erros no Render
        LOG.error("❌ Erro no render-webhook: %s", e)
        return {"status": "error", "message": str(e)}, 200

# ============================
# CALLBACKS DE PAGAMENTO (ANTES DO APP.RUN)
# ============================

from telegram.constants import ParseMode

async def subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para callback de assinatura premium"""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    try:
        if not MERCADOPAGO_AVAILABLE or not MERCADOPAGO_ACCESS_TOKEN:
            await query.edit_message_text("❌ Sistema de pagamentos não configurado.")
            return
            
        reference = create_pix_payment(user_id, PREMIUM_PRICE)
        sdk = mercadopago.SDK(MERCADOPAGO_ACCESS_TOKEN)

        payment_data = {
            "transaction_amount": PREMIUM_PRICE,
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
                    f"✅ Pedido criado!\n\n"
                    f"<code>{qr_code_text}</code>\n\n"
                    "🖼️ Escaneie o QR Code abaixo para pagar:"
                ),
                parse_mode=ParseMode.HTML
            )

            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=f"data:image/png;base64,{qr_code_base64}"
            )
        else:
            await query.edit_message_text("❌ Erro ao criar pagamento. Tente novamente mais tarde.")
    except Exception as e:
        LOG.error("Erro no subscribe_callback: %s", e)
        await query.edit_message_text(f"❌ Falha interna: {e}")

# Registra callback handler
application.add_handler(CallbackQueryHandler(subscribe_callback, pattern="^subscribe_premium$"))

# ============================
# MAIN
# ============================

if __name__ == "__main__":
    # Inicia thread de limpeza automática e garbage collection
    cleanup_thread = threading.Thread(target=cleanup_and_gc_routine, daemon=True)
    cleanup_thread.start()
    LOG.info("✅ Thread de limpeza automática e GC iniciada")
    
    # 🔄 Inicia sistema de auto-recuperação e keepalive
    if KEEPALIVE_ENABLED:
        keepalive_thread = threading.Thread(target=keepalive_routine, daemon=True)
        keepalive_thread.start()
        LOG.info("✅ Thread de keepalive iniciada (intervalo: %d segundos)", KEEPALIVE_INTERVAL)
        
        watchdog_thread = threading.Thread(target=webhook_watchdog, daemon=True)
        watchdog_thread.start()
        LOG.info("✅ Thread de watchdog iniciada")
    else:
        LOG.warning("⚠️ Sistema de keepalive desabilitado")
    
    # Configura webhook se URL estiver definida
    if WEBHOOK_URL:
        try:
            webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
            LOG.info("🔗 Configurando webhook: %s", webhook_url)
            
            # CORREÇÃO: Remove webhook antigo PRIMEIRO para evitar erros 502
            LOG.info("🗑️ Removendo webhook antigo...")
            delete_future = asyncio.run_coroutine_threadsafe(
                application.bot.delete_webhook(drop_pending_updates=True),
                APP_LOOP
            )
            delete_future.result(timeout=10)
            LOG.info("✅ Webhook antigo removido")
            
            # Aguarda um pouco para Telegram processar
            time.sleep(2)
            
            # Agora configura o novo webhook
            LOG.info("🔗 Configurando novo webhook...")
            set_future = asyncio.run_coroutine_threadsafe(
                application.bot.set_webhook(
                    url=webhook_url,
                    drop_pending_updates=False,
                    max_connections=100,
                    allowed_updates=["message", "callback_query"]
                ),
                APP_LOOP
            )
            result = set_future.result(timeout=10)
            
            if result:
                LOG.info("✅ Webhook configurado com sucesso!")
                
                # Verifica webhook
                info_future = asyncio.run_coroutine_threadsafe(
                    application.bot.get_webhook_info(),
                    APP_LOOP
                )
                webhook_info = info_future.result(timeout=10)
                LOG.info("📊 Webhook Info: URL=%s, Pending=%d", 
                        webhook_info.url, 
                        webhook_info.pending_update_count)
            else:
                LOG.error("❌ Falha ao configurar webhook")
            
        except Exception as e:
            LOG.error("❌ Erro ao configurar webhook: %s", e)
    else:
        LOG.warning("⚠️ WEBHOOK_URL não definida - bot não receberá updates!")
    
    port = int(os.environ.get("PORT", 10000))
    LOG.info("🚀 Iniciando servidor Flask na porta %d", port)
    LOG.info("🤖 Bot: @%s", application.bot.username if hasattr(application.bot, 'username') else 'desconhecido')
    app.run(host="0.0.0.0", port=port)
