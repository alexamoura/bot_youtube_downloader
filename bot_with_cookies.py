#!/usr/bin/env python3
"""
bot_with_cookies_melhorado.py - Versão Profissional

Telegram bot IA (webhook) com sistema de controle de downloads e suporte a pagamento PIX - ATUALIZADO EM 04/11/2025 - 15:00HS
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

# ============================================================
# SHOPEE VIDEO EXTRACTOR - SEM MARCA D'ÁGUA
# ============================================================

class ShopeeVideoExtractor:
    """Extrator de vídeos da Shopee sem marca d'água usando API interna"""
    
    def __init__(self):
        self.session = requests.Session() if REQUESTS_AVAILABLE else None
        if self.session:
            self.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
                'Referer': 'https://shopee.com.br/',
            })
    
    def extract_ids(self, url: str):
        """Extrai shop_id e item_id da URL"""
        patterns = [
            r'/product/(\d+)/(\d+)',
            r'-i\.(\d+)\.(\d+)',
            r'\.i\.(\d+)\.(\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return (match.group(1), match.group(2))
        return None
    
    def remove_watermark_pattern(self, video_url: str) -> str:
        """
        Remove padrão de marca d'água da URL
        Padrão: .123.456.mp4 → .mp4
        """
        if not video_url:
            return None
        
        # Remove .NUMERO.NUMERO antes de .mp4
        clean_url = re.sub(r'\.\d+\.\d+(?=\.mp4)', '', video_url)
        
        if clean_url != video_url:
            LOG.info("✨ Marca d'água removida da URL")
            LOG.debug("   Original: %s", video_url[:80])
            LOG.debug("   Limpa: %s", clean_url[:80])
        
        return clean_url
    
    def extract_from_next_data(self, url: str):
        """
        Extrai vídeo do __NEXT_DATA__ (técnica Next.js)
        Esta é a técnica DEFINITIVA para remover marca d'água!
        """
        if not REQUESTS_AVAILABLE or not self.session:
            return None
        
        try:
            LOG.info("🎯 Usando técnica __NEXT_DATA__ (SEM marca d'água garantido!)")
            
            # Busca HTML da página
            response = self.session.get(url, timeout=10)
            html = response.text
            
            # Extrai __NEXT_DATA__ script tag
            pattern = r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>'
            match = re.search(pattern, html, re.DOTALL)
            
            if not match:
                LOG.warning("⚠️ __NEXT_DATA__ não encontrado")
                return None
            
            # Parse JSON
            import json
            data = json.loads(match.group(1))
            LOG.info("✅ __NEXT_DATA__ extraído com sucesso!")
            
            # Navega no JSON para encontrar vídeo
            # Caminho: props.pageProps.mediaInfo.video.watermarkVideoUrl
            video_data = data.get('props', {}).get('pageProps', {}).get('mediaInfo', {}).get('video', {})
            
            if not video_data:
                # Tenta caminho alternativo
                video_data = data.get('props', {}).get('pageProps', {}).get('item', {})
            
            # Pega URL com marca
            watermark_url = video_data.get('watermarkVideoUrl')
            
            if not watermark_url:
                # Tenta outros campos
                watermark_url = video_data.get('url') or video_data.get('video_url')
            
            if watermark_url:
                # Remove padrão de marca d'água
                clean_url = self.remove_watermark_pattern(watermark_url)
                
                # Extrai título
                title = video_data.get('title')
                if not title:
                    title = data.get('props', {}).get('pageProps', {}).get('item', {}).get('name', 'Vídeo da Shopee')
                
                LOG.info("🎬 Vídeo SEM marca d'água extraído!")
                
                return {
                    'url': clean_url,
                    'url_with_watermark': watermark_url,
                    'title': title,
                    'uploader': 'Shopee',
                    'no_watermark': True,  # Flag importante!
                }
            
            LOG.warning("⚠️ URL do vídeo não encontrada no __NEXT_DATA__")
            return None
            
        except Exception as e:
            LOG.error("❌ Erro ao extrair do __NEXT_DATA__: %s", e)
            return None
    
    def extract_video_from_html(self, url: str):
        """Extrai vídeo diretamente do HTML para URLs sv.shopee.com.br"""
        if not REQUESTS_AVAILABLE or not self.session:
            return None
        
        try:
            LOG.info("🔍 Extraindo vídeo do HTML da página...")
            response = self.session.get(url, timeout=10)
            html = response.text
            
            # Padrões para encontrar URL do vídeo
            patterns = [
                r'"video_url"\s*:\s*"([^"]+)"',
                r'"url"\s*:\s*"(https://[^"]*\.mp4[^"]*)"',
                r'(https://cf\.shopee\.com\.br/file/[a-zA-Z0-9_-]+)',
                r'(https://[^"\']*shopee[^"\']*\.mp4[^"\']*)',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, html)
                if matches:
                    video_url = matches[0].replace('\\/', '/')
                    LOG.info("✅ URL de vídeo encontrada no HTML!")
                    return {
                        'url': video_url,
                        'title': 'Vídeo da Shopee',
                        'uploader': 'Desconhecido',
                    }
            
            return None
            
        except Exception as e:
            LOG.error("Erro ao extrair do HTML: %s", e)
            return None
    
    def get_video(self, url: str):
        """Extrai vídeo da Shopee sem marca d'água - PRIORIZA __NEXT_DATA__"""
        if not REQUESTS_AVAILABLE or not self.session:
            return None
        
        try:
            # 🎯 MÉTODO 1 (PRIORITÁRIO): __NEXT_DATA__ - SEM marca d'água GARANTIDO!
            LOG.info("🎯 MÉTODO 1: Tentando __NEXT_DATA__ (técnica definitiva)...")
            next_data_result = self.extract_from_next_data(url)
            
            if next_data_result:
                LOG.info("🎉 __NEXT_DATA__ funcionou - SEM marca d'água!")
                return next_data_result
            
            LOG.info("⚠️ __NEXT_DATA__ falhou, tentando outros métodos...")
            
            # 🔧 MÉTODO 2: Se for URL de vídeo (sv.shopee.com.br), usa extração HTML
            if 'sv.shopee' in url.lower() or 'share-video' in url.lower():
                LOG.info("🎬 MÉTODO 2: URL de vídeo direto (sv.shopee.com.br)")
                return self.extract_video_from_html(url)
            
            # 🔧 MÉTODO 3: API /item/get
            ids = self.extract_ids(url)
            if not ids:
                LOG.warning("⚠️ Não foi possível extrair IDs, tentando HTML...")
                return self.extract_video_from_html(url)
            
            shop_id, item_id = ids
            LOG.info("🔧 MÉTODO 3: API /item/get - Shop: %s, Item: %s", shop_id, item_id)
            
            api_url = "https://shopee.com.br/api/v4/item/get"
            params = {'itemid': item_id, 'shopid': shop_id}
            
            response = self.session.get(api_url, params=params, timeout=10)
            data = response.json()
            
            if 'data' not in data:
                LOG.warning("⚠️ API falhou, tentando HTML...")
                return self.extract_video_from_html(url)
            
            item = data['data']
            
            # Tenta extrair vídeo da API
            if 'video_info_list' in item and item['video_info_list']:
                video = item['video_info_list'][0]
                if 'default_format' in video:
                    video_url = video['default_format'].get('url')
                    # Remove marca d'água se tiver padrão
                    clean_url = self.remove_watermark_pattern(video_url)
                    
                    LOG.info("✅ Vídeo da API (marca removida se presente)")
                    return {
                        'url': clean_url,
                        'title': item.get('name', 'Vídeo da Shopee'),
                        'uploader': item.get('shop_name', 'Desconhecido'),
                    }
            
            if 'video' in item and item['video']:
                video_url = item['video'].get('url')
                clean_url = self.remove_watermark_pattern(video_url)
                
                LOG.info("✅ Vídeo da API campo video (marca removida)")
                return {
                    'url': clean_url,
                    'title': item.get('name', 'Vídeo da Shopee'),
                    'uploader': item.get('shop_name', 'Desconhecido'),
                }
            
            LOG.warning("⚠️ API sem vídeo, tentando HTML...")
            return self.extract_video_from_html(url)
            
        except Exception as e:
            LOG.error("Erro no ShopeeVideoExtractor: %s", e)
            return None

# Instância global
SHOPEE_EXTRACTOR = ShopeeVideoExtractor()


# ============================================================
# WATERMARK REMOVER - Remove marca d'água após download
# ============================================================

class WatermarkRemover:
    """Remove marca d'água de vídeos da Shopee usando FFmpeg"""
    
    # Posições da marca d'água da Shopee
    # CORREÇÃO: Marca fica no MEIO VERTICAL, LADO DIREITO ✅
    POSITIONS = {
        'middle_right': '(iw-210):(ih/2-25):200:50',      # Meio direito (PRINCIPAL) ✅
        'middle_right_high': '(iw-210):(ih/2-100):200:50', # Meio direito mais acima
        'middle_right_low': '(iw-210):(ih/2+50):200:50',   # Meio direito mais abaixo
        'middle_center': '(iw/2-100):(ih/2-25):200:50',    # Centro da tela
        'bottom_right': '(iw-210):(ih-60):200:50',         # Canto inferior direito
        'top_right': '(iw-210):10:200:50',                 # Canto superior direito
        'bottom_left': '10:(ih-60):200:50',                # Canto inferior esquerdo
        'top_left': '10:10:200:50'                         # Canto superior esquerdo
    }

    
    @staticmethod
    def is_available() -> bool:
        """Verifica se FFmpeg está disponível"""
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
            return True
        except:
            return False
    
    @staticmethod
    def remove(video_path: str, position: str = 'middle_right') -> str:
        """
        Remove marca d'água do vídeo
        
        Args:
            video_path: Caminho do vídeo
            position: Posição da marca (padrão: middle_right - meio direito)
        
        Returns:
            Caminho do vídeo limpo ou original se falhar
        """
        if not WatermarkRemover.is_available():
            LOG.warning("⚠️ FFmpeg não disponível - vídeo mantém marca")
            return video_path
        
        if position not in WatermarkRemover.POSITIONS:
            position = 'middle_right'
        
        try:
            LOG.info(f"🎬 Removendo marca d'água (posição: {position})...")
            
            # Cria arquivo temporário
            base, ext = os.path.splitext(video_path)
            temp_path = f"{base}_temp{ext}"
            
            # Comando FFmpeg
            coords = WatermarkRemover.POSITIONS[position]
            cmd = [
                'ffmpeg',
                '-i', video_path,
                '-vf', f'delogo=x={coords}:show=0',
                '-c:a', 'copy',
                '-y',
                temp_path
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60  # 60 segundos max
            )
            
            if result.returncode == 0 and os.path.exists(temp_path):
                # Substitui original
                os.remove(video_path)
                os.rename(temp_path, video_path)
                LOG.info("✅ Marca d'água removida com sucesso!")
                return video_path
            else:
                LOG.error(f"❌ FFmpeg falhou: {result.stderr[:200]}")
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                return video_path
                
        except subprocess.TimeoutExpired:
            LOG.error("❌ Timeout ao remover marca")
            return video_path
        except Exception as e:
            LOG.error(f"❌ Erro ao remover marca: {e}")
            return video_path


# Instância global do removedor
WATERMARK_REMOVER = WatermarkRemover()


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

# Configuração de Logging Otimizada
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),  # Console
        logging.handlers.RotatingFileHandler(
            'bot.log',
            maxBytes=5*1024*1024,  # 5MB máximo
            backupCount=2,  # Mantém apenas 2 arquivos de backup
            encoding='utf-8'
        ) if os.path.exists('/tmp') else logging.StreamHandler()
    ]
)
LOG = logging.getLogger("ytbot")
LOG.setLevel(logging.WARNING)  # WARNING em produção economiza memória


# Token do Bot
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido.")
    sys.exit(1)

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# Constantes do Sistema
URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = os.getenv("DB_FILE", "/data/users.db") if os.path.exists("/data") else "users.db"
PENDING_MAX_SIZE = 1000
PENDING_EXPIRE_SECONDS = 600
WATCHDOG_TIMEOUT = 180
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB - limite do Telegram para bots (API padrão)
SPLIT_SIZE = 45 * 1024 * 1024

# Constantes de Controle de Downloads
FREE_DOWNLOADS_LIMIT = 10
MAX_CONCURRENT_DOWNLOADS = 3  # Até 3 downloads simultâneos

# Configuração do Mercado Pago
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
PREMIUM_PRICE = float(os.getenv("PREMIUM_PRICE", "9.90"))
PREMIUM_DURATION_DAYS = int(os.getenv("PREMIUM_DURATION_DAYS", "30"))

if MERCADOPAGO_AVAILABLE and MERCADOPAGO_ACCESS_TOKEN:
    LOG.info("✅ Mercado Pago configurado - Token: %s...", MERCADOPAGO_ACCESS_TOKEN[:20])
else:
    if not MERCADOPAGO_AVAILABLE:
        LOG.warning("⚠️ mercadopago não instalado - pip install mercadopago")
    if not MERCADOPAGO_ACCESS_TOKEN:
        LOG.warning("⚠️ MERCADOPAGO_ACCESS_TOKEN não configurado")

# Configuração do Groq (IA)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = None

if GROQ_AVAILABLE and GROQ_API_KEY:
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
        LOG.info("✅ Groq AI configurado - Inteligência artificial ativa!")
    except Exception as e:
        LOG.error("❌ Erro ao inicializar Groq: %s", e)
        groq_client = None
else:
    if not GROQ_AVAILABLE:
        LOG.warning("⚠️ groq não instalado - pip install groq")
    if not GROQ_API_KEY:
        LOG.warning("⚠️ GROQ_API_KEY não configurado - IA desativada")

# Estado Global
PENDING = OrderedDict()
DB_LOCK = threading.Lock()
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)  # Controle de fila
ACTIVE_DOWNLOADS = {}  # Rastreamento de downloads ativos

# Mensagens Profissionais do Bot
MESSAGES = {
    "welcome": (
        "🎥 <b>Bem-vindo ao Serviço de Downloads</b>\n\n"
        "Envie um link de vídeo do TikTok, Instagram ou Shopee e eu processarei o download para você.\n\n"
        "📊 <b>Planos disponíveis:</b>\n"
        "• Gratuito: {free_limit} downloads/mês\n"
        "• Premium: Downloads ilimitados\n\n"
        "⚙️ <b>Especificações:</b>\n"
        "• Vídeos curtos (até 50 MB)\n"
        "• Qualidade até 720p\n"
        "• Fila: até 3 downloads simultâneos\n\n"
        "Digite /status para verificar seu saldo de downloads ou /premium para assinar o plano."
    ),
    "url_prompt": "📎 Por favor, envie o link do vídeo que deseja baixar.",
    "processing": "⚙️ Processando sua solicitação...",
    "invalid_url": "⚠️ O link fornecido não é válido. Por favor, verifique e tente novamente.",
    "file_too_large": "⚠️ <b>Arquivo muito grande</b>\n\nEste vídeo excede o limite de 50 MB. Por favor, escolha um vídeo mais curto.",
    "confirm_download": "🎬 <b>Confirmar Download</b>\n\n📹 Vídeo: {title}\n⏱️ Duração: {duration}\n📦 Tamanho: {filesize}\n\n✅ Deseja prosseguir com o download?",
    "queue_position": "⏳ Aguardando na fila... Posição: {position}\n\n{active} downloads em andamento.",
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
        "1️⃣ Clique no botão \"Assinar Premium\"\n"
        "2️⃣ Escaneie o QR Code PIX gerado\n"
        "3️⃣ Confirme o pagamento no seu banco\n"
        "4️⃣ Aguarde a ativação automática (30-60 segundos)\n\n"
        "⚡ <b>Ativação instantânea via PIX!</b>"
    ),
    "stats": "📈 <b>Estatísticas do Bot</b>\n\n👥 Usuários ativos este mês: {count}",
    "error_timeout": "⏱️ O tempo de processamento excedeu o limite. Por favor, tente novamente.",
    "error_network": "🌐 Erro de conexão detectado. Verifique sua internet e tente novamente em alguns instantes.",
    "error_file_large": "📦 O arquivo excede o limite de 50 MB. Por favor, escolha um vídeo mais curto.",
    "error_ffmpeg": "🎬 Ocorreu um erro durante o processamento do vídeo.",
    "error_upload": "📤 Falha ao enviar o arquivo. Por favor, tente novamente.",
    "error_unknown": "❌ Um erro inesperado ocorreu. Nossa equipe foi notificada. Por favor, tente novamente.",
    "error_expired": "⏰ Esta solicitação expirou. Por favor, envie o link novamente.",
    "download_cancelled": "🚫 Download cancelado com sucesso.",
    "cleanup": "🧹 Limpeza: removido {path}",
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
            c.execute("SELECT downloads_count, is_premium, last_reset, premium_expires FROM user_downloads WHERE user_id=?", (user_id,))
            row = c.fetchone()
            
            current_month = time.strftime("%Y-%m")
            today = time.strftime("%Y-%m-%d")
            
            if row:
                downloads_count, is_premium, last_reset, premium_expires = row
                
                # ✅ VERIFICA SE PREMIUM EXPIROU
                if is_premium and premium_expires:
                    if today > premium_expires:
                        # Premium expirou! Volta para plano gratuito
                        LOG.info(f"🔔 Premium expirou para usuário {user_id} (expirou em {premium_expires})")
                        is_premium = 0
                        downloads_count = 0  # Reseta contador
                        c.execute("""
                            UPDATE user_downloads 
                            SET is_premium=0, downloads_count=0, last_reset=? 
                            WHERE user_id=?
                        """, (current_month, user_id))
                        conn.commit()
                
                # Reseta contador se mudou o mês (apenas para plano gratuito)
                elif last_reset != current_month and not is_premium:
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

def get_format_for_url(url: str) -> str:
    """Retorna o formato apropriado baseado na plataforma - OTIMIZADO PARA 50MB"""
    url_lower = url.lower()
    
    # Shopee: melhor qualidade disponível (geralmente já é pequeno)
    if 'shopee' in url_lower or 'shope.ee' in url_lower:
        LOG.info("🛍️ Formato Shopee: best (otimizado)")
        return "best[ext=mp4][filesize<=50M]/best[ext=mp4]/best"
    
    # Instagram: formato único já otimizado
    elif 'instagram' in url_lower or 'insta' in url_lower:
        LOG.info("📸 Formato Instagram: best (otimizado)")
        return "best[ext=mp4]/best"
    
    # YouTube: 720p ou 480p, formato já combinado para evitar cortes
    elif 'youtube' in url_lower or 'youtu.be' in url_lower:
        LOG.info("🎥 Formato YouTube: até 1080p (otimizado, sem cortes)")
        # Prioriza formatos já combinados (evita cortes) e limita tamanho
        return "best[height<=720][ext=mp4]/best[height<=480][ext=mp4]/best[ext=mp4]/best"
    
    # Outras plataformas: formato otimizado
    else:
        LOG.info("🎬 Formato padrão: best (otimizado)")
        return "best[ext=mp4]/best"


def resolve_shopee_universal_link(url: str) -> str:
    """Resolve universal links da Shopee para URL real"""
    try:
        # Detecta se é universal-link
        if 'universal-link' not in url:
            return url
        
        # Método 1: Extrai do parâmetro redir
        if 'redir=' in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'redir' in params:
                redir = unquote(params['redir'][0])
                LOG.info("🔗 Universal link resolvido: %s", redir[:80])
                return redir
        
        # Método 2: Tenta seguir redirect HTTP
        try:
            import requests
            response = requests.head(url, allow_redirects=True, timeout=5)
            if response.url != url:
                LOG.info("🔗 Redirect HTTP seguido: %s", response.url[:80])
                return response.url
        except:
            pass
        
        LOG.warning("⚠️ Não foi possível resolver universal-link")
        return url
        
    except Exception as e:
        LOG.error("Erro ao resolver universal link: %s", e)
        return url


def expand_short_url(url: str) -> str:
    """
    Expande links encurtados da Shopee (br.shp.ee, shope.ee)
    
    Retorna a URL expandida ou None se falhar
    """
    try:
        if not REQUESTS_AVAILABLE:
            LOG.warning("⚠️ requests não disponível para expandir link")
            return None
        
        import requests
        
        LOG.info("🔗 Expandindo link encurtado: %s", url[:50])
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
        }
        
        # Tenta seguir redirects
        response = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        
        if response.url != url:
            LOG.info("✅ Link expandido: %s", response.url[:80])
            return response.url
        else:
            LOG.warning("⚠️ Link não redirecionou")
            return None
            
    except requests.exceptions.RequestException as e:
        LOG.error("❌ Erro ao expandir link: %s", e)
        return None
    except Exception as e:
        LOG.error("❌ Erro inesperado ao expandir link: %s", e)
        return None


def extract_shopee_video_direct(url: str) -> dict:
    """
    Extrai informações de vídeo da Shopee diretamente da página.
    Usado quando yt-dlp não suporta o formato.
    """
    try:
        import requests
        import re
        import json
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://shopee.com.br/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        
        LOG.info("🛍️ Tentando extração direta da Shopee...")
        response = requests.get(url, headers=headers, timeout=10)
        html = response.text
        
        # Procura por URLs de vídeo no HTML/JavaScript
        video_patterns = [
            r'"video_url"\s*:\s*"([^"]+)"',
            r'"url"\s*:\s*"(https://[^"]*\.mp4[^"]*)"',
            r'https://cf\.shopee\.com\.br/file/[a-zA-Z0-9]+',
            r'https://[^"\']*shopee[^"\']*\.mp4[^"\']*',
        ]
        
        video_url = None
        for pattern in video_patterns:
            matches = re.findall(pattern, html)
            if matches:
                video_url = matches[0].replace('\\/', '/')
                LOG.info("✅ URL de vídeo encontrada: %s", video_url[:80])
                break
        
        if video_url:
            return {
                'url': video_url,
                'title': 'Vídeo da Shopee',
                'ext': 'mp4',
                'direct': True  # Marca como extração direta
            }
        
        LOG.warning("⚠️ Nenhuma URL de vídeo encontrada na página")
        return None
        
    except Exception as e:
        LOG.error("Erro na extração direta: %s", e)
        return None

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

def format_filesize(bytes_size: int) -> str:
    """Formata tamanho de arquivo em bytes para formato legível"""
    if not bytes_size:
        return "N/A"
    
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    
    return f"{bytes_size:.1f} TB"

async def _download_shopee_video(url: str, tmpdir: str, chat_id: int, pm: dict):
    """Download especial para Shopee Video usando extração avançada"""
    if not REQUESTS_AVAILABLE:
        await application.bot.edit_message_text(
            text="⚠️ Extrator Shopee não disponível. Instale: pip install requests beautifulsoup4",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        return

    try:
        # Atualiza mensagem
        await application.bot.edit_message_text(
            text="🛍️ Extraindo vídeo da Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        LOG.info("Iniciando extração customizada da Shopee: %s", url)

        # Prepara headers e cookies para download (usados em ambos os métodos)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://shopee.com.br/",
        }
        
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

        # 🎯 MÉTODO 1: Usa ShopeeVideoExtractor (API interna)
        LOG.info("🎯 Tentando método ShopeeVideoExtractor (API)...")
        video_info = SHOPEE_EXTRACTOR.get_video(url)
        
        video_url = None
        url_already_clean = False  # Flag para saber se URL já está sem marca
        
        if video_info and video_info.get('url'):
            LOG.info("✅ Vídeo extraído via ShopeeVideoExtractor!")
            video_url = video_info['url']
            # Verifica se a marca já foi removida na URL
            url_already_clean = video_info.get('no_watermark', False)
            if url_already_clean:
                LOG.info("✨ URL já está SEM marca d'água - FFmpeg não necessário!")
        else:
            LOG.warning("⚠️ ShopeeVideoExtractor falhou, tentando método HTML...")
            
            # 🔧 MÉTODO 2: Scraping HTML (fallback)

            response = await asyncio.to_thread(
                lambda: requests.get(url, headers=headers, cookies=cookies_dict, timeout=30)
            )
            response.raise_for_status()
            LOG.info("Página da Shopee carregada, analisando...")

            # Busca URL do vídeo no HTML com múltiplos padrões
            patterns = [
                # Padrões comuns da Shopee
                r'"videoUrl"\s*:\s*"([^"]+)"',
                r'"video_url"\s*:\s*"([^"]+)"',
                r'"playAddr"\s*:\s*"([^"]+)"',
                r'"url"\s*:\s*"(https://[^"]*\.mp4[^"]*)"',
                # Padrões do domínio específico
                r'(https://down-[^"]*\.vod\.susercontent\.com[^"]*)',
                r'(https://[^"]*susercontent\.com[^"]*\.mp4[^"]*)',
                r'(https://cf\.shopee\.com\.br/file/[^"]+)',
                # Padrão watermarkVideoUrl
                r'"watermarkVideoUrl"\s*:\s*"([^"]+)"',
                r'"defaultFormat"[^}]*"url"\s*:\s*"([^"]+)"',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, response.text)
                if matches:
                    video_url = matches[0].replace('\\/', '/')
                    LOG.info("URL de vídeo encontrada via regex: %s", video_url[:100])
                    break
        
        # Verifica se conseguiu URL por qualquer método
        if not video_url:
            LOG.error("Nenhuma URL de vídeo encontrada (todos os métodos falharam)")
            await application.bot.edit_message_text(
                text="⚠️ <b>Não consegui encontrar o vídeo</b>\n\n"
                     "Possíveis causas:\n"
                     "• O link pode estar incorreto\n"
                     "• O vídeo pode ter sido removido\n"
                     "• A Shopee mudou a estrutura do site\n\n"
                     "Tente baixar pelo app oficial da Shopee.",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"],
                parse_mode="HTML"
            )
            return

        # Ajusta URL se necessário
        if not video_url.startswith('http'):
            video_url = 'https:' + video_url if video_url.startswith('//') else 'https://sv.shopee.com.br' + video_url

        LOG.info("Baixando vídeo da URL: %s", video_url[:100])

        # Atualiza mensagem
        await application.bot.edit_message_text(
            text="📥 Baixando vídeo da Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )

        # Baixa o vídeo
        output_path = os.path.join(tmpdir, "shopee_video.mp4")
        video_response = await asyncio.to_thread(
            lambda: requests.get(video_url, headers=headers, cookies=cookies_dict, stream=True, timeout=120)
        )
        video_response.raise_for_status()
        total_size = int(video_response.headers.get('content-length', 0))

        # Shopee: SEM limite de tamanho (Telegram suporta até 2GB com Bot API)
        LOG.info("📦 Tamanho do vídeo Shopee: %.2f MB", total_size / (1024 * 1024))

        with open(output_path, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        LOG.info("✅ Vídeo da Shopee baixado com sucesso: %s", output_path)

        # ✅ Remove marca d'água SOMENTE se necessário
        if url_already_clean:
            # Marca já foi removida na URL - FFmpeg não necessário!
            LOG.info("✅ Vídeo baixado já SEM marca d'água (removida na URL)")
            caption = "🛍️ Shopee Video\n✨ Marca d'água removida (método URL)"
        elif WATERMARK_REMOVER.is_available():
            # Marca ainda presente - usar FFmpeg
            LOG.info("🎬 Marca d'água ainda presente - usando FFmpeg...")
            await application.bot.edit_message_text(
                text="✨ Removendo marca d'água...",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )

            # POSIÇÃO CORRETA: MEIO DIREITO ✅
            cleaned_path = WATERMARK_REMOVER.remove(output_path, position='middle_right')
            if not os.path.exists(cleaned_path):
                LOG.warning("⚠️ Falha na posição middle_right, tentando outras...")
                for pos in ['middle_right_high', 'middle_right_low', 'middle_center', 'bottom_right']:
                    cleaned_path = WATERMARK_REMOVER.remove(output_path, position=pos)
                    if os.path.exists(cleaned_path):
                        break

            output_path = cleaned_path if os.path.exists(cleaned_path) else output_path
            caption = "🛍️ Shopee Video\n✨ Marca d'água removida (método FFmpeg)"
        else:
            LOG.warning("⚠️ FFmpeg não disponível, enviando vídeo original.")
            caption = "🛍️ Shopee Video"

        # Envia o vídeo
        await application.bot.edit_message_text(
            text="✅ Download concluído, enviando...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )

        with open(output_path, "rb") as fh:
            await application.bot.send_video(chat_id=chat_id, video=fh, caption=caption)

        # Mensagem de sucesso
        stats = get_user_download_stats(pm["user_id"])
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
        LOG.exception("Erro no download Shopee customizado: %s", e)
        await application.bot.edit_message_text(
            text="⚠️ <b>Erro ao baixar vídeo da Shopee</b>\n\n"
                 "A Shopee pode ter proteções especiais neste vídeo. "
                 "Tente baixar pelo app oficial.",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="HTML"
        )
        return
       
        # Ajusta URL se necessário
        if not video_url.startswith('http'):
            video_url = 'https:' + video_url if video_url.startswith('//') else 'https://sv.shopee.com.br' + video_url
        
        LOG.info("Baixando vídeo da URL: %s", video_url[:100])
        
        # Atualiza mensagem
        await application.bot.edit_message_text(
            text="📥 Baixando vídeo da Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        # Baixa o vídeo
        output_path = os.path.join(tmpdir, "shopee_video.mp4")
        
        video_response = await asyncio.to_thread(
            lambda: requests.get(video_url, headers=headers, cookies=cookies_dict, stream=True, timeout=120)
        )
        video_response.raise_for_status()
        
        total_size = int(video_response.headers.get('content-length', 0))
        
        # Shopee: SEM limite de tamanho
        LOG.info("📦 Tamanho do vídeo Shopee: %.2f MB", total_size / (1024 * 1024))
        
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
                            bar = "█" * blocks + "░" * (20 - blocks)
                            try:
                                await application.bot.edit_message_text(
                                    text=f"📥 Shopee: {percent}%\n{bar}",
                                    chat_id=pm["chat_id"],
                                    message_id=pm["message_id"]
                                )
                            except:
                                pass
        
        LOG.info("Vídeo da Shopee baixado com sucesso: %s", output_path)
        
        # Verifica se arquivo foi criado
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            raise Exception("Arquivo baixado está vazio ou corrompido")
        
        # Envia o vídeo
        await application.bot.edit_message_text(
            text="✅ Download concluído, enviando...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
        
        with open(output_path, "rb") as fh:
            await application.bot.send_video(chat_id=chat_id, video=fh, caption="🛍️ Shopee Video")
        
        # Mensagem de sucesso com contador
        stats = get_user_download_stats(pm["user_id"])
        success_text = MESSAGES["upload_complete"].format(
            remaining=stats["remaining"],
            total=stats["limit"] if not stats["is_premium"] else "∞"
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
            text="⚠️ <b>Erro ao baixar vídeo da Shopee</b>\n\n"
                 "A Shopee pode ter proteções especiais neste vídeo. "
                 "Tente baixar pelo app oficial.",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"],
            parse_mode="HTML"
        )

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
    
    # Verifica data de expiração se for premium
    premium_info = ""
    if stats["is_premium"]:
        # Busca data de expiração
        try:
            with DB_LOCK:
                conn = sqlite3.connect(DB_FILE, timeout=5)
                c = conn.cursor()
                c.execute("SELECT premium_expires FROM user_downloads WHERE user_id=?", (user_id,))
                row = c.fetchone()
                conn.close()
                
                if row and row[0]:
                    expires_date = row[0]
                    premium_info = f"✅ Plano: <b>Premium Ativo</b>\n📅 Expira em: <b>{expires_date}</b>"
                else:
                    premium_info = "✅ Plano: <b>Premium Ativo</b>"
        except:
            premium_info = "✅ Plano: <b>Premium Ativo</b>"
    else:
        premium_info = "📦 Plano: <b>Gratuito</b>"
    
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

async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para o comando /ai - conversar com IA"""
    if not groq_client:
        await update.message.reply_text(
            "🤖 <b>IA Não Disponível</b>\n\n"
            "A inteligência artificial não está configurada no momento.\n"
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
            system_prompt="""Você é um assistente amigável para um bot de downloads do Telegram.
- Seja útil, direto e use frases curtas.
- Utilize emojis apenas quando fizer sentido.
- Nunca invente informações. Se não souber, responda exatamente: "Não tenho essa informação".
- Não forneça detalhes que não estejam listados abaixo.
- Se o usuário quiser assinar o plano, peça para digitar /premium.
- Este bot não faz download de músicas e não permite escolher qualidade de vídeos.
- Não responda sobre assuntos que não sejam relacionados ao que esse assistente faz

Funcionalidades:
- Download de vídeos (Shopee, Instagram, TikTok, Twitter, etc.)
- Plano gratuito: 10 downloads/mês
- Plano premium: downloads ilimitados (R$9,90/mês)
- Se o usuário falar para você baixar algum vídeo, incentive ele a te enviar um link
"""
        )
        
        if response:
            await update.message.reply_text(response, parse_mode="HTML")
        else:
            await update.message.reply_text(
                "⚠️ Erro ao processar sua mensagem. Tente novamente."
            )
    else:
        # Sem argumentos, mostra instruções
        await update.message.reply_text(
            "🤖 <b>Assistente com IA</b>\n\n"
            "Converse comigo! Use:\n"
            "• <code>/ai sua pergunta aqui</code>\n\n"
            "<b>Ou simplesmente envie uma mensagem de texto!</b>\n\n"
            "<i>Exemplos:</i>\n"
            "• /ai como baixar vídeos?\n"
            "• /ai o que é o plano premium?\n"
            "• /ai me recomende vídeos sobre Música",
            parse_mode="HTML"
        )
    
    LOG.info("Comando /ai executado por usuário %d", update.effective_user.id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para mensagens de texto (URLs ou chat com IA)"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    update_user(user_id)
    
    # Verifica se é um link válido
    urls = URL_RE.findall(text)
    if not urls:
        # Não há URL - verifica se tem IA disponível para chat
        if groq_client:
            # Analisa intenção do usuário
            intent_data = await analyze_user_intent(text)
            intent = intent_data.get('intent', 'chat')
            
            # Se for pedido de ajuda ou chat geral, responde com IA
            if intent in ['help', 'chat']:
                LOG.info("💬 Chat IA - Usuário %d: %s", user_id, text[:50])
                await update.message.chat.send_action("typing")
                
                response = await chat_with_ai(
                    text,
                    system_prompt="""Você é um assistente amigável para um bot de downloads do Telegram.
- Seja útil, direto e use frases curtas.
- Utilize emojis apenas quando fizer sentido.
- Nunca invente informações. Se não souber, responda exatamente: "Não tenho essa informação".
- Não forneça detalhes que não estejam listados abaixo.
- Se o usuário quiser assinar o plano, peça para digitar /premium.
- Este bot não faz download de músicas e não permite escolher qualidade de vídeos.
- Não responda sobre assuntos que não sejam relacionados ao que esse assistente faz

Funcionalidades:
- Download de vídeos (Shopee, Instagram, TikTok, Twitter, etc.)
- Plano gratuito: 10 downloads/mês
- Plano premium: downloads ilimitados (R$9,90/mês)
- Se o usuário falar para você baixar algum vídeo, incentive ele a te enviar um link

Comandos:
/start - Iniciar
/status - Ver estatísticas
/premium - Plano premium 
"""
                )
                
                if response:
                    await update.message.reply_text(response)
                else:
                    await update.message.reply_text(
                        "⚠️ Desculpe, não consegui processar sua mensagem.\n\n"
                        "💡 <b>Dica:</b> Para baixar vídeos, envie um link!\n"
                        "Use /ai para conversar comigo.",
                        parse_mode="HTML"
                    )
                return
        
        # Sem IA ou não conseguiu processar - mostra mensagem padrão
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
    
    # 🔗 PASSO 1: Expande links encurtados (br.shp.ee, shope.ee)
    if 'shp.ee' in url.lower() or 'shope.ee' in url.lower():
        LOG.info("🔗 Link encurtado detectado! Tentando expandir...")
        
        expanded = expand_short_url(url)
        
        if expanded:
            LOG.info("✅ Link expandido com sucesso!")
            url = expanded
        else:
            # Se falhar, avisa o usuário
            await update.message.reply_text(
                "🔗 <b>Link Encurtado Detectado</b>\n\n"
                "⚠️ Não foi possível expandir automaticamente.\n\n"
                "Por favor:\n"
                "1️⃣ Abra o link no navegador\n"
                "2️⃣ Copie a URL completa da página\n"
                "3️⃣ Envie novamente\n\n"
                "Exemplo: <code>https://shopee.com.br/product/123/456</code>",
                parse_mode="HTML"
            )
            LOG.warning("❌ Não foi possível expandir link encurtado")
            return
    
    # 🔗 PASSO 2: Resolve links universais da Shopee
    if 'shopee' in url.lower():
        original_url = url
        url = resolve_shopee_universal_link(url)
        if url != original_url:
            LOG.info("✅ URL resolvida com sucesso")
    
    # Envia mensagem de processamento
    processing_msg = await update.message.reply_text(MESSAGES["processing"])
    
    # Verifica se é Shopee Video
    is_shopee_video = 'sv.shopee' in url.lower() or 'share-video' in url.lower()
    
    if is_shopee_video:
        # Para Shopee Video, criamos confirmação simples sem informações detalhadas
        LOG.info("Detectado Shopee Video - confirmação sem extração prévia")
        
        # Cria botões de confirmação
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirmar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        confirm_text = (
            "🎬 <b>Confirmar Download</b>\n\n"
            "🛍️ Vídeo da Shopee\n"
            "⚠️ Informações disponíveis apenas após download\n\n"
            "✅ Deseja prosseguir com o download?"
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
        return
    
    # Obtém informações do vídeo (para não-Shopee)
    try:
        video_info = await get_video_info(url)
        
        if not video_info:
            await processing_msg.edit_text(MESSAGES["invalid_url"])
            return
        
        title = video_info.get("title", "Vídeo")[:100]
        duration = format_duration(video_info.get("duration", 0))
        filesize_bytes = video_info.get("filesize") or video_info.get("filesize_approx", 0)
        filesize = format_filesize(filesize_bytes)
        
        # Verifica se o arquivo excede o limite de 50 MB
        if filesize_bytes and filesize_bytes > MAX_FILE_SIZE:
            await processing_msg.edit_text(MESSAGES["file_too_large"], parse_mode="HTML")
            LOG.info("Vídeo rejeitado por exceder 50 MB: %d bytes", filesize_bytes)
            return
        
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
            duration=duration,
            filesize=filesize
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
    
    # Configuração especial para Shopee
    is_shopee = 'shopee' in url.lower() or 'shope.ee' in url.lower()
    
    # 🔗 CRÍTICO: Resolve universal-links ANTES de tudo!
    if is_shopee and 'universal-link' in url:
        original_url = url
        url = resolve_shopee_universal_link(url)
        LOG.info("🔗 Universal link resolvido: %s", url[:80])
        # Atualiza flag is_shopee após resolver
        is_shopee = 'shopee' in url.lower() or 'shope.ee' in url.lower()
    
    # 🎯 NOVO: Se for Shopee, tenta API primeiro (SEM marca d'água!)
    if is_shopee:
        LOG.info("🛍️ Detectado Shopee - tentando API interna (sem marca d'água)...")
        shopee_video = await asyncio.to_thread(SHOPEE_EXTRACTOR.get_video, url)
        
        if shopee_video and shopee_video.get('url'):
            LOG.info("✅ Vídeo extraído da API Shopee SEM marca d'água!")
            return {
                'url': shopee_video['url'],
                'title': shopee_video.get('title', 'Vídeo da Shopee'),
                'uploader': shopee_video.get('uploader', 'Desconhecido'),
                'ext': 'mp4',
                'from_shopee_api': True,  # Marca que veio da API
            }
        else:
            LOG.warning("⚠️ API Shopee falhou, tentando yt-dlp...")
    
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "no_check_certificate": True,
        "prefer_insecure": True,
    }
    
    if is_shopee:
        # Configurações específicas para Shopee
        ydl_opts.update({
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Referer": "https://shopee.com.br/",
                "Origin": "https://shopee.com.br",
            },
            "socket_timeout": 30,
            "retries": 3,
        })
        LOG.info("🛍️ Configurações especiais para Shopee aplicadas")
    
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            return info
    except Exception as e:
        LOG.error("Erro ao extrair informações com yt-dlp: %s", e)
        
        # Se for Shopee e yt-dlp falhou, tenta extração direta
        if is_shopee:
            LOG.info("🛍️ Tentando extração direta da Shopee como fallback...")
            direct_info = extract_shopee_video_direct(url)
            if direct_info:
                LOG.info("✅ Extração direta bem-sucedida!")
                return direct_info
        
        return None

# ====================================================================
# FUNÇÕES DE INTELIGÊNCIA ARTIFICIAL (GROQ)
# ====================================================================

async def chat_with_ai(message: str, system_prompt: str = None) -> str:
    """
    Envia mensagem para Groq AI e retorna resposta.
    
    Args:
        message: Mensagem do usuário
        system_prompt: Instruções do sistema (opcional)
        
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
        
        # Adiciona mensagem do usuário
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
    Gera resumo inteligente de um vídeo usando IA.
    
    Args:
        video_info: Dicionário com informações do vídeo
        
    Returns:
        str: Resumo do vídeo ou string vazia se IA indisponível
    """
    if not groq_client:
        return ""
    
    try:
        title = video_info.get('title', 'N/A')
        description = video_info.get('description', '')
        
        # Limita descrição para não exceder tokens
        if description and len(description) > 500:
            description = description[:500] + "..."
        
        prompt = f"""Crie um resumo CURTO e OBJETIVO deste vídeo em 3-4 pontos principais.
Use bullets (•) e seja direto.

Título: {title}
Descrição: {description or 'Sem descrição'}

Responda APENAS com o resumo, sem introduções."""
        
        summary = await chat_with_ai(
            prompt,
            system_prompt="Você é um assistente que resume vídeos de forma clara e concisa."
        )
        
        return summary if summary else ""
        
    except Exception as e:
        LOG.error("Erro ao gerar resumo: %s", e)
        return ""


async def analyze_user_intent(message: str) -> dict:
    """
    Analisa a intenção do usuário na mensagem.
    
    Args:
        message: Mensagem do usuário
        
    Returns:
        dict: {'intent': 'download' | 'chat' | 'help', 'confidence': 0.0-1.0}
    """
    # Fallback simples sem IA
    if URL_RE.search(message):
        return {'intent': 'download', 'confidence': 1.0}
    
    if not groq_client:
        return {'intent': 'chat', 'confidence': 0.5}
    
    try:
        prompt = f"""Analise esta mensagem de usuário e identifique a intenção:
"{message}"

Responda APENAS com uma das opções:
- download: se pede para baixar algo ou tem URL
- help: se pede ajuda, instruções ou explicações
- chat: conversa geral

Responda APENAS uma palavra."""
        
        response = await chat_with_ai(
            prompt,
            system_prompt="Você analisa intenções de usuários. Responda apenas: download, help ou chat."
        )
        
        if response:
            intent = response.strip().lower()
            if intent in ['download', 'help', 'chat']:
                return {'intent': intent, 'confidence': 0.9}
        
    except Exception as e:
        LOG.error("Erro ao analisar intenção: %s", e)
    
    return {'intent': 'chat', 'confidence': 0.5}


# ====================================================================
# FUNÇÕES DO MERCADO PAGO
# ====================================================================

async def callback_buy_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para compra de premium via Mercado Pago PIX"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    username = query.from_user.first_name or f"User{user_id}"
    
    LOG.info("🛒 Usuário %d iniciou compra de premium", user_id)
    
    # Verifica se já é premium
    stats = get_user_download_stats(user_id)
    if stats["is_premium"]:
        await query.edit_message_text(
            "💎 <b>Você já é Premium!</b>\n\n"
            "Continue aproveitando seus benefícios ilimitados! 🎉",
            parse_mode="HTML"
        )
        LOG.info("Usuário %d já é premium", user_id)
        return
    
    # Verifica se Mercado Pago está disponível
    if not MERCADOPAGO_AVAILABLE or not MERCADOPAGO_ACCESS_TOKEN:
        await query.edit_message_text(
            "❌ <b>Sistema de Pagamento Indisponível</b>\n\n"
            "O sistema de pagamento está temporariamente indisponível.\n"
            "Por favor, tente novamente mais tarde ou contate o suporte.",
            parse_mode="HTML"
        )
        LOG.error("Tentativa de compra mas Mercado Pago não configurado")
        return
    
    # Mostra mensagem de processamento
    await query.edit_message_text(
        "⏳ <b>Gerando pagamento PIX...</b>\n\nAguarde um momento.",
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
        
        LOG.info("Criando pagamento PIX para usuário %d - Valor: R$ %.2f", user_id, PREMIUM_PRICE)
        
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
        
        LOG.info("✅ Payment criado - ID: %s, Status: %s", payment_id, payment.get("status"))
        
        # Valida estrutura do PIX
        if "point_of_interaction" not in payment:
            LOG.error("Resposta sem point_of_interaction: %s", payment)
            raise Exception("PIX não foi gerado - point_of_interaction ausente")
        
        poi = payment["point_of_interaction"]
        if "transaction_data" not in poi:
            LOG.error("point_of_interaction sem transaction_data: %s", poi)
            raise Exception("PIX não foi gerado - transaction_data ausente")
        
        td = poi["transaction_data"]
        if "qr_code" not in td or "qr_code_base64" not in td:
            LOG.error("transaction_data sem QR codes: %s", td)
            raise Exception("PIX não foi gerado - QR codes ausentes")
        
        # Extrai informações do PIX
        pix_info = {
            "payment_id": payment_id,
            "qr_code": td["qr_code"],
            "qr_code_base64": td["qr_code_base64"],
            "amount": payment["transaction_amount"]
        }
        
        LOG.info("✅ PIX gerado com sucesso - ID: %s", payment_id)
        
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
            "💳 <b>Pagamento PIX Gerado</b>\n\n"
            f"💰 Valor: R$ {pix_info['amount']:.2f}\n"
            f"🆔 ID: <code>{payment_id}</code>\n\n"
            "📱 <b>Como pagar:</b>\n"
            "1️⃣ Abra o app do seu banco\n"
            "2️⃣ Vá em PIX → Ler QR Code\n"
            "3️⃣ Escaneie o código abaixo\n"
            "4️⃣ Confirme o pagamento\n\n"
            "⏱️ <b>Expira em:</b> 30 minutos\n"
            "✅ <b>Ativação automática após confirmação!</b>\n\n"
            "⚡ Seu premium será ativado em até 60 segundos."
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
                
                # Remove arquivo temporário
                os.remove(qr_path)
                qr_sent = True
                LOG.info("✅ QR Code enviado como imagem")
                
            except Exception as e:
                LOG.error("Erro ao enviar QR Code como imagem: %s", e)
        
        # Se enviou imagem, envia código separado; senão envia tudo junto
        if qr_sent:
            # Envia código PIX copia e cola em mensagem separada
            LOG.info("Enviando código PIX copia e cola em mensagem separada")
            await query.message.reply_text(
                "📋 <b>Código PIX Copia e Cola:</b>\n\n"
                "Caso prefira, copie o código abaixo e cole no seu app de pagamento:\n\n"
                f"<code>{pix_info['qr_code']}</code>\n\n"
                "💡 <i>Clique no código acima para copiar automaticamente</i>",
                parse_mode="HTML"
            )
        else:
            # Fallback: envia tudo como texto
            LOG.info("Enviando QR Code como texto (código copia e cola)")
            await query.message.reply_text(
                message_text + f"\n\n📋 <b>Código PIX Copia e Cola:</b>\n<code>{pix_info['qr_code']}</code>",
                parse_mode="HTML"
            )
        
        # Deleta mensagem antiga
        try:
            await query.message.delete()
        except Exception as e:
            LOG.debug("Não foi possível deletar mensagem antiga: %s", e)
        
        # Inicia monitoramento do pagamento
        LOG.info("Iniciando monitoramento do pagamento %s", payment_id)
        asyncio.create_task(monitor_payment_status(user_id, payment_id))
        
        LOG.info("✅ Processo completo - Pagamento %s criado e em monitoramento", payment_id)
        
    except Exception as e:
        LOG.exception("❌ ERRO ao gerar pagamento PIX: %s", e)
        
        # Determina mensagem de erro específica
        error_msg = str(e).lower()
        if "401" in error_msg or "unauthorized" in error_msg:
            error_detail = "Token do Mercado Pago inválido ou expirado."
        elif "point_of_interaction" in error_msg or "qr" in error_msg:
            error_detail = "Erro ao gerar QR Code PIX. Verifique as credenciais."
        elif "mercadopago_access_token" in error_msg:
            error_detail = "Sistema de pagamento não configurado no servidor."
        else:
            error_detail = f"Erro ao processar pagamento."
        
        await query.edit_message_text(
            f"❌ <b>Erro ao Gerar Pagamento</b>\n\n"
            f"{error_detail}\n\n"
            f"Por favor, tente novamente em alguns instantes.\n\n"
            f"Se o erro persistir, entre em contato com o suporte.",
            parse_mode="HTML"
        )


async def monitor_payment_status(user_id: int, payment_id: str):
    """Monitora o status do pagamento em segundo plano"""
    if not MERCADOPAGO_AVAILABLE or not MERCADOPAGO_ACCESS_TOKEN:
        LOG.error("Não é possível monitorar pagamento - Mercado Pago não configurado")
        return
    
    try:
        sdk = mercadopago.SDK(MERCADOPAGO_ACCESS_TOKEN)
        max_attempts = 60  # 30 minutos (30s * 60)
        
        LOG.info("🔍 Monitorando pagamento %s (max %d tentativas)", payment_id, max_attempts)
        
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
                    LOG.info("🎉 Pagamento %s APROVADO!", payment_id)
                    await activate_premium(user_id, payment_id)
                    break
                    
                elif status in ["rejected", "cancelled", "refunded"]:
                    LOG.info("⚠️ Pagamento %s não concluído: %s", payment_id, status)
                    
                    # Notifica usuário
                    try:
                        status_messages = {
                            "rejected": "rejeitado",
                            "cancelled": "cancelado",
                            "refunded": "reembolsado"
                        }
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"⚠️ <b>Pagamento {status_messages.get(status, status)}</b>\n\n"
                                f"ID: <code>{payment_id}</code>\n\n"
                                "Seu pagamento não foi concluído.\n"
                                "Se precisar de ajuda, entre em contato com o suporte."
                            ),
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        LOG.error("Erro ao notificar usuário sobre falha: %s", e)
                    break
                    
            except Exception as e:
                LOG.error("Erro ao verificar status do pagamento %s: %s", payment_id, e)
        
        if attempt >= max_attempts - 1:
            LOG.info("⏰ Timeout de monitoramento para pagamento %s após %d minutos", 
                    payment_id, (max_attempts * 30) // 60)
            
    except Exception as e:
        LOG.exception("Erro crítico no monitoramento do pagamento %s: %s", payment_id, e)


async def activate_premium(user_id: int, payment_id: str):
    """Ativa o plano premium para o usuário"""
    try:
        LOG.info("🔓 Ativando premium para usuário %d - Pagamento: %s", user_id, payment_id)
        
        # Calcula data de expiração
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
        
        LOG.info("✅ Premium ativado no banco de dados (%d linhas atualizadas)", rows_affected)
        
        # Notifica o usuário
        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>Pagamento Confirmado!</b>\n\n"
                f"✅ Plano Premium ativado com sucesso!\n"
                f"🆔 Pagamento: <code>{payment_id}</code>\n"
                f"📅 Válido até: <b>{premium_expires}</b>\n\n"
                "💎 <b>Benefícios liberados:</b>\n"
                "• ♾️ Downloads ilimitados\n"
                "• 🎬 Qualidade máxima (até 1080p)\n"
                "• ⚡ Processamento prioritário\n"
                "• 🎧 Suporte dedicado\n\n"
                "Obrigado pela confiança! 🙏\n\n"
                "Use /status para ver suas informações."
            ),
            parse_mode="HTML"
        )
        
        LOG.info("✅ Usuário %d notificado sobre ativação do premium", user_id)
        
    except Exception as e:
        LOG.exception("❌ ERRO ao ativar premium para usuário %d: %s", user_id, e)
        
        # Tenta notificar sobre o erro
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ <b>Pagamento Recebido</b>\n\n"
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
        # Verifica quantos downloads estão ativos
        active_count = len(ACTIVE_DOWNLOADS)
        
        if active_count >= MAX_CONCURRENT_DOWNLOADS:
            # Mostra posição na fila
            queue_position = active_count - MAX_CONCURRENT_DOWNLOADS + 1
            queue_text = MESSAGES["queue_position"].format(
                position=queue_position,
                active=MAX_CONCURRENT_DOWNLOADS
            )
            await query.edit_message_text(queue_text)
        
        # Remove da lista de pendentes
        del PENDING[token]
        
        # Adiciona à lista de downloads ativos
        ACTIVE_DOWNLOADS[token] = {
            "user_id": pm["user_id"],
            "started_at": time.time()
        }
        
        await query.edit_message_text(MESSAGES["download_started"])
        
        # Incrementa contador de downloads
        increment_download_count(pm["user_id"])
        
        # Inicia download em background
        asyncio.create_task(_process_download(token, pm))
        LOG.info("Download iniciado para usuário %d (Token: %s)", pm["user_id"], token)

async def _process_download(token: str, pm: dict):
    """Processa o download em background"""
    tmpdir = None
    
    # Aguarda na fila (semáforo para controlar 3 downloads simultâneos)
    async with DOWNLOAD_SEMAPHORE:
        try:
            tmpdir = tempfile.mkdtemp(prefix=f"ytbot_")
            LOG.info("Diretório temporário criado: %s", tmpdir)
            
            try:
                await _do_download(token, pm["url"], tmpdir, pm["chat_id"], pm)
            finally:
                # Limpa arquivos temporários e envia mensagem de cleanup
                if tmpdir and os.path.exists(tmpdir):
                    try:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                        cleanup_msg = MESSAGES["cleanup"].format(path=tmpdir)
                        LOG.info(cleanup_msg)
                        
                        # Envia mensagem de cleanup para o usuário
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
    """Executa o download do vídeo"""
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
                
                # Verifica se o tamanho está excedendo o limite durante download
                if total and total > MAX_FILE_SIZE:
                    LOG.warning("Download cancelado: arquivo excede 50 MB (%d bytes)", total)
                    raise Exception(f"Arquivo muito grande: {total} bytes")
                
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
    is_shopee = 'shopee' in url.lower() or 'shope.ee' in url.lower()
    
    ydl_opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "quiet": False,
        "logger": LOG,
        "format": get_format_for_url(url),
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 1,
        "force_ipv4": True,
        "socket_timeout": 30,
        "http_chunk_size": 1048576,
        "retries": 20,
        "fragment_retries": 20,
        "no_check_certificate": True,
        "prefer_insecure": True,
        # Configurações para evitar cortes e garantir qualidade
        "postprocessors": [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        "keepvideo": False,  # Remove arquivos temporários
        "prefer_ffmpeg": True,  # Usa FFmpeg para merge (evita cortes)
    }
    
    # Configurações específicas para Shopee
    if is_shopee:
        LOG.info("🛍️ Aplicando configurações otimizadas para Shopee")
        ydl_opts.update({
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://shopee.com.br/",
                "Origin": "https://shopee.com.br",
                "Sec-Fetch-Dest": "video",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "cross-site",
            },
            "extractor_args": {
                "shopee": {
                    "api_ver": "v4"
                }
            },
            # Força download direto sem fragmentação
            "noprogress": False,
            "keep_fragments": False,
        })
    
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
            
            # Verifica se o arquivo excede 50 MB (EXCETO Shopee - sem limite)
            is_shopee = 'shopee' in pm["url"].lower()
            
            if not is_shopee and tamanho > MAX_FILE_SIZE:
                LOG.error("Arquivo muito grande após download: %d bytes", tamanho)
                await _notify_error(pm, "error_file_large")
                return
            
            if is_shopee:
                LOG.info("📦 Vídeo Shopee: %.2f MB (sem limite de tamanho)", tamanho / (1024 * 1024))
            
            # 🎬 REMOVE MARCA D'ÁGUA SE FOR SHOPEE
            if is_shopee:
                LOG.info("🛍️ Vídeo da Shopee detectado - removendo marca d'água...")
                
                try:
                    # Atualiza mensagem
                    await application.bot.edit_message_text(
                        text="✨ Removendo marca d'água...",
                        chat_id=pm["chat_id"],
                        message_id=pm["message_id"]
                    )
                except:
                    pass
                
                # Remove marca d'água - POSIÇÃO CORRETA: MEIO DIREITO ✅
                path = WATERMARK_REMOVER.remove(path, position='middle_right')
                
                # Se falhar, tenta outras posições
                if os.path.exists(path) and 'temp' not in path:
                    # Tenta posições alternativas
                    LOG.info("   Tentando posições alternativas...")
                    for pos in ['middle_right_high', 'middle_right_low', 'middle_center', 'bottom_right']:
                        try:
                            path = WATERMARK_REMOVER.remove(path, position=pos)
                            break
                        except:
                            continue
            
            # Envia o vídeo
            with open(path, "rb") as fh:
                caption = "🛍️ Shopee Video" if 'shopee' in pm["url"].lower() else None
                if caption and WATERMARK_REMOVER.is_available():
                    caption += "\n✨ Marca d'água removida"
                
                await application.bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    caption=caption
                )
                    
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
application.add_handler(CommandHandler("ai", ai_cmd))  # ← Novo comando
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
    return "🤖 Bot de Download Ativo"

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
    """Endpoint para receber notificações de pagamento PIX do Mercado Pago"""
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
                        LOG.warning("Formato de referência inválido: %s", reference)
                else:
                    LOG.warning("Referência externa ausente ou inválida: %s", reference)

        return "ok", 200

    except Exception as e:
        LOG.exception("Erro no webhook PIX: %s", e)
        return "erro", 500

# ======================
# ALERTAS DISCORD (Render)
# ======================

from flask import Flask, request
from datetime import datetime, timezone, timedelta
import os

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1435259548255518813/JA9d0SJD8n8SWtnjWMLJUr5kA9jLdQyVn5fOi5lYWULKYB2Nv94rD37wF_d8RiGGt5-Z"  # Substitua pela URL do Discord

@app.route("/render-webhook", methods=["GET", "POST"])
def render_webhook():
    if request.method == "GET":
        return "Webhook ativo", 200

    payload = request.get_json(silent=True) or {}
    
    # Padroniza tipo do evento para minúsculas
    event_type = (payload.get("type") or "evento_desconhecido").lower()
    timestamp_utc = payload.get("timestamp")
    data = payload.get("data", {})

    service_name = data.get("serviceName", "Serviço não informado")
    status = data.get("status")

    # === 🔹 FILTRO DE EVENTOS RELEVANTES ===
    eventos_relevantes = [
        "deploy_started",
        "deploy_ended",
        "service_unhealthy",
        "server_unhealthy",
        "service_started",
        "server_started"
    ]
    
    if event_type not in eventos_relevantes:
        # Apenas log temporário para verificação
        print(f"Ignorado: {event_type}")
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
        return {"error": "Webhook do Discord não configurado"}, 500

    # === 🔹 Envia mensagem pro Discord ===
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
        if response.status_code != 204:
            print(f"Erro ao enviar alerta: {response.text}")
        else:
            print(f"Mensagem enviada: {message}")
        return {"discord_status": response.status_code}, 200
    except Exception as e:
        print(f"Erro ao enviar alerta: {e}")
        return {"error": str(e)}, 500

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
        await query.edit_message_text(f"❌ Falha interna: {e}")
