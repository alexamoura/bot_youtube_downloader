#!/usr/bin/env python3
"""
bot_with_cookies_melhorado.py - Versão Profissional

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

# Configuração de Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ytbot")

# Token do Bot
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido.")
    sys.exit(1)

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# Constantes do Sistema
URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = "users.db"
PENDING_MAX_SIZE = 1000
PENDING_EXPIRE_SECONDS = 600
WATCHDOG_TIMEOUT = 180
MAX_FILE_SIZE = 50 * 1024 * 1024
SPLIT_SIZE = 45 * 1024 * 1024

# Constantes de Controle de Downloads
FREE_DOWNLOADS_LIMIT = 10

# Estado Global
PENDING = OrderedDict()
DB_LOCK = threading.Lock()

# Mensagens Profissionais do Bot
MESSAGES = {
    "welcome": (
        "🎥 <b>Bem-vindo ao Serviço de Downloads</b>\n\n"
        "Envie um link de vídeo de YouTube, Instagram ou Shopee e eu processarei o download para você.\n\n"
        "📊 <b>Planos disponíveis:</b>\n"
        "• Gratuito: {free_limit} downloads\n"
        "• Premium: Downloads ilimitados\n\n"
        "Digite /status para verificar seu saldo de downloads."
    ),
    "url_prompt": "📎 Por favor, envie o link do vídeo que deseja baixar.",
    "processing": "⚙️ Processando sua solicitação...",
    "invalid_url": "⚠️ O link fornecido não é válido. Por favor, verifique e tente novamente.",
    "confirm_download": "🎬 <b>Confirmar Download</b>\n\n📹 Vídeo: {title}\n⏱️ Duração: {duration}\n\n✅ Deseja prosseguir com o download?",
    "download_started": "📥 Download iniciado. Aguarde enquanto processamos seu vídeo...",
    "download_progress": "📥 Progresso: {percent}%\n{bar}",
    "download_complete": "✅ Download concluído. Enviando arquivo...",
    "upload_complete": "✅ Vídeo enviado com sucesso!\n\n📊 Downloads restantes: {remaining}/{total}",
    "limit_reached": (
        "⚠️ <b>Limite de Downloads Atingido</b>\n\n"
        "Você atingiu o limite de {limit} downloads gratuitos.\n\n"
        "💎 <b>Adquira o Plano Premium para downloads ilimitados!</b>\n\n"
        "💳 Valor: R$ 9,90/mês\n"
        "🔄 Pagamento via PIX\n\n"
        "Entre em contato para mais informações: /premium"
    ),
    "status": (
        "📊 <b>Status da Sua Conta</b>\n\n"
        "👤 ID: {user_id}\n"
        "📥 Downloads realizados: {used}/{total}\n"
        "💾 Downloads restantes: {remaining}\n"
        "📅 Período: Mensal\n\n"
        "{premium_info}"
    ),
    "premium_info": (
        "💎 <b>Informações sobre o Plano Premium</b>\n\n"
        "✨ <b>Benefícios:</b>\n"
        "• Downloads ilimitados\n"
        "• Qualidade máxima (até 1080p)\n"
        "• Processamento prioritário\n"
        "• Suporte dedicado\n\n"
        "💰 <b>Valor:</b> R$ 9,90/mês\n\n"
        "📱 <b>Como contratar:</b>\n"
        "1. Faça o pagamento via PIX\n"
        "2. Envie o comprovante para confirmação\n"
        "3. Seu acesso será liberado em até 5 minutos\n\n"
        "🔑 Chave PIX: [A SER CONFIGURADA]\n\n"
        "⚠️ <b>Importante:</b> Após o pagamento, envie uma mensagem com o texto 'COMPROVANTE' junto com a imagem do comprovante."
    ),
    "stats": "📈 <b>Estatísticas do Bot</b>\n\n👥 Usuários ativos este mês: {count}",
    "error_timeout": "⏱️ O tempo de processamento excedeu o limite. Por favor, tente novamente.",
    "error_network": "🌐 Erro de conexão detectado. Verifique sua internet e tente novamente em alguns instantes.",
    "error_file_large": "📦 O arquivo excede o tamanho máximo permitido para processamento.",
    "error_ffmpeg": "🎬 Ocorreu um erro durante o processamento do vídeo.",
    "error_upload": "📤 Falha ao enviar o arquivo. Por favor, tente novamente.",
    "error_unknown": "❌ Um erro inesperado ocorreu. Nossa equipe foi notificada. Por favor, tente novamente.",
    "error_expired": "⏰ Esta solicitação expirou. Por favor, envie o link novamente.",
    "download_cancelled": "🚫 Download cancelado com sucesso.",
}

app = Flask(__name__)

# Inicialização do Telegram Application
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
    """Inicializa o banco de dados com as tabelas necessárias"""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            
            # Tabela de usuários mensais
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
            
            # Tabela de histórico de pagamentos PIX (para implementação futura)
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
    """Atualiza o registro de acesso mensal do usuário"""
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
            LOG.error("Erro ao atualizar usuário: %s", e)

def get_user_download_stats(user_id: int) -> dict:
    """Retorna estatísticas de downloads do usuário"""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            
            # Busca ou cria registro do usuário
            c.execute("SELECT downloads_count, is_premium, last_reset FROM user_downloads WHERE user_id=?", (user_id,))
            row = c.fetchone()
            
            current_month = time.strftime("%Y-%m")
            
            if row:
                downloads_count, is_premium, last_reset = row
                
                # Reseta contador se mudou o mês
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
            LOG.error("Erro ao obter estatísticas de download: %s", e)
            return {"downloads_count": 0, "is_premium": False, "remaining": FREE_DOWNLOADS_LIMIT, "limit": FREE_DOWNLOADS_LIMIT}

def can_download(user_id: int) -> bool:
    """Verifica se o usuário pode realizar um download"""
    stats = get_user_download_stats(user_id)
    
    if stats["is_premium"]:
        return True
    
    return stats["downloads_count"] < FREE_DOWNLOADS_LIMIT

def increment_download_count(user_id: int):
    """Incrementa o contador de downloads do usuário"""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("UPDATE user_downloads SET downloads_count = downloads_count + 1 WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
            LOG.info("Contador de downloads incrementado para usuário %d", user_id)
        except sqlite3.Error as e:
            LOG.error("Erro ao incrementar contador de downloads: %s", e)

def get_monthly_users_count() -> int:
    """Retorna o número de usuários ativos no mês atual"""
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
# PIX PAYMENT SYSTEM (Estrutura para implementação futura)
# ============================

def create_pix_payment(user_id: int, amount: float) -> str:
    """
    Cria um registro de pagamento PIX pendente
    
    TODO: Implementar integração com gateway de pagamento
    - Gerar QR Code PIX
    - Criar chave PIX única por transação
    - Retornar dados para exibição ao usuário
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
    
    TODO: Implementar verificação automática de pagamento
    - Webhook do gateway de pagamento
    - Validação do comprovante
    - Ativação automática do premium
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
            
            # Ativa premium para o usuário
            premium_expires = time.strftime("%Y-%m-%d", time.localtime(time.time() + 30*24*60*60))  # +30 dias
            c.execute("""
                UPDATE user_downloads 
                SET is_premium=1, premium_expires=? 
                WHERE user_id=?
            """, (premium_expires, user_id))
            
            conn.commit()
            conn.close()
            
            LOG.info("Pagamento PIX confirmado para usuário %d", user_id)
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
    """Prepara arquivo de cookies a partir de variável de ambiente Base64"""
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("Variável %s não encontrada.", env_var)
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
    """Valida se a string é uma URL válida"""
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
    
    LOG.info("Nenhum cookie disponível")
    return None

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
    """Formata duração em segundos para formato legível"""
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

def split_video_file(input_path: str, output_dir: str, segment_size: int = SPLIT_SIZE) -> list:
    """Divide arquivo de vídeo em partes menores"""
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
    LOG.info("Comando /start executado por usuário %d", user_id)

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
    
    premium_info = "✅ Plano: <b>Premium Ativo</b>" if stats["is_premium"] else "📦 Plano: <b>Gratuito</b>"
    
    status_text = MESSAGES["status"].format(
        user_id=user_id,
        used=stats["downloads_count"],
        total=stats["limit"] if not stats["is_premium"] else "∞",
        remaining=stats["remaining"],
        premium_info=premium_info
    )
    
    await update.message.reply_text(status_text, parse_mode="HTML")
    LOG.info("Comando /status executado por usuário %d", user_id)

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para o comando /premium - informações sobre plano premium"""
    user_id = update.effective_user.id
    
    keyboard = [[
        InlineKeyboardButton("💳 Assinar Premium", callback_data=f"subscribe:{user_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        MESSAGES["premium_info"],
        parse_mode="HTML",
        reply_markup=reply_markup
    )
    LOG.info("Comando /premium executado por usuário %d", user_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para mensagens de texto (URLs)"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    update_user(user_id)
    
    # Verifica se é um link válido
    urls = URL_RE.findall(text)
    if not urls:
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
        LOG.info("Usuário %d atingiu limite de downloads", user_id)
        return
    
    # Cria token único para esta requisição
    token = str(uuid.uuid4())
    
    # Resolve links universais da Shopee
    if 'shopee' in url.lower() and 'universal-link' in url:
        url = resolve_shopee_universal_link(url)
    
    # Envia mensagem de processamento
    processing_msg = await update.message.reply_text(MESSAGES["processing"])
    
    # Obtém informações do vídeo
    try:
        video_info = await get_video_info(url)
        
        if not video_info:
            await processing_msg.edit_text(MESSAGES["invalid_url"])
            return
        
        title = video_info.get("title", "Vídeo")[:100]
        duration = format_duration(video_info.get("duration", 0))
        
        # Cria botões de confirmação
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirmar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        confirm_text = MESSAGES["confirm_download"].format(
            title=title,
            duration=duration
        )
        
        await processing_msg.edit_text(
            confirm_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        
        # Armazena informações pendentes
        PENDING[token] = {
            "url": url,
            "user_id": user_id,
            "chat_id": update.effective_chat.id,
            "message_id": processing_msg.message_id,
            "timestamp": time.time(),
        }
        
        # Remove requisições antigas
        _cleanup_pending()
        
    except Exception as e:
        LOG.exception("Erro ao obter informações do vídeo: %s", e)
        await processing_msg.edit_text(MESSAGES["error_unknown"])

async def get_video_info(url: str) -> dict:
    """Obtém informações básicas do vídeo sem fazer download"""
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
        LOG.error("Erro ao extrair informações: %s", e)
        return None

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para callbacks de confirmação de download"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    action, token = data.split(":", 1)
    
    if token not in PENDING:
        await query.edit_message_text(MESSAGES["error_expired"])
        return
    
    pm = PENDING[token]
    
    # Verifica se o usuário é o mesmo que solicitou
    if pm["user_id"] != query.from_user.id:
        await query.answer("⚠️ Esta ação não pode ser realizada por você.", show_alert=True)
        return
    
    if action == "cancel":
        del PENDING[token]
        await query.edit_message_text(MESSAGES["download_cancelled"])
        LOG.info("Download cancelado pelo usuário %d", pm["user_id"])
        return
    
    if action == "dl":
        # Inicia o download
        del PENDING[token]
        await query.edit_message_text(MESSAGES["download_started"])
        
        # Incrementa contador de downloads
        increment_download_count(pm["user_id"])
        
        # Inicia download em background
        asyncio.create_task(_process_download(token, pm))
        LOG.info("Download iniciado para usuário %d", pm["user_id"])

async def _process_download(token: str, pm: dict):
    """Processa o download em background"""
    try:
        tmpdir = tempfile.mkdtemp(prefix=f"dl_{token}_")
        
        try:
            await _do_download(token, pm["url"], tmpdir, pm["chat_id"], pm)
        finally:
            # Limpa arquivos temporários
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception as e:
                LOG.error("Erro ao limpar tmpdir: %s", e)
                
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

async def _do_download(token: str, url: str, tmpdir: str, chat_id: int, pm: dict):
    """Executa o download do vídeo"""
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
    last_percent = -1
    
    def progress_hook(d):
        nonlocal last_percent
        try:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    percent = int(downloaded * 100 / total)
                    if percent != last_percent and percent % 10 == 0:
                        last_percent = percent
                        blocks = int(percent / 5)
                        bar = "█" * blocks + "░" * (20 - blocks)
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

    # Configurações do yt-dlp
    ydl_opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "quiet": False,
        "logger": LOG,
        "format": "best[height<=720]+bestaudio/best[height<=720]",  # Limite de 720p
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
            
            if tamanho > MAX_FILE_SIZE:
                # Divide arquivo grande
                partes_dir = os.path.join(tmpdir, "partes")
                try:
                    partes = split_video_file(path, partes_dir)
                    LOG.info("Arquivo dividido em %d partes", len(partes))
                    
                    for idx, ppath in enumerate(partes, 1):
                        with open(ppath, "rb") as fh:
                            await application.bot.send_video(
                                chat_id=chat_id, 
                                video=fh,
                                caption=f"📦 Parte {idx}/{len(partes)}"
                            )
                except Exception as e:
                    LOG.exception("Erro ao dividir/enviar arquivo: %s", e)
                    await _notify_error(pm, "error_ffmpeg")
                    return
            else:
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
            total=stats["limit"] if not stats["is_premium"] else "∞"
        )
        
        await application.bot.edit_message_text(
            text=success_text,
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.error("Erro ao enviar mensagem final: %s", e)

def _run_ydl(options, urls):
    """Executa yt-dlp com as opções fornecidas"""
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

async def _notify_error(pm: dict, error_key: str):
    """Notifica o usuário sobre um erro"""
    try:
        await application.bot.edit_message_text(
            text=MESSAGES.get(error_key, MESSAGES["error_unknown"]),
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.error("Erro ao notificar erro: %s", e)

def _cleanup_pending():
    """Remove requisições pendentes expiradas"""
    now = time.time()
    expired = [
        token for token, pm in PENDING.items()
        if now - pm["timestamp"] > PENDING_EXPIRE_SECONDS
    ]
    for token in expired:
        del PENDING[token]
    
    # Limita tamanho máximo
    while len(PENDING) > PENDING_MAX_SIZE:
        PENDING.popitem(last=False)

# ============================
# REGISTRO DE HANDLERS
# ============================

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CommandHandler("status", status_cmd))
application.add_handler(CommandHandler("premium", premium_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:|subscribe:)"))
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
    return "🤖 Bot de Download Ativo"

@app.route("/health")
def health():
    """Endpoint de health check"""
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

# ============================
# MAIN
# ============================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    LOG.info("Iniciando servidor Flask na porta %d", port)
    app.run(host="0.0.0.0", port=port)
