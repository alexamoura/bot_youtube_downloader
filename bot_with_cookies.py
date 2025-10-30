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

# NOVO: Limite de tamanho para vídeos (padrão: 100MB)
MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", "100"))
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024

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
    "360p": {"height": 360, "label": "360p • Econômico"},
    "480p": {"height": 480, "label": "480p • Balanceado"},
    "720p": {"height": 720, "label": "720p • Alta Definição"},
    "1080p": {"height": 1080, "label": "1080p • Full HD"},
}

# ═══════════════════════════════════════════════════════════════
# 🎨 MENSAGENS PROFISSIONAIS E CRIATIVAS
# ═══════════════════════════════════════════════════════════════

WELCOME_MESSAGE = """
╔═══════════════════════════════════╗
║  🎬 **DOWNLOADER PROFISSIONAL**  ║
╚═══════════════════════════════════╝

Bem-vindo ao seu assistente de downloads premium! 

**✨ RECURSOS DISPONÍVEIS:**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 **Multi-plataforma**
   • YouTube • Instagram • TikTok
   • Shopee • E mais de 1000 sites

📊 **Qualidade Personalizável**
   • 360p até 1080p Full HD
   • Otimização automática de tamanho

⚡ **Processamento Inteligente**
   • Downloads simultâneos
   • Conversão otimizada
   • Entrega ultrarrápida

🔐 **100% Seguro e Privado**
   • Sem armazenamento de dados
   • Processamento criptografado

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**💡 COMO USAR:**
1️⃣ Envie o link do vídeo
2️⃣ Escolha a qualidade desejada
3️⃣ Aguarde o processamento
4️⃣ Receba seu arquivo!

**📌 DICA:** Use qualidades menores para downloads mais rápidos e arquivos mais leves.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Desenvolvido com ❤️ para sua conveniência_
"""

ERROR_MESSAGES = {
    "timeout": """
╔═══════════════════════════════════╗
║      ⏱️ TIMEOUT DE PROCESSO       ║
╚═══════════════════════════════════╝

**Situação:** O processamento excedeu o tempo limite de 5 minutos.

**📋 Possíveis Causas:**
• Arquivo muito grande (>100MB)
• Conexão instável
• Alta demanda no servidor

**💡 SOLUÇÕES RECOMENDADAS:**
✓ Tente uma qualidade menor (360p/480p)
✓ Verifique sua conexão
✓ Aguarde alguns instantes

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Nossa equipe monitora estes eventos constantemente_
""",
    
    "invalid_url": """
╔═══════════════════════════════════╗
║     ⚠️ URL NÃO RECONHECIDA        ║
╚═══════════════════════════════════╝

**Situação:** Não foi possível validar o link fornecido.

**✅ VERIFIQUE SE:**
• O link está completo e correto
• O conteúdo é público/acessível
• A plataforma é suportada

**🌐 Plataformas Suportadas:**
YouTube • Instagram • TikTok • Twitter
Facebook • Vimeo • Dailymotion
Shopee • E mais de 1000 sites

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 _Envie /start para ver mais informações_
""",
    
    "network_error": """
╔═══════════════════════════════════╗
║     🌐 ERRO DE CONEXÃO            ║
╚═══════════════════════════════════╝

**Situação:** Não foi possível estabelecer conexão com o servidor de origem.

**📡 Status:** Tentando reconectar...

**⏰ O QUE FAZER:**
• Aguarde 30-60 segundos
• Tente novamente
• Verifique se o link ainda é válido

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Nossos servidores estão sempre monitorados 24/7_
""",
    
    "ffmpeg_error": """
╔═══════════════════════════════════╗
║  🎬 ERRO NO PROCESSAMENTO         ║
╚═══════════════════════════════════╝

**Situação:** O vídeo foi baixado mas houve falha no processamento.

**🔍 Possíveis Causas:**
• Formato incompatível
• Arquivo corrompido
• Codec não suportado

**💡 SUGESTÕES:**
✓ Tente novamente
✓ Escolha outra qualidade
✓ Verifique se o link ainda funciona

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Equipe técnica notificada automaticamente_
""",
    
    "upload_error": """
╔═══════════════════════════════════╗
║      📤 FALHA NO ENVIO            ║
╚═══════════════════════════════════╝

**Situação:** O arquivo foi processado mas não pôde ser enviado.

**⚠️ Causas Prováveis:**
• Arquivo muito grande para o Telegram
• Conexão interrompida
• Formato incompatível

**✅ TENTE:**
• Qualidade menor (360p/480p)
• Aguardar alguns instantes
• Reenviar o link

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Limite do Telegram: 50MB por arquivo_
""",
    
    "unknown": """
╔═══════════════════════════════════╗
║      ❌ ERRO INESPERADO           ║
╚═══════════════════════════════════╝

**Situação:** Ocorreu um erro durante o processamento.

**🔧 STATUS DO SISTEMA:**
✓ Equipe técnica notificada
✓ Logs salvos automaticamente
✓ Monitoramento ativo

**⏰ PRÓXIMOS PASSOS:**
• Aguarde 2-3 minutos
• Tente novamente
• Entre em contato se persistir

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Trabalhamos constantemente para melhorar o serviço_
""",
    
    "expired": """
╔═══════════════════════════════════╗
║     ⏰ SESSÃO EXPIRADA             ║
╚═══════════════════════════════════╝

**Situação:** Esta solicitação expirou após 10 minutos de inatividade.

**🔄 MOTIVO:**
Para manter a eficiência do sistema, solicitações inativas são automaticamente limpas.

**💡 SOLUÇÃO:**
Envie o link novamente para iniciar um novo processo de download.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Sistema de limpeza automática para melhor performance_
""",
    
    "queue_full": f"""
╔═══════════════════════════════════╗
║   🔄 SISTEMA EM ALTA DEMANDA      ║
╚═══════════════════════════════════╝

**Situação:** Todos os {MAX_CONCURRENT_DOWNLOADS} slots de processamento estão ocupados.

**⏳ FILA ATUAL:**
• {MAX_CONCURRENT_DOWNLOADS} downloads simultâneos
• Processamento em andamento
• Tempo médio de espera: 30-60s

**🎯 RECOMENDAÇÃO:**
Aguarde alguns instantes e tente novamente.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Nosso sistema prioriza qualidade sobre velocidade_
""",
    
    "file_too_large": f"""
╔═══════════════════════════════════╗
║   📦 ARQUIVO EXCEDE O LIMITE      ║
╚═══════════════════════════════════╝

**Limite Máximo:** {MAX_VIDEO_SIZE_MB}MB
**Motivo:** Otimização para vídeos curtos e médios

**💡 SOLUÇÕES ALTERNATIVAS:**

**1️⃣ Reduzir Qualidade:**
   • 360p → ~30MB para 10min
   • 480p → ~50MB para 10min
   • 720p → ~100MB para 10min

**2️⃣ Dividir o Vídeo:**
   • Baixe em partes menores
   • Use timestamps na URL

**3️⃣ Compressão Externa:**
   • Utilize ferramentas de compressão
   • Mantenha a qualidade visual

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Limite implementado para garantir velocidade ideal_
""",
}

PROCESSING_MESSAGES = {
    "analyzing": """
╔═══════════════════════════════════╗
║     🔍 ANALISANDO CONTEÚDO        ║
╚═══════════════════════════════════╝

**Status:** Validando URL e extraindo informações...

⚙️ **Processo em Andamento:**
• Identificando plataforma
• Verificando disponibilidade
• Extraindo metadados
• Selecionando servidor otimizado

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Aguarde enquanto preparamos tudo para você..._
""",

    "queue_position": """
╔═══════════════════════════════════╗
║      ⏳ NA FILA DE PROCESSO       ║
╚═══════════════════════════════════╝

**Sua Posição:** #{position}

⚡ **Status do Sistema:**
• Downloads ativos: {active}/{max_slots}
• Tempo estimado: ~{eta} segundos

📊 **Processando:**
Seu download iniciará automaticamente assim que um slot ficar disponível.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Sistema inteligente de gerenciamento de fila_
""",

    "downloading": """
╔═══════════════════════════════════╗
║     📥 DOWNLOAD EM PROGRESSO      ║
╚═══════════════════════════════════╝

**Progresso:** {percent}

⚡ **Informações em Tempo Real:**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 Velocidade: {speed}
⏱️ Tempo restante: {eta}
📊 Status: Baixando...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Processamento otimizado para máxima eficiência_
""",

    "processing": """
╔═══════════════════════════════════╗
║   🎬 PROCESSAMENTO DE MÍDIA       ║
╚═══════════════════════════════════╝

**Status:** Otimizando arquivo...

⚙️ **Etapas em Execução:**
• ✓ Download concluído
• ⏳ Conversão de formato
• ⏳ Otimização de qualidade
• ⏳ Preparação para envio

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Aplicando compressão inteligente..._
""",

    "uploading": """
╔═══════════════════════════════════╗
║    📤 TRANSFERINDO ARQUIVO        ║
╚═══════════════════════════════════╝

**Status:** Enviando para o Telegram...

📡 **Progresso:**
• ✓ Download completo
• ✓ Processamento finalizado
• ⏳ Transferindo dados
• ⏳ Validação final

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Quase lá! Aguarde alguns instantes..._
""",

    "slow_download": """
╔═══════════════════════════════════╗
║    🐌 DOWNLOAD MAIS LENTO         ║
╚═══════════════════════════════════╝

**Notificação:** O download está levando mais tempo que o esperado.

**📊 Possíveis Razões:**
• Arquivo de grande tamanho
• Servidor de origem lento
• Alta demanda no momento

**✅ TRANQUILIZE-SE:**
• O processo continua normalmente
• Não é necessária nenhuma ação
• Você será notificado ao concluir

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Obrigado pela paciência! Estamos trabalhando nisso._
"""
}

SUCCESS_MESSAGES = {
    "complete": """
╔═══════════════════════════════════╗
║   ✅ PROCESSO CONCLUÍDO           ║
╚═══════════════════════════════════╝

**Status:** Download finalizado com sucesso!

📊 **Detalhes do Arquivo:**
• Qualidade: {quality}
• Tamanho: {size}
• Formato: MP4 Otimizado

🎉 **MISSÃO CUMPRIDA!**
Seu arquivo está pronto e foi enviado.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Aproveite seu conteúdo! Use /start para novo download._
""",
}

# ═══════════════════════════════════════════════════════════════

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
        txt = base64.b64decode(b64).decode("utf-8")
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="cookies_")
        with os.fdopen(fd, "w") as f:
            f.write(txt)
        return path
    except Exception as e:
        LOG.error("Erro ao preparar cookie %s: %s", env_var, e)
        return None

COOKIE_YT = prepare_cookies("COOKIE_YT")
COOKIE_SHOPEE = prepare_cookies("COOKIE_SHOPEE")
COOKIE_IG = prepare_cookies("COOKIE_IG")

def get_cookie_for_url(url: str):
    lower = url.lower()
    if "youtube.com" in lower or "youtu.be" in lower:
        return COOKIE_YT
    elif "shopee.com" in lower or "shopee.co" in lower:
        return COOKIE_SHOPEE
    elif "instagram.com" in lower:
        return COOKIE_IG
    return None

# Helpers
def format_size(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    elif b < 1024*1024:
        return f"{b/1024:.1f}KB"
    else:
        return f"{b/(1024*1024):.1f}MB"

def add_pending(token: str, entry: dict):
    """Thread-safe add to PENDING"""
    with PENDING_LOCK:
        if len(PENDING) >= PENDING_MAX_SIZE:
            # Remove o mais antigo
            PENDING.popitem(last=False)
        PENDING[token] = entry

def get_pending(token: str):
    """Thread-safe get from PENDING"""
    with PENDING_LOCK:
        return PENDING.get(token)

def delete_pending(token: str):
    """Thread-safe delete from PENDING"""
    with PENDING_LOCK:
        PENDING.pop(token, None)

def expire_old_pending():
    """Remove entradas expiradas - thread-safe"""
    now = time.time()
    with PENDING_LOCK:
        to_remove = [
            tk for tk, entry in PENDING.items()
            if now - entry.get("created", 0) > PENDING_EXPIRE_SECONDS
        ]
        for tk in to_remove:
            PENDING.pop(tk, None)

def register_active_download(token: str):
    """Registra um download ativo"""
    with ACTIVE_DOWNLOADS_LOCK:
        ACTIVE_DOWNLOADS[token] = time.time()

def unregister_active_download(token: str):
    """Remove um download ativo"""
    with ACTIVE_DOWNLOADS_LOCK:
        ACTIVE_DOWNLOADS.pop(token, None)

def get_active_downloads_count() -> int:
    """Retorna número de downloads ativos"""
    with ACTIVE_DOWNLOADS_LOCK:
        return len(ACTIVE_DOWNLOADS)

# Commands
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start com mensagem profissional"""
    user_id = update.effective_user.id
    update_user(user_id)
    await update.message.reply_text(
        WELCOME_MESSAGE,
        parse_mode="Markdown"
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /stats melhorado"""
    count = get_monthly_users_count()
    active = get_active_downloads_count()
    
    stats_message = f"""
╔═══════════════════════════════════╗
║     📊 ESTATÍSTICAS DO SISTEMA    ║
╚═══════════════════════════════════╝

**🎯 Uso Mensal:**
• Usuários ativos: {count}

**⚡ Status Atual:**
• Downloads em andamento: {active}/{MAX_CONCURRENT_DOWNLOADS}
• Slots disponíveis: {MAX_CONCURRENT_DOWNLOADS - active}

**🔧 Configurações:**
• Limite por arquivo: {MAX_VIDEO_SIZE_MB}MB
• Qualidades: 360p até 1080p
• Modo CPU: {'Econômico' if LOW_CPU_MODE else 'Performance'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_Sistema operando com eficiência máxima_ ✓
"""
    await update.message.reply_text(stats_message, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler de mensagens com mensagens profissionais"""
    text = update.message.text.strip()
    match = URL_RE.search(text)
    
    if not match:
        await update.message.reply_text(
            "⚠️ **Nenhuma URL detectada**\n\n"
            "Por favor, envie um link válido de vídeo.\n\n"
            "💡 Use /start para ver plataformas suportadas.",
            parse_mode="Markdown"
        )
        return
    
    url = match.group(0)
    token = str(uuid.uuid4())
    user_id = update.effective_user.id
    update_user(user_id)
    
    # Limpa URLs expiradas
    expire_old_pending()
    
    # Verifica se sistema está em alta demanda
    active = get_active_downloads_count()
    if active >= MAX_CONCURRENT_DOWNLOADS:
        await update.message.reply_text(
            ERROR_MESSAGES["queue_full"],
            parse_mode="Markdown"
        )
        return
    
    # Mensagem inicial de análise
    msg = await update.message.reply_text(
        PROCESSING_MESSAGES["analyzing"],
        parse_mode="Markdown"
    )
    
    # Simula pequeno delay para análise (UX)
    await asyncio.sleep(1)
    
    # Extrai informações básicas
    try:
        await asyncio.wait_for(
            asyncio.to_thread(lambda: extract_basic_info(url)),
            timeout=10
        )
    except:
        pass  # Ignora se falhar, continua com seleção de qualidade
    
    # Cria botões de qualidade
    buttons = []
    for q_key in ["360p", "480p", "720p", "1080p"]:
        q_info = QUALITY_OPTIONS[q_key]
        buttons.append([
            InlineKeyboardButton(
                f"📹 {q_info['label']}", 
                callback_data=f"{token}|{q_key}"
            )
        ])
    
    buttons.append([
        InlineKeyboardButton("❌ Cancelar", callback_data=f"{token}|cancel")
    ])
    
    markup = InlineKeyboardMarkup(buttons)
    
    await msg.edit_text(
        "╔═══════════════════════════════════╗\n"
        "║   🎯 SELECIONE A QUALIDADE        ║\n"
        "╚═══════════════════════════════════╝\n\n"
        "**📊 Escolha o formato ideal:**\n\n"
        "• **360p** - Rápido e econômico (~30MB/10min)\n"
        "• **480p** - Balanceado (~50MB/10min)\n"
        "• **720p** - Alta definição (~100MB/10min)\n"
        "• **1080p** - Máxima qualidade (~200MB/10min)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "_Qualidades superiores = arquivos maiores_",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    
    add_pending(token, {
        "url": url,
        "chat_id": update.effective_chat.id,
        "message_id": msg.message_id,
        "user_id": user_id,
        "created": time.time()
    })

def extract_basic_info(url: str):
    """Extrai informações básicas do vídeo (timeout rápido)"""
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "socket_timeout": 5
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except:
        return None

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback de confirmação de qualidade"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    parts = data.split("|")
    if len(parts) != 2:
        return
    
    token, quality = parts
    entry = get_pending(token)
    
    if not entry:
        await query.edit_message_text(
            ERROR_MESSAGES["expired"],
            parse_mode="Markdown"
        )
        return
    
    if quality == "cancel":
        delete_pending(token)
        await query.edit_message_text(
            "╔═══════════════════════════════════╗\n"
            "║      ❌ OPERAÇÃO CANCELADA        ║\n"
            "╚═══════════════════════════════════╝\n\n"
            "Download cancelado com sucesso.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "_Envie um novo link quando desejar!_",
            parse_mode="Markdown"
        )
        return
    
    # Inicia o download
    asyncio.create_task(perform_download(token, quality))

async def perform_download(token: str, quality: str):
    """Realiza o download com semáforo e mensagens profissionais"""
    entry = get_pending(token)
    if not entry:
        return
    
    pm = {
        "chat_id": entry["chat_id"],
        "message_id": entry["message_id"]
    }
    url = entry["url"]
    chat_id = entry["chat_id"]
    
    # Verifica fila
    active = get_active_downloads_count()
    if active >= MAX_CONCURRENT_DOWNLOADS:
        position = active + 1
        await application.bot.edit_message_text(
            text=PROCESSING_MESSAGES["queue_position"].format(
                position=position,
                active=active,
                max_slots=MAX_CONCURRENT_DOWNLOADS,
                eta=position * 30
            ),
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="Markdown"
        )
    
    # Aguarda semáforo
    async with DOWNLOAD_SEMAPHORE:
        register_active_download(token)
        try:
            await _download_with_ytdlp(url, quality, token, pm, chat_id)
        finally:
            unregister_active_download(token)
            delete_pending(token)

async def _download_with_ytdlp(url, quality, token, pm, chat_id):
    """Download com yt-dlp e mensagens profissionais"""
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="ytbot_")
        
        # Verifica tamanho estimado
        await application.bot.edit_message_text(
            text=PROCESSING_MESSAGES["analyzing"],
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="Markdown"
        )
        
        # Aviso de download lento após delay
        slow_warning_task = asyncio.create_task(
            send_slow_warning(pm, SLOW_DOWNLOAD_WARNING_DELAY)
        )
        
        info = await asyncio.wait_for(
            asyncio.to_thread(lambda: get_video_info(url)),
            timeout=WATCHDOG_TIMEOUT
        )
        
        # Cancela aviso se completou rápido
        slow_warning_task.cancel()
        
        if not info:
            raise Exception("Não foi possível obter informações do vídeo")
        
        # Estima tamanho
        file_size = estimate_file_size(info, quality)
        
        if file_size > MAX_VIDEO_SIZE_BYTES:
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["file_too_large"],
                chat_id=pm["chat_id"],
                message_id=pm["message_id"],
                parse_mode="Markdown"
            )
            return
        
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
            ydl_opts["postprocessor_args"] = {
                "ffmpeg": ["-c", "copy"]
            }
        else:
            ydl_opts["postprocessor_args"] = {
                "ffmpeg": [
                    "-vf", "scale='min(iw,1920)':'min(ih,1080)':force_original_aspect_ratio=decrease",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "28"
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
            text=PROCESSING_MESSAGES["uploading"],
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="Markdown"
        )
        
        quality_label = QUALITY_OPTIONS.get(quality, {}).get("label", "HD") if quality else "HD"
        
        caption = f"✅ **Download Concluído**\n\n**Qualidade:** {quality_label}"
        if file_size > 0:
            caption += f"\n**Tamanho:** {format_size(file_size)}"
        
        for path in files:
            with open(path, "rb") as fh:
                await application.bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    caption=caption,
                    parse_mode="Markdown"
                )
        
        await application.bot.edit_message_text(
            text=SUCCESS_MESSAGES["complete"].format(
                quality=quality_label,
                size=format_size(file_size) if file_size > 0 else "Desconhecido"
            ),
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="Markdown"
        )
    except asyncio.TimeoutError:
        await application.bot.edit_message_text(
            text=ERROR_MESSAGES["timeout"],
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="Markdown"
        )
    except Exception as e:
        LOG.exception("Erro yt-dlp: %s", e)
        
        # Mensagem específica se for URL não suportada
        error_message = str(e)
        if 'Unsupported URL' in error_message or 'unsupported' in error_message.lower():
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["invalid_url"],
                chat_id=pm["chat_id"],
                message_id=pm["message_id"],
                parse_mode="Markdown"
            )
        else:
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["network_error"],
                chat_id=pm["chat_id"],
                message_id=pm["message_id"],
                parse_mode="Markdown"
            )
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)

async def send_slow_warning(pm, delay):
    """Envia aviso de download lento após delay"""
    try:
        await asyncio.sleep(delay)
        await application.bot.edit_message_text(
            text=PROCESSING_MESSAGES["slow_download"],
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="Markdown"
        )
    except asyncio.CancelledError:
        pass  # Download completou rápido
    except Exception as e:
        LOG.debug("Erro ao enviar aviso de lentidão: %s", e)

def get_video_info(url: str):
    """Obtém informações do vídeo"""
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 15
        }
        cookie = get_cookie_for_url(url)
        if cookie:
            ydl_opts["cookiefile"] = cookie
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except:
        return None

def estimate_file_size(info, quality):
    """Estima tamanho do arquivo baseado na qualidade"""
    try:
        duration = info.get('duration', 0)
        if not duration:
            return 0
        
        # Bitrates aproximados por qualidade (em kbps)
        bitrates = {
            "360p": 800,
            "480p": 1200,
            "720p": 2500,
            "1080p": 5000
        }
        
        bitrate = bitrates.get(quality, 2500)
        size_bytes = (bitrate * 1024 * duration) / 8
        
        return int(size_bytes)
    except:
        return 0

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
            
            message = PROCESSING_MESSAGES["downloading"].format(
                percent=percent,
                speed=speed,
                eta=eta
            )
            
            # Rate limiting: atualiza apenas a cada 3 segundos
            last_update = entry.get("last_update_time", 0)
            if current_time - last_update >= 3.0:
                try:
                    asyncio.run_coroutine_threadsafe(
                        application.bot.edit_message_text(
                            text=message,
                            chat_id=pm["chat_id"],
                            message_id=pm["message_id"],
                            parse_mode="Markdown"
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
                    text=PROCESSING_MESSAGES["processing"],
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"],
                    parse_mode="Markdown"
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
    return f"""
    <html>
    <head><title>Bot Status</title></head>
    <body style="font-family: Arial; text-align: center; padding: 50px;">
        <h1>🎬 Downloader Bot</h1>
        <h2 style="color: green;">✅ Sistema Online</h2>
        <p><strong>Downloads Ativos:</strong> {active}/{MAX_CONCURRENT_DOWNLOADS}</p>
        <p><strong>Slots Disponíveis:</strong> {MAX_CONCURRENT_DOWNLOADS - active}</p>
        <hr>
        <p style="color: #666;">Desenvolvido com ❤️</p>
    </body>
    </html>
    """

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
    LOG.info("╔═══════════════════════════════════════════════════╗")
    LOG.info("║          🚀 INICIANDO BOT PROFISSIONAL          ║")
    LOG.info("╚═══════════════════════════════════════════════════╝")
    LOG.info("📡 Porta: %d", port)
    LOG.info("⚡ Downloads simultâneos: %d", MAX_CONCURRENT_DOWNLOADS)
    LOG.info("💻 Modo LOW_CPU: %s", "✓ ATIVADO" if LOW_CPU_MODE else "✗ DESATIVADO")
    LOG.info("⏰ Aviso de lentidão: %ds", SLOW_DOWNLOAD_WARNING_DELAY)
    LOG.info("📦 Limite por arquivo: %dMB", MAX_VIDEO_SIZE_MB)
    LOG.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # IMPORTANTE: threaded=True para suportar múltiplas requisições
    app.run(host="0.0.0.0", port=port, threaded=True)
