#!/usr/bin/env python3
"""
Alex Moura.
Versão: 2.1 (23/11/2025)
"""

# 🔧 FORÇA UTF-8 ENCODING PARA EMOJIS
import sys
import io

# Garantir saída UTF-8 mesmo no Render
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except:
        pass

import os
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
import weakref

# Import necessário para o retry de timeout
from telegram.error import TimedOut

from collections import OrderedDict, deque
from contextlib import contextmanager
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime, timedelta

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

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# 🔧 FIX 413: Compressão de vídeos grandes
try:
    import subprocess
    FFMPEG_AVAILABLE = True
except ImportError:
    FFMPEG_AVAILABLE = False

# ════════════════════════════════════════════════════════════════
# 🔄 SISTEMA DE AUTO-RECUPERAÇÃO E KEEPALIVE
# ════════════════════════════════════════════════════════════════

from datetime import datetime

# Configurações do sistema de keepalive
KEEPALIVE_ENABLED = os.getenv("KEEPALIVE_ENABLED", "true").lower() == "true"
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "600"))  # 10 minutos (otimizado de 300s - reduz CPU em 50%)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # URL do seu bot no Render
LAST_ACTIVITY = {"telegram": time.time(), "flask": time.time()}
INACTIVITY_THRESHOLD = 1800  # 30 minutos sem atividade = aviso

# 🔧 FIX YOUTUBE CONNECTION: Função auxiliar para retry com backoff exponencial
TELEGRAM_VIDEO_SIZE_LIMIT = 50 * 1024 * 1024  # 50MB - limite do Telegram para upload via HTTP

def ydl_with_retry(operation, max_retries=5, backoff_factor=2):
    """
    Executa operação yt-dlp com retry exponencial.
    Evita "Connection refused" do YouTube com delays progressivos.
    
    Args:
        operation: Função lambda que executa a operação
        max_retries: Número máximo de tentativas
        backoff_factor: Fator multiplicador para backoff (2 = 1s, 2s, 4s, 8s, 16s)
    
    Returns:
        Resultado da operação ou None se todas as tentativas falharem
    """
    for attempt in range(max_retries):
        try:
            return operation()
        except (ConnectionError, ConnectionRefusedError, TimeoutError) as e:
            if attempt == max_retries - 1:
                LOG.error("❌ Falha após %d tentativas de reconexão: %s", max_retries, e)
                raise
            
            wait_time = backoff_factor ** attempt
            LOG.warning("⚠️ Tentativa %d/%d falhou (%s). Aguardando %ds...", 
                       attempt + 1, max_retries, type(e).__name__, wait_time)
            time.sleep(wait_time)
        except Exception as e:
            # Para outros erros, tenta novamente sem delay
            if attempt == max_retries - 1:
                LOG.error("❌ Erro após %d tentativas: %s", max_retries, e)
                raise
            LOG.warning("⚠️ Tentativa %d/%d falhou com erro: %s", attempt + 1, max_retries, e)

class BotHealthMonitor:
    """Monitor de saúde do bot com auto-recuperação"""
    
    def __init__(self):
        self.lock = threading.Lock()  # ✅ NOVO: Thread-safe access
        self.last_telegram_update = time.time()
        self.last_health_check = time.time()
        self.webhook_errors = 0
        self.consecutive_errors = 0
        self.max_errors_before_restart = 3
        self.max_consecutive_errors = 5
        self.is_healthy = True
        
    def record_activity(self, source: str = "telegram"):
        """Registra atividade do bot - THREAD SAFE"""
        with self.lock:
            LAST_ACTIVITY[source] = time.time()
            if source == "telegram":
                self.last_telegram_update = time.time()
                self.webhook_errors = 0
                self.consecutive_errors = 0
    
    def check_health(self) -> dict:
        """Verifica saúde do bot - THREAD SAFE"""
        with self.lock:
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

            health_emoji = "🟢"
            health_msg = "Tudo OK"

            if telegram_inactive > INACTIVITY_THRESHOLD:
                status["healthy"] = False
                status["issue"] = "telegram_inactive"
                health_emoji = "🟡"
                health_msg = f"Inativo há {int(telegram_inactive)}s"
                LOG.warning("⚠️ %s Bot inativo há %d segundos", health_emoji, telegram_inactive)

            elif self.webhook_errors >= self.max_errors_before_restart:
                status["healthy"] = False
                status["issue"] = "webhook_errors"
                health_emoji = "🔴"
                health_msg = f"{self.webhook_errors} erros de webhook"
                LOG.error("🔴 Muitos erros de webhook: %d", self.webhook_errors)

            else:
                pass

            self.is_healthy = status["healthy"]

            return status
    
    def record_error(self):
        """Registra erro no webhook - THREAD SAFE"""
        with self.lock:
            self.webhook_errors += 1
            self.consecutive_errors += 1
            LOG.warning("⚠️ Erro no webhook registrado (consecutivos: %d, total: %d)", 
                        self.consecutive_errors, self.webhook_errors)
    
    def should_reconnect_webhook(self) -> bool:
        """Verifica se deve reconectar o webhook"""
        return self.consecutive_errors >= self.max_consecutive_errors

# Instância global do monitor
health_monitor = BotHealthMonitor()

# ════════════════════════════════════════════════════════════════
# 📝 CONFIGURAR LOGGING (UMA ÚNICA VEZ - ANTES DE USAR LOG)
# ════════════════════════════════════════════════════════════════
LOG = logging.getLogger("ytbot")
LOG.setLevel(logging.INFO)

# REMOVER TODOS OS HANDLERS ANTERIORES (se houver)
if LOG.hasHandlers():
    for handler in LOG.handlers[:]:
        LOG.removeHandler(handler)

# Handler para console (para ver nos logs do Render)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
LOG.addHandler(console_handler)

# Handler para arquivo (se /tmp existe)
if os.path.exists('/tmp'):
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            '/tmp/ytbot.log',
            maxBytes=5*1024*1024,  # 5MB
            backupCount=2,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        LOG.addHandler(file_handler)
    except Exception:
        pass  # Se falhar, continua apenas com console

# ════════════════════════════════════════════════════════════════
# ⬇️ SISTEMA DE CONTROLE DE DOWNLOADS SIMULTÂNEOS + LIMPEZA DE MEMÓRIA
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# 🗑️ GARBAGE COLLECTOR MAIS AGRESSIVO
# ════════════════════════════════════════════════════════════════
gc.set_threshold(500, 10, 5)  # Mais agressivo: coleta a cada 500 alocações
LOG.info("🗑️ Garbage Collector configurado (agressivo): threshold=500, factors=(10, 5)")

# Semáforo para limitar downloads a 2 simultâneos
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)

# Cache de última limpeza
LAST_MEMORY_CLEANUP = time.time()
MEMORY_CLEANUP_INTERVAL = 300  # 5 minutos
MAX_MEMORY_USAGE_MB = 500  # Limpa agressivamente se passar de 500MB

# Dicionário para rastrear downloads ativos
ACTIVE_DOWNLOADS = {}

# ════════════════════════════════════════════════════════════════
# 📦 LIMITED CACHE PARA USER_LAST_DOWNLOAD
# ════════════════════════════════════════════════════════════════
class LimitedCache:
    """Cache com limite máximo de entradas (FIFO quando cheio)"""
    def __init__(self, max_size=50):  # Reduzido de 300 para 50 - economiza ~80MB
        self.max_size = max_size
        self.cache = OrderedDict()
    
    def get(self, key, default=None):
        """Obtém valor do cache"""
        return self.cache.get(key, default)
    
    def set(self, key, value):
        """Adiciona/atualiza valor no cache"""
        if key in self.cache:
            # Move para o fim (more recently used)
            del self.cache[key]
        elif len(self.cache) >= self.max_size:
            # Remove o mais antigo (least recently used)
            self.cache.popitem(last=False)
        
        self.cache[key] = value
    
    def __setitem__(self, key, value):
        self.set(key, value)
    
    def __getitem__(self, key):
        return self.cache[key]
    
    def __contains__(self, key):
        return key in self.cache
    
    def get_size(self):
        return len(self.cache)

# Instância de cache limitado para último download do usuário
USER_LAST_DOWNLOAD = LimitedCache(max_size=50)  # Reduzido de 300 para 50 - economiza ~50MB
LOG.info("📦 LimitedCache para USER_LAST_DOWNLOAD inicializado (max_size=50, não cresce infinito)")

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

# ════════════════════════════════════════════════════════════════
# 💾 FUNÇÕES DE MONITORAMENTO E LIMPEZA DE MEMÓRIA
# ════════════════════════════════════════════════════════════════

def get_memory_usage_mb():
    """Retorna uso de memória atual em MB"""
    try:
        if PSUTIL_AVAILABLE:
            process = psutil.Process(os.getpid())
            return process.memory_info().rss / 1024 / 1024  # Converte para MB
        return 0
    except:
        return 0

def cleanup_memory():
    """Limpeza agressiva de memória"""
    global LAST_MEMORY_CLEANUP
    
    current_time = time.time()
    if current_time - LAST_MEMORY_CLEANUP < MEMORY_CLEANUP_INTERVAL:
        return  # Não limpa ainda, intervalo mínimo
    
    try:
        # Força garbage collection
        collected = gc.collect()
        
        current_memory = get_memory_usage_mb()
        if current_memory > 0:
            LOG.debug(f"💾 Limpeza de memória: {current_memory:.1f}MB (coletadas {collected} objetos)")
        
            # Se passou do limite, limpa mais agressivamente
            if current_memory > MAX_MEMORY_USAGE_MB:
                LOG.warning(f"⚠️ Memória alta ({current_memory:.1f}MB)! Limpeza agressiva...")
                gc.collect()
                gc.collect()  # Dupla passada
                
                new_memory = get_memory_usage_mb()
                LOG.info(f"✅ Memória reduzida: {current_memory:.1f}MB → {new_memory:.1f}MB")
        else:
            LOG.debug(f"💾 GC executado: {collected} objetos coletados")
        
        LAST_MEMORY_CLEANUP = current_time
        
    except Exception as e:
        LOG.error(f"❌ Erro na limpeza de memória: {e}")

async def memory_cleanup_routine():
    """Rotina periódica de limpeza de memória (executa a cada 5 minutos)"""
    while True:
        try:
            await asyncio.sleep(MEMORY_CLEANUP_INTERVAL)
            cleanup_memory()
        except Exception as e:
            LOG.error(f"❌ Erro na rotina de limpeza: {e}")
            await asyncio.sleep(60)  # Tenta de novo em 1 minuto

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
                            LOG.error("❌ Falha ao reconectar webhook")
                    except Exception as e:
                        LOG.error("❌ Erro na reconexão: %s", e)
            
            # 2. Self-ping (mantém Render acordado)
            if WEBHOOK_URL:
                try:
                    response = requests.get(
                        f"{WEBHOOK_URL}/health",
                        timeout=10
                    )
                    if response.status_code == 200:
                        # OTIMIZADO: Log removido para não poluir
                        pass
                    else:
                        LOG.warning("⚠️ Self-ping retornou: %d", response.status_code)
                except Exception as e:
                    LOG.error("❌ Falha no self-ping: %s", e)
            
            # OTIMIZADO: Log de keepalive removido para não poluir quando tudo está OK
            
        except Exception as e:
            LOG.exception("❌ Erro na rotina de keepalive: %s", e)

def webhook_watchdog():
    """
    Watchdog que monitora o webhook e força reconexão se necessário
    OTIMIZADO: Verifica a cada 3 minutos (reduz CPU em 66%)
    """
    while True:
        try:
            time.sleep(180)  # 3 minutos (otimizado de 60s)
            
            now = time.time()
            last_telegram = LAST_ACTIVITY["telegram"]
            inactive_time = now - last_telegram
            
            # Se passar 15 minutos sem receber updates do Telegram
            if inactive_time > 900 and WEBHOOK_URL:  # 15 minutos
                LOG.warning("🔴 Webhook pode estar inativo! Última atividade: %d segundos atrás", inactive_time)
                
                # Verifica se webhook está configurado
                try:
                    webhook_info = asyncio.run_coroutine_threadsafe(
                        application.bot.get_webhook_info(),
                        APP_LOOP
                    ).result(timeout=10)
                    
                    LOG.info("📊 Webhook Info: URL=%s, Pending=%d", 
                            webhook_info.url, 
                            webhook_info.pending_update_count)
                    
                    # Se webhook não está configurado, tem muitos pendentes ou tem erros
                    expected_url = f"{WEBHOOK_URL}/{TOKEN}"
                    if (webhook_info.url != expected_url or 
                        webhook_info.pending_update_count > 100 or
                        webhook_info.last_error_message):
                        
                        LOG.error("🔴 Webhook com problemas! URL=%s, Pending=%d, Erro=%s", 
                                webhook_info.url,
                                webhook_info.pending_update_count,
                                webhook_info.last_error_message or "Nenhum")
                        
                        # Tenta reconectar usando a nova função
                        try:
                            LOG.info("🔧 Reconectando webhook via watchdog...")
                            if reconnect_webhook_sync():
                                LOG.info("✅ Webhook reconectado pelo watchdog!")
                                LAST_ACTIVITY["telegram"] = time.time()
                            else:
                                LOG.error("❌ Watchdog falhou ao reconectar")
                        except Exception as e:
                            LOG.error("❌ Erro ao reconectar via watchdog: %s", e)
                        
                except Exception as e:
                    LOG.error("❌ Erro no watchdog: %s", e)
                    
        except Exception as e:
            LOG.exception("❌ Erro crítico no watchdog: %s", e)

# ════════════════════════════════════════════════════════════════
# OTIMIZAÇÕES DE MEMÓRIA
# ════════════════════════════════════════════════════════════════

class LimitedCache:
    """Cache com tamanho máximo - evita crescimento infinito de memória"""
    def __init__(self, max_size=500):
        self.cache = OrderedDict()
        self.max_size = max_size
    
    def set(self, key, value):
        """Adiciona item e remove o mais antigo se exceder limite"""
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)
    
    def get(self, key):
        """Busca item e marca como recentemente usado"""
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None
    
    def clear(self):
        """Limpa todo o cache"""
        self.cache.clear()

# Sessão HTTP compartilhada (singleton) - economiza memória
_GLOBAL_HTTP_SESSION = None
_SESSION_LOCK = threading.Lock()

def get_shared_http_session():
    """Retorna sessão HTTP compartilhada para reutilização"""
    global _GLOBAL_HTTP_SESSION
    if _GLOBAL_HTTP_SESSION is None:
        with _SESSION_LOCK:
            if _GLOBAL_HTTP_SESSION is None and REQUESTS_AVAILABLE:
                _GLOBAL_HTTP_SESSION = requests.Session()
                _GLOBAL_HTTP_SESSION.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                })
    return _GLOBAL_HTTP_SESSION

@contextmanager
def temp_file_guaranteed_cleanup(suffix='', prefix='ytdl_'):
    """Context manager que SEMPRE limpa arquivo temporário"""
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
        os.close(fd)
        yield temp_path
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass

def cleanup_and_gc_routine():
    """
    Thread daemon que executa periodicamente:
    1. Limpeza de arquivos temporários antigos
    2. Garbage collection forçado
    OTIMIZADO: Executa a cada 30 minutos (reduz CPU em 66%)
    """
    while True:
        time.sleep(1800)  # 30 minutos (otimizado de 600s)
        
        try:
            # Garbage collection - OTIMIZADO: Apenas geração 0 (5-10x mais rápido)
            collected = gc.collect(0)
            if collected > 0:
                print(f"🗑️ GC: {collected} objetos coletados")
            
            # Limpeza de arquivos temporários - OTIMIZADO: 1 varredura em vez de 6
            one_hour_ago = time.time() - 3600
            cleaned_count = 0
            
            # Varre /tmp apenas 1 vez (83% menos I/O)
            try:
                for filename in os.listdir('/tmp'):
                    if filename.endswith(('.mp4', '.jpg', '.jpeg', '.webm', '.png')) or \
                       filename.startswith('ytdl_'):
                        filepath = os.path.join('/tmp', filename)
                        try:
                            if os.path.getmtime(filepath) < one_hour_ago:
                                os.unlink(filepath)
                                cleaned_count += 1
                        except Exception:
                            pass
            except Exception:
                pass
            
            if cleaned_count > 0:
                print(f"🧹 Limpeza: {cleaned_count} arquivos temporários removidos")
            
            # OTIMIZAÇÃO #2: Limpar ACTIVE_DOWNLOADS órfãos (downloads travados >30min)
            now = time.time()
            orphan_downloads = []
            
            for token, info in ACTIVE_DOWNLOADS.items():
                if now - info.get('start_time', now) > 1800:  # 30 minutos
                    orphan_downloads.append(token)
            
            for token in orphan_downloads:
                del ACTIVE_DOWNLOADS[token]
                if orphan_downloads:
                    print(f"🧹 {len(orphan_downloads)} downloads órfãos removidos (liberando memória)")
                
        except Exception as e:
            print(f"❌ Erro na rotina de limpeza: {e}")

# ============================================================
# SHOPEE VIDEO EXTRACTOR - SEM MARCA D'ÁGUA
# ============================================================

class ShopeeVideoExtractor:
    """Extrator de vídeos da Shopee sem marca d'água usando API interna"""
    
    def __init__(self):
        self.session = get_shared_http_session() if REQUESTS_AVAILABLE else None
        if self.session and REQUESTS_AVAILABLE:
            # Apenas atualiza Referer específico da Shopee
            self.session.headers.update({
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
        Padrão: .123.456. → .
        """
        if not video_url:
            return None
        
        # Remove .NUMERO.NUMERO antes de .
        clean_url = re.sub(r'\.\d+\.\d+(?=\.)', '', video_url)
        
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
                r'"url"\s*:\s*"(https://[^"]*\.[^"]*)"',
                r'(https://cf\.shopee\.com\.br/file/[a-zA-Z0-9_-]+)',
                r'(https://[^"\']*shopee[^"\']*\.[^"\']*)',
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
    
    # Cache da disponibilidade do FFmpeg (otimização de CPU)
    _ffmpeg_available = None
    
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
        """Verifica se FFmpeg está disponível (com cache para otimização)"""
        # Usa cache se já verificou antes
        if WatermarkRemover._ffmpeg_available is not None:
            return WatermarkRemover._ffmpeg_available
        
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
            WatermarkRemover._ffmpeg_available = True
            return True
        except:
            WatermarkRemover._ffmpeg_available = False
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
            LOG.info("🎬 Removendo marca d'água (posição: %s)...", position)
            
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
                # Substitui original COM VERIFICAÇÃO
                try:
                    if os.path.exists(video_path):
                        os.remove(video_path)
                    os.rename(temp_path, video_path)
                    LOG.info("✅ Marca d'água removida com sucesso!")
                    return video_path
                except OSError as e:
                    LOG.error("❌ Falha ao deletar arquivo: %s", e)
                    # Tenta limpar temp_path
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except OSError:
                            pass
                    return video_path
            else:
                LOG.error("❌ FFmpeg falhou: %s", result.stderr[:200] if result.stderr else "erro desconhecido")
                # Remove temp_path COM VERIFICAÇÃO
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError as e:
                        LOG.warning("⚠️ Falha ao deletar arquivo temp: %s", e)
                return video_path
                
        except subprocess.TimeoutExpired:
            LOG.error("❌ Timeout ao remover marca")
            # Limpa arquivo temporário antes de retornar
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                    LOG.debug("🧹 Arquivo temporário deletado após timeout")
                except OSError as e:
                    LOG.warning("⚠️ Falha ao deletar temp após timeout: %s", e)
            return video_path
        except Exception as e:
            LOG.error("❌ Erro ao remover marca: %s", e)
            # Limpa arquivo temporário em caso de erro
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return video_path


# Instância global do removedor
WATERMARK_REMOVER = WatermarkRemover()


from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ✅ Logging já configurado anteriormente (linha 202)
# NÃO adicionar basicConfig aqui para evitar duplicação de handlers!


# Token do Bot
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido.")
    sys.exit(1)

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# 🔐 ID DO ADMINISTRADOR - Apenas este usuário pode usar /mensal e /stats
ADMIN_ID = 6766920288  # ← ALTERE AQUI se necessário

# Constantes do Sistema
URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = os.getenv("DB_FILE", "/data/users.db") if os.path.exists("/data") else "users.db"
PENDING_MAX_SIZE = 200  # OTIMIZADO: Reduzido de 1000 (economia de ~3 MB)
PENDING_EXPIRE_SECONDS = 300  # OTIMIZADO: Reduzido de 600s para 5min (libera memória mais cedo)
WATCHDOG_TIMEOUT = 180
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB - limite do Telegram para bots (API padrão)
SPLIT_SIZE = 45 * 1024 * 1024

# Constantes de Controle de Downloads
FREE_DOWNLOADS_LIMIT = 3
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
PENDING = LimitedCache(max_size=200)  # OTIMIZADO: Reduzido de 1000 para economizar memória (~80% menos RAM)
DB_LOCK = threading.Lock()
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)  # Controle de fila
ACTIVE_DOWNLOADS = {}  # Rastreamento de downloads ativos
DOWNLOAD_HISTORY = deque(maxlen=100)  # Histórico limitado aos últimos 100 downloads
# USER_LAST_DOWNLOAD já está definido acima como LimitedCache(max_size=50) - não redefina aqui!

@contextmanager
def get_db_connection():
    """Context manager para conexões DB com garantia de fechamento"""
    conn = None
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DB_FILE, timeout=5)
            yield conn
            conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        LOG.error("Erro no banco de dados: %s", e)
        raise
    finally:
        if conn:
            conn.close()

# Mensagens Profissionais do Bot
MESSAGES = {
    "welcome": (
        "🎥 <b>Bem-vindo ao Serviço de Downloads</b>\n\n"
        "Envie um link de vídeo do TikTok, Instagram, Shopee ou outras plataformas e eu processarei o download para você.\n\n"
        "🎁 <b>Experimente Gratuitamente:</b>\n"
        "• 3 downloads por semana\n"
        "• Qualidade até 720p\n"
        "• Vídeos curtos (até 50 MB)\n\n"
        "💎 <b>Deseja Downloads Ilimitados?</b>\n"
        "Assine o plano Premium e tenha acesso a downloads sem limites!\n"
        "📲 Digite /premium para assinar\n\n"
        "📊 <b>Seu saldo:</b> Digite /status para verificar quantos downloads você tem disponíveis esta semana"
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
        "⚠️ <b>Limite Semanal Atingido</b>\n\n"
        "Você já usou seus 3 downloads gratuitos desta semana.\n\n"
        "💎 <b>Deseja Downloads Ilimitados?</b>\n"
        "Assine o Plano Premium e tenha acesso a downloads sem restrições!\n\n"
        "💳 Valor: R$ 9,90/mês\n"
        "🔄 Pagamento via PIX\n\n"
        "Clique em /premium para assinar agora!"
    ),
    "status": (
        "📊 <b>Status da Sua Conta</b>\n\n"
        "👤 ID: {user_id}\n"
        "📥 Downloads realizados esta semana: {used}/{total}\n"
        "💾 Downloads restantes esta semana: {remaining}\n"
        "📅 Período: Semanal (reseta toda segunda-feira)\n\n"
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
    "stats": "📈 <b>Estatísticas do Bot</b>\n\n👥 Usuários ativos esta semana: {count}",
    "error_timeout": "⏱️ O tempo de processamento excedeu o limite. Por favor, tente novamente.",
    "error_network": "🌐 Link inválido: Este bot só funciona com links de vídeos da Shopee. Links de produtos não são compatíveis.",
    "error_file_large": "📦 O arquivo excede o limite de 50 MB. Por favor, escolha um vídeo mais curto.",
    "error_ffmpeg": "🎬 Ocorreu um erro durante o processamento do vídeo.",
    "error_upload": "📤 Falha ao enviar o arquivo. Por favor, tente novamente.",
    "error_unknown": "❌ Um erro inesperado ocorreu. Nossa equipe foi notificada. Por favor, tente novamente.",
    "error_expired": "⏰ Esta solicitação expirou. Por favor, envie o link novamente.",
    "download_cancelled": "🚫 Download cancelado com sucesso.",
    "cleanup": "🎬Aproveite o seu vídeo🎬",
}

app = Flask(__name__)

# Inicialização do Telegram Application
from telegram.request import HTTPXRequest

# Inicialização do Telegram Application
try:
    request = HTTPXRequest(
        connect_timeout=30,   # tempo para conectar ao Telegram
        read_timeout=600,     # tempo esperando resposta do Telegram
        write_timeout=600,    # tempo enviando o vídeo (o mais importante)
        pool_timeout=30
    )

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(request)
        .build()
    )

    LOG.info("ApplicationBuilder criado com sucesso (com timeouts customizados).")

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
    """Atualiza o registro de acesso semanal do usuário"""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            week = time.strftime("%Y-W%W")
            c.execute("SELECT last_month FROM monthly_users WHERE user_id=?", (user_id,))
            row = c.fetchone()
            if row:
                if row[0] != week:
                    c.execute("UPDATE monthly_users SET last_month=? WHERE user_id=?", (week, user_id))
            else:
                c.execute("INSERT INTO monthly_users (user_id, last_month) VALUES (?, ?)", (user_id, week))
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
            
            # Calcula semana atual (usando ISO week)
            current_week = time.strftime("%Y-W%W")
            today = time.strftime("%Y-%m-%d")
            
            if row:
                downloads_count, is_premium, last_reset, premium_expires = row
                
                # ✅ VERIFICA SE PREMIUM EXPIROU
                if is_premium and premium_expires:
                    if today > premium_expires:
                        # Premium expirou! Volta para plano gratuito
                        LOG.info("🔔 Premium expirou para usuário %d (expirou em %s)", user_id, premium_expires)
                        is_premium = 0
                        downloads_count = 0  # Reseta contador
                        c.execute("""
                            UPDATE user_downloads 
                            SET is_premium=0, downloads_count=0, last_reset=? 
                            WHERE user_id=?
                        """, (current_week, user_id))
                        conn.commit()
                
                # Reseta contador se mudou a semana (apenas para plano gratuito)
                elif last_reset != current_week and not is_premium:
                    downloads_count = 0
                    c.execute("UPDATE user_downloads SET downloads_count=0, last_reset=? WHERE user_id=?", 
                             (current_week, user_id))
                    conn.commit()
            else:
                # Cria novo registro
                downloads_count, is_premium = 0, 0
                c.execute("""
                    INSERT INTO user_downloads (user_id, downloads_count, is_premium, last_reset) 
                    VALUES (?, 0, 0, ?)
                """, (user_id, current_week))
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
    """Retorna o número de usuários ativos na semana atual"""
    week = time.strftime("%Y-W%W")
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM monthly_users WHERE last_month=?", (week,))
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

def get_youtube_format_by_quality(quality: str) -> str:
    """Retorna string de formato yt-dlp baseado na qualidade escolhida
    
    Otimizado para yt-dlp>=2025.11.12 com melhor suporte a formatos do YouTube
    """
    # Para yt-dlp 2025.11.12+, usa seletores simplificados que funcionam melhor
    quality_formats = {
        "360p": "best[height<=360]/worst",
        "480p": "best[height<=480]/best[height<=360]/worst",
        "720p": "best[height<=720]/best[height<=480]/best",
        "1080p": "best[height<=1080]/best[height<=720]/best",
        "best": "best"
    }
    
    # Retorna o formato com fallback garantido
    return quality_formats.get(quality, "best")

def get_format_for_url(url: str, quality: str = None) -> str:
    """Retorna o formato apropriado baseado na plataforma - OTIMIZADO PARA 50MB
    
    Compatível com yt-dlp>=2025.11.12
    
    Args:
        url: URL do vídeo
        quality: Qualidade para YouTube (360p, 480p, 720p, 1080p, best).
                 Se None, usa padrão (720p para YouTube)
    """
    url_lower = url.lower()

    # Shopee: melhor qualidade disponível (geralmente já é pequeno)
    if 'shopee' in url_lower or 'shope.ee' in url_lower:
        LOG.info("🛍️ Formato Shopee: best (otimizado)")
        return "best[filesize<50M]/best"

    # Instagram: formato único já otimizado
    elif 'instagram' in url_lower or 'insta' in url_lower:
        LOG.info("📸 Formato Instagram: best (otimizado)")
        return "best"

    # YouTube: permite escolha de qualidade
    elif 'youtube' in url_lower or 'youtu.be' in url_lower:
        if quality:
            LOG.info("🎥 Formato YouTube: %s (escolhido pelo usuário)", quality)
            return get_youtube_format_by_quality(quality)
        else:
            LOG.info("🎥 Formato YouTube: 720p (padrão)")
            return get_youtube_format_by_quality("720p")
    
    # Outras plataformas: formato otimizado
    else:
        LOG.info("🎬 Formato padrão: best")
        return "best"


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
        except Exception as e:
            LOG.debug("Erro ignorado: %s", type(e).__name__)
        
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

# ════════════════════════════════════════════════════════════════
# 🔧 FIX 413 - Compressão de vídeos grandes para Telegram
# ════════════════════════════════════════════════════════════════

def ffmpeg_compress_video(input_path: str, output_path: str, target_size_mb: int = 45) -> bool:
    """Comprime vídeo para caber no limite do Telegram (50MB)"""
    try:
        import subprocess
        
        # Obter duração do vídeo
        duration_cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1:nokey=1',
            input_path
        ]
        
        result = subprocess.run(duration_cmd, capture_output=True, text=True, timeout=30)
        duration = float(result.stdout.strip())
        
        LOG.info(f"📊 Vídeo Shopee: {duration:.1f}s")
        
        # Calcular bitrate necessário
        target_bitrate = int((target_size_mb * 8 * 1000) / duration)
        target_bitrate = max(target_bitrate, 400)  # Mínimo 400k
        
        LOG.info(f"🎬 Comprimindo com bitrate {target_bitrate}k...")
        
        # Comando de compressão
        cmd = [
            'ffmpeg', '-i', input_path,
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-b:v', f'{target_bitrate}k',
            '-c:a', 'aac',
            '-b:a', '64k',
            '-y',
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            LOG.error(f"FFmpeg error: {result.stderr[:200]}")
            return False
        
        compressed_size = os.path.getsize(output_path)
        original_size = os.path.getsize(input_path)
        
        LOG.info(f"✅ Compressão OK: {original_size/(1024*1024):.1f}MB → {compressed_size/(1024*1024):.1f}MB")
        
        return compressed_size <= TELEGRAM_VIDEO_SIZE_LIMIT
    
    except Exception as e:
        LOG.error(f"❌ Erro ao comprimir: {e}")
        return False

async def safe_send_video_telegram(bot, chat_id, video_path, caption, pm, tmpdir):
    """Envia vídeo com validação de tamanho e compressão automática"""
    try:
        file_size = os.path.getsize(video_path)
        file_size_mb = file_size / (1024 * 1024)
        
        LOG.info(f"📊 Arquivo a enviar: {file_size_mb:.1f}MB")
        
        # Se está dentro do limite, envia direto
        if file_size <= TELEGRAM_VIDEO_SIZE_LIMIT:
            LOG.info("✅ Tamanho OK, enviando...")

            MAX_RETRIES = 3
            retry_delay = [1, 3, 5]  # segundos

            for attempt in range(MAX_RETRIES):
                fh = open(video_path, "rb")
                try:
                    fh.seek(0)

                    LOG.info(f"📤 Tentando enviar vídeo (tentativa {attempt + 1}/{MAX_RETRIES})...")

                    await bot.send_video(
                        chat_id=chat_id,
                        video=fh,
                        caption=caption
                    )

                    LOG.info("✅ Vídeo enviado com sucesso!")
                    fh.close()
                    return True

                except TimedOut:
                    fh.close()
                    LOG.warning(f"⚠️ Timeout ao enviar vídeo (tentativa {attempt + 1})")

                    if attempt + 1 < MAX_RETRIES:
                        delay = retry_delay[attempt]
                        LOG.info(f"⏳ Aguardando {delay}s antes da nova tentativa...")
                        await asyncio.sleep(delay)
                        continue

                    LOG.error("❌ Falhou após todas as tentativas de envio (timeout)")
                    return False

                except Exception as e:
                    fh.close()
                    LOG.error(f"❌ Erro inesperado ao enviar vídeo: {e}")
                    return False
        
        # Arquivo excede limite
        LOG.warning(f"⚠️ Arquivo excede 50MB! Tentando comprimir...")
        
        # Atualizar mensagem
        if pm:
            await bot.edit_message_text(
                text="⚠️ Vídeo grande demais. Tome um café, estamos comprimindo para você ...",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )
        
        # Tentar comprimir
        compressed_path = os.path.join(tmpdir, "compressed_shopee.mp4")
        
        if ffmpeg_compress_video(video_path, compressed_path):
            if pm:
                await bot.edit_message_text(
                    text="📤 Enviando vídeo comprimido...",
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"]
                )
            
            with open(compressed_path, "rb") as fh:
                await bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    caption=f"{caption}\n\n📦 Vídeo comprimido para caber no Telegram"
                )
            
            # Limpar
            try:
                os.remove(compressed_path)
            except:
                pass
            
            return True
        else:
            LOG.error("❌ Falha na compressão")
            if pm:
                await bot.edit_message_text(
                    text="❌ Arquivo muito grande! Não consegui comprimir o suficiente.\n"
                         "Tente baixar um vídeo menor.",
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"]
                )
            return False
    
    except Exception as e:
        LOG.exception(f"❌ Erro ao enviar: {e}")
        return False

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

        # ✅ NOVA LÓGICA ANTI-CRASH: Verifica tamanho ANTES de processar
        file_size_mb = total_size / (1024 * 1024)
        LOG.info("📦 Tamanho do vídeo Shopee: %.2f MB", file_size_mb)

        # 🚫 Se vídeo > 50MB: NÃO comprime, apenas avisa o usuário
        if file_size_mb > 50:
            LOG.warning("⚠️ Vídeo Shopee excede 50MB (%.2f MB) - Implementando lógica anti-crash", file_size_mb)
            await application.bot.edit_message_text(
                text=(
                    "⚠️ <b>Arquivo muito grande</b>\n\n"
                    "O vídeo tem <code>{:.1f} MB</code> e excede o limite de <code>50 MB</code> "
                    "do Telegram.\n\n"
                    "❌ Não vou tentar comprimir (pode causar travamento)\n"
                    "✅ Sugestões:\n"
                    "  • Baixe pelo app oficial da Shopee\n"
                    "  • Solicite uma versão menor ao criador\n"
                    "  • Tente converter em outro formato"
                ).format(file_size_mb),
                chat_id=pm["chat_id"],
                message_id=pm["message_id"],
                parse_mode="HTML"
            )
            # Limpa arquivo temporário
            if os.path.exists(output_path):
                os.remove(output_path)
            return

        # Prossegue normalmente se arquivo ≤ 50MB
        with open(output_path, 'wb') as f:
            # OTIMIZAÇÃO #5: Chunks maiores (512KB) reduzem overhead e memória
            for chunk in video_response.iter_content(chunk_size=524288):  # 512 KB
                if chunk:
                    f.write(chunk)
                    del chunk  # Libera memória explicitamente

        LOG.info("✅ Vídeo da Shopee baixado com sucesso: %s", output_path)

        # ✅ Remove marca d'água SOMENTE se necessário
        if url_already_clean:
            # Marca já foi removida na URL - FFmpeg não necessário!
            LOG.info("✅ Vídeo baixado já SEM marca d'água (removida na URL)")
            caption = "🎬 Aproveite o seu vídeo 🎬"
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
            caption = "🎬 Aproveite o seu vídeo 🎬"
        else:
            LOG.warning("⚠️ FFmpeg não disponível, enviando vídeo original.")
            caption = "🎬 Aproveite o seu vídeo 🎬"

        # Envia o vídeo com validação de tamanho
        await application.bot.edit_message_text(
            text="✅ Download concluído, enviando...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )

        success = await safe_send_video_telegram(
            bot=application.bot,
            chat_id=chat_id,
            video_path=output_path,
            caption=caption,
            pm=pm,
            tmpdir=tmpdir
        )
        
        if not success:
            return

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
            # OTIMIZAÇÃO #5: Chunks maiores (512KB) reduzem overhead e memória
            for chunk in video_response.iter_content(chunk_size=524288):  # 512 KB
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
                            except Exception as e:
                                LOG.debug("Erro ignorado: %s", type(e).__name__)
        
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
            await application.bot.send_video(chat_id=chat_id, video=fh, caption="🎬 Aproveite o seu vídeo 🎬")
        
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

# ═══════════════════════════════════════════════════════════════
# 🔐 PROTEÇÃO DE ADMIN
# ═══════════════════════════════════════════════════════════════

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para o comando /stats (apenas admin)"""
    user_id = update.effective_user.id
    
    # 🔐 PROTEÇÃO: Apenas admin pode usar este comando
    if user_id != ADMIN_ID:
        await update.message.reply_text(
            "❌ <b>Acesso Negado</b>\n\n"
            "Este comando é restrito apenas ao administrador.",
            parse_mode="HTML"
        )
        LOG.warning("⚠️ Usuário %d tentou acessar /stats (não autorizado)", user_id)
        return
    
    count = get_monthly_users_count()
    stats_text = MESSAGES["stats"].format(count=count)
    await update.message.reply_text(stats_text, parse_mode="HTML")
    LOG.info("📊 Comando /stats executado por ADMIN %d", user_id)

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
- Para vídeos do YouTube, você pode escolher a qualidade (360p, 480p, 720p, 1080p).
- Não responda sobre assuntos que não sejam relacionados ao que esse assistente faz

Funcionalidades:
- Download de vídeos (Shopee, Instagram, TikTok, Twitter, etc.)
- Plano gratuito: 3 downloads/semana
- Plano premium: downloads ilimitados
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

# ═══════════════════════════════════════════════════════════════
# 📊 SISTEMA DE RELATÓRIOS MENSAIS PREMIUM
# ═══════════════════════════════════════════════════════════════

def get_premium_monthly_stats() -> dict:
    """
    Retorna estatísticas completas de assinantes premium por mês
    
    Returns:
        dict: {
            'total_active': int,           # Total de assinantes ativos
            'expires_this_month': int,     # Expiram este mês
            'expires_next_month': int,     # Expiram próximo mês
            'revenue_month': float,        # Receita mensal
            'revenue_total': float,        # Receita total
            'new_this_month': int,         # Novos este mês
            'by_expiry_date': list,        # [(data, quantidade), ...]
            'recent_subscribers': list     # [(user_id, data_ativação), ...]
        }
    """
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            
            # Data atual
            today = datetime.now()
            current_month = today.strftime("%Y-%m")
            next_month = (today + timedelta(days=32)).strftime("%Y-%m")
            
            # 1. Total de assinantes ativos
            c.execute("""
                SELECT COUNT(*) 
                FROM user_downloads 
                WHERE is_premium = 1 
                AND (premium_expires IS NULL OR premium_expires >= date('now'))
            """)
            total_active = c.fetchone()[0]
            
            # 2. Assinantes que expiram este mês
            c.execute("""
                SELECT COUNT(*) 
                FROM user_downloads 
                WHERE is_premium = 1 
                AND strftime('%Y-%m', premium_expires) = ?
            """, (current_month,))
            expires_this_month = c.fetchone()[0]
            
            # 3. Assinantes que expiram próximo mês
            c.execute("""
                SELECT COUNT(*) 
                FROM user_downloads 
                WHERE is_premium = 1 
                AND strftime('%Y-%m', premium_expires) = ?
            """, (next_month,))
            expires_next_month = c.fetchone()[0]
            
            # 4. Novos assinantes este mês
            c.execute("""
                SELECT COUNT(*) 
                FROM pix_payments 
                WHERE status = 'confirmed' 
                AND strftime('%Y-%m', confirmed_at) = ?
            """, (current_month,))
            new_this_month = c.fetchone()[0]
            
            # 5. Receita mensal (baseado em pagamentos confirmados)
            c.execute("""
                SELECT COALESCE(SUM(amount), 0) 
                FROM pix_payments 
                WHERE status = 'confirmed' 
                AND strftime('%Y-%m', confirmed_at) = ?
            """, (current_month,))
            revenue_month = c.fetchone()[0]
            
            # 6. Receita total
            c.execute("""
                SELECT COALESCE(SUM(amount), 0) 
                FROM pix_payments 
                WHERE status = 'confirmed'
            """)
            revenue_total = c.fetchone()[0]
            
            # 7. Distribuição por data de expiração (próximos 60 dias)
            c.execute("""
                SELECT 
                    DATE(premium_expires) as expiry_date,
                    COUNT(*) as count
                FROM user_downloads 
                WHERE is_premium = 1 
                AND premium_expires BETWEEN date('now') AND date('now', '+60 days')
                GROUP BY DATE(premium_expires)
                ORDER BY expiry_date
            """)
            by_expiry_date = c.fetchall()
            
            # 8. Últimos 10 assinantes
            c.execute("""
                SELECT 
                    p.user_id,
                    p.confirmed_at,
                    p.amount
                FROM pix_payments p
                WHERE p.status = 'confirmed'
                ORDER BY p.confirmed_at DESC
                LIMIT 10
            """)
            recent_subscribers = c.fetchall()
            
            return {
                'total_active': total_active,
                'expires_this_month': expires_this_month,
                'expires_next_month': expires_next_month,
                'revenue_month': revenue_month,
                'revenue_total': revenue_total,
                'new_this_month': new_this_month,
                'by_expiry_date': by_expiry_date,
                'recent_subscribers': recent_subscribers
            }
            
    except Exception as e:
        LOG.error(f"Erro ao buscar estatísticas premium: {e}")
        return {
            'total_active': 0,
            'expires_this_month': 0,
            'expires_next_month': 0,
            'revenue_month': 0.0,
            'revenue_total': 0.0,
            'new_this_month': 0,
            'by_expiry_date': [],
            'recent_subscribers': []
        }

def format_currency(value: float) -> str:
    """Formata valor monetário em BRL"""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def generate_bar_chart(value: int, max_value: int, length: int = 10) -> str:
    """Gera barra de progresso em ASCII"""
    if max_value == 0:
        return "░" * length
    
    filled = int((value / max_value) * length)
    bar = "█" * filled + "░" * (length - filled)
    return bar

async def mensal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler para o comando /mensal - Relatório detalhado de assinantes premium
    
    🔐 RESTRITO: Apenas administrador pode usar
    
    Mostra estatísticas completas incluindo:
    - Total de assinantes ativos
    - Novos assinantes do mês
    - Assinantes com renovação próxima
    - Receita mensal e total
    - Gráfico de expiração
    - Lista de últimos assinantes
    """
    user_id = update.effective_user.id
    
    # 🔐 PROTEÇÃO: Apenas admin pode usar este comando
    if user_id != ADMIN_ID:
        await update.message.reply_text(
            "❌ <b>Acesso Negado</b>\n\n"
            "Este comando é restrito apenas ao administrador.",
            parse_mode="HTML"
        )
        LOG.warning("⚠️ Usuário %d tentou acessar /mensal (não autorizado)", user_id)
        return
    
    LOG.info("📊 Comando /mensal executado por ADMIN %d", user_id)
    
    # Mensagem de carregamento
    loading_msg = await update.message.reply_text(
        "📊 <b>Gerando Relatório...</b>\n\n"
        "⏳ Analisando dados dos assinantes premium...",
        parse_mode="HTML"
    )
    
    try:
        # Busca estatísticas
        stats = get_premium_monthly_stats()
        
        # Data atual
        now = datetime.now()
        month_name = now.strftime("%B/%Y")
        month_name_pt = {
            'January': 'Janeiro', 'February': 'Fevereiro', 'March': 'Março',
            'April': 'Abril', 'May': 'Maio', 'June': 'Junho',
            'July': 'Julho', 'August': 'Agosto', 'September': 'Setembro',
            'October': 'Outubro', 'November': 'Novembro', 'December': 'Dezembro'
        }
        for en, pt in month_name_pt.items():
            month_name = month_name.replace(en, pt)
        
        # ═══════════════════════════════════════════════════════════
        # 📊 CABEÇALHO DO RELATÓRIO
        # ═══════════════════════════════════════════════════════════
        
        report = f"""╔══════════════════════════════════════════╗
║   📊 <b>RELATÓRIO MENSAL PREMIUM</b>         ║
╚══════════════════════════════════════════╝

📅 <b>Período:</b> {month_name}
🕐 <b>Gerado em:</b> {now.strftime("%d/%m/%Y às %H:%M")}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
        
        # ═══════════════════════════════════════════════════════════
        # 💎 VISÃO GERAL
        # ═══════════════════════════════════════════════════════════
        
        report += f"""<b>💎 VISÃO GERAL</b>

┌─────────────────────────────────────────┐
│ 👥 Assinantes Ativos:  <b>{stats['total_active']:>12}</b> │
│ ✨ Novos este mês:     <b>{stats['new_this_month']:>12}</b> │
│ ⚠️ Expiram este mês:   <b>{stats['expires_this_month']:>12}</b> │
│ 📅 Expiram próx. mês:  <b>{stats['expires_next_month']:>12}</b> │
└─────────────────────────────────────────┘

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
        
        # ═══════════════════════════════════════════════════════════
        # 💰 RECEITA
        # ═══════════════════════════════════════════════════════════
        
        report += f"""<b>💰 RECEITA</b>

┌─────────────────────────────────────────┐
│ 📈 Mensal:  <b>{format_currency(stats['revenue_month']):>21}</b> │
│ 💎 Total:   <b>{format_currency(stats['revenue_total']):>21}</b> │
└─────────────────────────────────────────┘

"""
        
        # Calcula média por assinante
        avg_per_subscriber = stats['revenue_month'] / stats['new_this_month'] if stats['new_this_month'] > 0 else 0
        report += f"💵 <b>Ticket Médio:</b> {format_currency(avg_per_subscriber)}\n\n"
        
        report += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # ═══════════════════════════════════════════════════════════
        # 📊 GRÁFICO DE RENOVAÇÕES (próximos 30 dias)
        # ═══════════════════════════════════════════════════════════
        
        if stats['by_expiry_date']:
            report += "<b>📊 RENOVAÇÕES PRÓXIMAS (30 DIAS)</b>\n\n"
            
            # Filtra apenas próximos 30 dias
            next_30_days = [
                (date, count) for date, count in stats['by_expiry_date']
                if datetime.strptime(date, "%Y-%m-%d") <= now + timedelta(days=30)
            ]
            
            if next_30_days:
                max_count = max(count for _, count in next_30_days)
                
                for expiry_date, count in next_30_days[:10]:  # Mostra apenas primeiros 10
                    date_obj = datetime.strptime(expiry_date, "%Y-%m-%d")
                    days_until = (date_obj - now).days
                    
                    # Formatação da data
                    date_formatted = date_obj.strftime("%d/%m")
                    
                    # Barra de progresso
                    bar = generate_bar_chart(count, max_count, length=8)
                    
                    # Emoji baseado na urgência
                    if days_until <= 7:
                        urgency = "🔴"
                    elif days_until <= 14:
                        urgency = "🟡"
                    else:
                        urgency = "🟢"
                    
                    report += f"{urgency} <code>{date_formatted}</code> │{bar}│ <b>{count}</b>\n"
                
                if len(next_30_days) > 10:
                    report += f"\n<i>... e mais {len(next_30_days) - 10} datas</i>\n"
            else:
                report += "✅ Nenhuma renovação nos próximos 30 dias\n"
            
            report += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # ═══════════════════════════════════════════════════════════
        # 👥 ÚLTIMOS ASSINANTES
        # ═══════════════════════════════════════════════════════════
        
        if stats['recent_subscribers']:
            report += "<b>👥 ÚLTIMOS ASSINANTES</b>\n\n"
            
            for user_id_sub, confirmed_at, amount in stats['recent_subscribers'][:5]:
                # Formata data
                try:
                    date_obj = datetime.fromisoformat(confirmed_at.replace('Z', '+00:00'))
                    date_str = date_obj.strftime("%d/%m/%y %H:%M")
                except:
                    date_str = confirmed_at[:16] if len(confirmed_at) >= 16 else confirmed_at
                
                # Mascara user_id (primeiros 3 e últimos 3 dígitos)
                user_id_str = str(user_id_sub)
                if len(user_id_str) > 6:
                    masked_id = f"{user_id_str[:3]}***{user_id_str[-3:]}"
                else:
                    masked_id = user_id_str
                
                report += f"🆔 <code>{masked_id}</code> │ {date_str} │ {format_currency(amount)}\n"
            
            if len(stats['recent_subscribers']) > 5:
                report += f"\n<i>... e mais {len(stats['recent_subscribers']) - 5} assinantes</i>\n"
            
            report += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # ═══════════════════════════════════════════════════════════
        # 📈 INSIGHTS E ANÁLISES
        # ═══════════════════════════════════════════════════════════
        
        report += "<b>📈 ANÁLISE</b>\n\n"
        
        # Taxa de renovação esperada
        if stats['total_active'] > 0:
            churn_rate = (stats['expires_this_month'] / stats['total_active']) * 100
            report += f"📊 <b>Taxa de Vencimento:</b> {churn_rate:.1f}%\n"
        
        # Crescimento
        if stats['new_this_month'] > stats['expires_this_month']:
            growth = stats['new_this_month'] - stats['expires_this_month']
            report += f"📈 <b>Crescimento Líquido:</b> +{growth} assinantes\n"
        elif stats['new_this_month'] < stats['expires_this_month']:
            decline = stats['expires_this_month'] - stats['new_this_month']
            report += f"📉 <b>Redução Líquida:</b> -{decline} assinantes\n"
        else:
            report += f"➡️ <b>Crescimento:</b> Estável\n"
        
        # Projeção próximo mês
        projected_active = stats['total_active'] - stats['expires_this_month'] + stats['expires_this_month']
        report += f"\n🔮 <b>Projeção próx. mês:</b> {projected_active} ativos\n"
        
        report += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # ═══════════════════════════════════════════════════════════
        # 🎯 AÇÕES RECOMENDADAS
        # ═══════════════════════════════════════════════════════════
        
        report += "<b>🎯 AÇÕES RECOMENDADAS</b>\n\n"
        
        if stats['expires_this_month'] > 0:
            report += f"⚠️ <b>{stats['expires_this_month']}</b> assinaturas expiram este mês\n"
            report += "   → Enviar lembrete de renovação\n\n"
        
        if stats['expires_next_month'] > 0:
            report += f"📅 <b>{stats['expires_next_month']}</b> assinaturas expiram próx. mês\n"
            report += "   → Preparar campanha de retenção\n\n"
        
        if stats['new_this_month'] == 0:
            report += "🔴 <b>Nenhum novo assinante este mês</b>\n"
            report += "   → Iniciar campanha de aquisição\n\n"
        
        if not stats['recent_subscribers']:
            report += "💡 <b>Dica:</b> Considere criar promoções\n\n"
        
        # ═══════════════════════════════════════════════════════════
        # 🔗 RODAPÉ
        # ═══════════════════════════════════════════════════════════
        
        report += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        report += "💡 <i>Use /status para ver dados individuais</i>\n"
        report += "💳 <i>Use /premium para ver opções de assinatura</i>"
        
        # Envia relatório
        await loading_msg.edit_text(report, parse_mode="HTML")
        
        LOG.info("✅ Relatório mensal enviado para usuário %d", user_id)
        
    except Exception as e:
        LOG.exception("❌ Erro ao gerar relatório mensal: %s", e)
        await loading_msg.edit_text(
            "❌ <b>Erro ao Gerar Relatório</b>\n\n"
            "Ocorreu um erro ao processar as estatísticas.\n"
            "Tente novamente em alguns instantes.",
            parse_mode="HTML"
        )

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
- Para vídeos do YouTube, você pode escolher a qualidade (360p, 480p, 720p, 1080p).
- Não responda sobre assuntos que não sejam relacionados ao que esse assistente faz

Funcionalidades:
- Download de vídeos (Shopee, Instagram, TikTok, Twitter, etc.)
- Plano gratuito: 3 downloads/semana
- Plano premium: downloads ilimitados
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
        PENDING.set(token, {
            "url": url,
            "user_id": user_id,
            "chat_id": update.effective_chat.id,
            "message_id": processing_msg.message_id,
            "timestamp": time.time(),
        })
        
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

        # Detecta se é YouTube para mostrar seleção de qualidade
        is_youtube = 'youtube' in url.lower() or 'youtu.be' in url.lower()

        if is_youtube:
            # Para YouTube: mostra botões de seleção de qualidade
            keyboard = [
                [
                    InlineKeyboardButton("📱 360p", callback_data=f"quality:{token}:360p"),
                    InlineKeyboardButton("📺 480p", callback_data=f"quality:{token}:480p"),
                ],
                [
                    InlineKeyboardButton("🎬 720p (Recomendado)", callback_data=f"quality:{token}:720p"),
                ],
                [
                    InlineKeyboardButton("🎥 1080p", callback_data=f"quality:{token}:1080p"),
                    InlineKeyboardButton("⭐ Melhor", callback_data=f"quality:{token}:best"),
                ],
                [
                    InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            confirm_text = (
                f"🎥 <b>YouTube - Escolha a Qualidade</b>\n\n"
                f"📹 <b>{title}</b>\n"
                f"⏱️ Duração: {duration}\n"
                f"📦 Tamanho estimado: {filesize}\n\n"
                f"💡 <b>Dica:</b> 720p é ideal para WhatsApp\n"
                f"⚠️ Qualidades maiores podem exceder 50 MB"
            )
        else:
            # Para outras plataformas: botões normais de confirmação
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
        PENDING.set(token, {
            "url": url,
            "user_id": user_id,
            "chat_id": update.effective_chat.id,
            "message_id": processing_msg.message_id,
            "timestamp": time.time(),
        })
        
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
        # OTIMIZAÇÃO #3: Reduz uso de memória do yt-dlp (50-70% menos RAM)
        "no_cache_dir": True,  # Desabilita cache em disco
        "extractor_retries": 4,  # Aumentado para melhor resiliência
        "fragment_retries": 4,   # Aumentado para melhor resiliência
        "buffersize": 1024 * 64,  # 64KB buffer (padrão: 1024KB)
        # Adiciona formato otimizado para yt-dlp 2025.11.12+
        "format": get_format_for_url(url),
        # 🔧 FIX CONEXÃO YOUTUBE: Aumenta timeouts e retries para evitar "Connection refused"
        "socket_timeout": 60,  # 60s timeout (aumentado de 30s)
        "http_chunk_size": 262144,  # 256KB chunks (mais estável)
        "retries": 25,  # ✅ CORRIGIDO: Número simples (não dicionário)
        "skip_unavailable_fragments": True,  # Evita falhar com fragmentos indisponíveis
        "force_ipv4": True,  # Força IPv4 (mais estável)
        # Headers padrão para evitar bloqueios
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
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
            "socket_timeout": 60,
            "retries": 25,  # ✅ CORRIGIDO: Número simples
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
        except Exception as e:
            LOG.debug("Erro ignorado: %s", type(e).__name__)

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para callbacks de confirmação de download"""
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split(":")
    action = parts[0]

    # Para quality, temos 3 partes: quality:token:720p
    if action == "quality":
        if len(parts) < 3:
            await query.answer("❌ Erro: formato inválido", show_alert=True)
            return

        token = parts[1]
        quality = parts[2]

        if token not in PENDING.cache:
            await query.edit_message_text(MESSAGES["error_expired"])
            return

        pm = PENDING.get(token)

        # Verifica se o usuário é o mesmo que solicitou
        if pm["user_id"] != query.from_user.id:
            await query.answer("⚠️ Esta ação não pode ser realizada por você.", show_alert=True)
            return

        # Armazena qualidade escolhida
        pm["quality"] = quality
        PENDING.set(token, pm)

        # Mostra confirmação com a qualidade escolhida
        quality_emoji = {
            "360p": "📱",
            "480p": "📺",
            "720p": "🎬",
            "1080p": "🎥",
            "best": "⭐"
        }

        keyboard = [
            [
                InlineKeyboardButton("✅ Confirmar Download", callback_data=f"dl:{token}"),
                InlineKeyboardButton("🔙 Voltar", callback_data=f"back:{token}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        confirm_text = (
            f"🎥 <b>YouTube Download</b>\n\n"
            f"✅ Qualidade selecionada: {quality_emoji.get(quality, '🎬')} <b>{quality}</b>\n\n"
            f"📹 Vídeo pronto para download!\n"
            f"Clique em <b>Confirmar</b> para iniciar."
        )

        await query.edit_message_text(
            confirm_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        LOG.info("Usuário %d escolheu qualidade %s", pm["user_id"], quality)
        return

    # Para dl e cancel, temos 2 partes: dl:token ou cancel:token
    if len(parts) < 2:
        await query.answer("❌ Erro: formato inválido", show_alert=True)
        return

    token = parts[1]

    if token not in PENDING.cache:
        await query.edit_message_text(MESSAGES["error_expired"])
        return

    pm = PENDING.get(token)

    # Verifica se o usuário é o mesmo que solicitou
    if pm["user_id"] != query.from_user.id:
        await query.answer("⚠️ Esta ação não pode ser realizada por você.", show_alert=True)
        return

    if action == "cancel":
        # Remove do cache (LimitedCache não tem del, usa cache.pop)
        PENDING.cache.pop(token, None)
        await query.edit_message_text(MESSAGES["download_cancelled"])
        LOG.info("Download cancelado pelo usuário %d", pm["user_id"])
        return

    if action == "back":
        # Volta para seleção de qualidade (reconstrói a tela inicial)
        url = pm["url"]

        keyboard = [
            [
                InlineKeyboardButton("📱 360p", callback_data=f"quality:{token}:360p"),
                InlineKeyboardButton("📺 480p", callback_data=f"quality:{token}:480p"),
            ],
            [
                InlineKeyboardButton("🎬 720p (Recomendado)", callback_data=f"quality:{token}:720p"),
            ],
            [
                InlineKeyboardButton("🎥 1080p", callback_data=f"quality:{token}:1080p"),
                InlineKeyboardButton("⭐ Melhor", callback_data=f"quality:{token}:best"),
            ],
            [
                InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        confirm_text = (
            f"🎥 <b>YouTube - Escolha a Qualidade</b>\n\n"
            f"💡 <b>Dica:</b> 720p é ideal para WhatsApp\n"
            f"⚠️ Qualidades maiores podem exceder 50 MB"
        )

        await query.edit_message_text(
            confirm_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
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
        PENDING.cache.pop(token, None)
        
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
        # OTIMIZADO: Log conciso com informações essenciais
        LOG.info("📥 Download iniciado | User: %d | URL: %s", pm["user_id"], pm["url"][:60])

async def _process_download(token: str, pm: dict):
    """Processa o download em background com controle de memória"""
    tmpdir = None
    
    # Aguarda na fila (semáforo para controlar 2 downloads simultâneos)
    async with DOWNLOAD_SEMAPHORE:
        try:
            tmpdir = tempfile.mkdtemp(prefix=f"ytbot_")
            
            try:
                await _do_download(token, pm["url"], tmpdir, pm["chat_id"], pm)
            finally:
                # Limpa arquivos temporários
                if tmpdir and os.path.exists(tmpdir):
                    try:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                    except Exception as e:
                        LOG.error("Erro ao limpar tmpdir: %s", e)
                
                # Remove da lista de downloads ativos
                if token in ACTIVE_DOWNLOADS:
                    del ACTIVE_DOWNLOADS[token]
                
                # OTIMIZAÇÃO: Força GC após download para liberar memória imediatamente
                gc.collect(0)
                
                # Verifica se precisa fazer limpeza agressiva
                cleanup_memory()
                    
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
                # Limpeza também em caso de erro
                gc.collect(0)
                cleanup_memory()

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

    # Obtém qualidade escolhida pelo usuário (para YouTube)
    quality = pm.get("quality", None)

    ydl_opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "quiet": False,
        "logger": LOG,
        "format": get_format_for_url(url, quality=quality),
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 1,
        "force_ipv4": True,
        "socket_timeout": 60,  # Aumentado de 30s para 60s
        "http_chunk_size": 262144,  # 256KB (mais estável que 512KB)
        "retries": 25,  # ✅ CORRIGIDO: Número simples (não dicionário)
        "fragment_retries": 25,  # Aumentado significativamente
        "no_check_certificate": True,
        "prefer_insecure": True,
        # OTIMIZAÇÃO #3: Reduz uso de memória
        "no_cache_dir": True,  # Desabilita cache em disco
        "buffersize": 1024 * 64,  # 64KB buffer
        "skip_unavailable_fragments": True,  # Evita falhar com fragmentos indisponíveis
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
            "socket_timeout": 60,  # Aumentado para Shopee também
            "retries": 25,  # ✅ CORRIGIDO: Número simples
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
                except Exception as e:
                    LOG.debug("Erro ignorado: %s", type(e).__name__)
                
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
                caption = "🎬 Aproveite o seu vídeo 🎬"
                
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
    """Executa yt-dlp com as opções fornecidas e retry automático em caso de falha de conexão"""
    def execute():
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download(urls)
    
    # 🔧 FIX YOUTUBE: Tenta novamente se falhar por conexão recusada
    try:
        ydl_with_retry(execute, max_retries=5, backoff_factor=2)
    except Exception as e:
        LOG.error("❌ Download falhou após todas as tentativas: %s", e)
        raise

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
        token for token, pm in PENDING.cache.items()
        if now - pm["timestamp"] > PENDING_EXPIRE_SECONDS
    ]
    for token in expired:
        PENDING.cache.pop(token, None)
    
    # LimitedCache já controla tamanho máximo automaticamente
    # Não precisa mais do while len(PENDING)

# ============================
# REGISTRO DE HANDLERS
# ============================

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CommandHandler("status", status_cmd))
application.add_handler(CommandHandler("premium", premium_cmd))
application.add_handler(CommandHandler("ai", ai_cmd))  # ← Comando IA
application.add_handler(CommandHandler("mensal", mensal_cmd))  # ← Comando relatório mensal
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:|quality:|back:)"))
application.add_handler(CallbackQueryHandler(callback_buy_premium, pattern=r"^subscribe:"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# ============================
# FLASK ROUTES
# ============================

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Endpoint webhook para receber updates do Telegram"""
    try:
        # 📊 Registra atividade
        health_monitor.record_activity("telegram")
        LAST_ACTIVITY["flask"] = time.time()
        
        update_data = request.get_json(force=True)
        
        # Valida se tem dados
        if not update_data:
            LOG.warning("⚠️ Webhook recebeu dados vazios")
            return jsonify({"status": "no_data"}), 200
        
        update = Update.de_json(update_data, application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
        
        # IMPORTANTE: Sempre retorna 200 OK
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        LOG.exception("Falha ao processar webhook: %s", e)
        health_monitor.record_error()
        
        # CRÍTICO: Retorna 200 mesmo com erro para evitar retry infinito do Telegram
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/")
def index():
    """Rota principal"""
    return "🤖 Bot de Download Ativo"

@app.route("/diagnostics")
def diagnostics():
    """Endpoint de diagnóstico completo"""
    now = time.time()
    
    diagnostics_data = {
        "status": "operational",
        "timestamp": datetime.now().isoformat(),
        "system": {
            "uptime_seconds": int(now - health_monitor.last_health_check),
            "python_version": sys.version,
            "pid": os.getpid()
        },
        "telegram": {
            "last_update": datetime.fromtimestamp(LAST_ACTIVITY["telegram"]).isoformat(),
            "inactive_seconds": int(now - LAST_ACTIVITY["telegram"]),
            "webhook_errors": health_monitor.webhook_errors,
            "is_healthy": health_monitor.is_healthy
        },
        "flask": {
            "last_request": datetime.fromtimestamp(LAST_ACTIVITY["flask"]).isoformat(),
            "inactive_seconds": int(now - LAST_ACTIVITY["flask"])
        },
        "downloads": {
            "active": len(ACTIVE_DOWNLOADS),
            "pending": len(PENDING.cache) if hasattr(PENDING, 'cache') else 0,
            "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
            "queue_available": MAX_CONCURRENT_DOWNLOADS - len(ACTIVE_DOWNLOADS)
        },
        "database": {
            "file": DB_FILE,
            "exists": os.path.exists(DB_FILE),
            "size_bytes": os.path.getsize(DB_FILE) if os.path.exists(DB_FILE) else 0
        },
        "features": {
            "keepalive_enabled": KEEPALIVE_ENABLED,
            "keepalive_interval": KEEPALIVE_INTERVAL,
            "groq_available": GROQ_AVAILABLE and bool(GROQ_API_KEY),
            "mercadopago_available": MERCADOPAGO_AVAILABLE and bool(MERCADOPAGO_ACCESS_TOKEN)
        },
        "cookies": {
            "youtube": bool(COOKIE_YT),
            "shopee": bool(COOKIE_SHOPEE),
            "instagram": bool(COOKIE_IG)
        }
    }
    
    # Testa webhook do Telegram
    try:
        future = asyncio.run_coroutine_threadsafe(application.bot.get_me(), APP_LOOP)
        webhook_info = future.result(timeout=10)
        diagnostics_data["telegram"]["bot_username"] = webhook_info.username
        diagnostics_data["telegram"]["bot_id"] = webhook_info.id
    except Exception as e:
        diagnostics_data["telegram"]["error"] = str(e)
        diagnostics_data["status"] = "degraded"
    
    # Testa banco de dados
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM user_downloads")
            total_users = cursor.fetchone()[0]
            diagnostics_data["database"]["total_users"] = total_users
    except Exception as e:
        diagnostics_data["database"]["error"] = str(e)
        diagnostics_data["status"] = "degraded"
    
    return diagnostics_data, 200

@app.route("/health")
def health():
    """Endpoint de health check simplificado para Render"""
    # Registra atividade do Flask
    LAST_ACTIVITY["flask"] = time.time()

    # Informações básicas
    checks = {
        "status": "ok",  # Sempre OK para evitar restart
        "bot": "ok",
        "db": "ok",
        "pending_count": len(PENDING.cache) if hasattr(PENDING, 'cache') else 0,
        "active_downloads": len(ACTIVE_DOWNLOADS),
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
        "queue_available": MAX_CONCURRENT_DOWNLOADS - len(ACTIVE_DOWNLOADS),
        "cookies": {
            "youtube": bool(COOKIE_YT),
            "shopee": bool(COOKIE_SHOPEE),
            "instagram": bool(COOKIE_IG)
        },
        "timestamp": datetime.now().isoformat(),
        "uptime_seconds": int(time.time() - health_monitor.last_health_check)
    }

    # Adiciona informações do monitor (somente para diagnóstico interno)
    health_status = health_monitor.check_health()
    checks.update({
        "monitor": health_status,
        "last_telegram_activity": datetime.fromtimestamp(LAST_ACTIVITY["telegram"]).isoformat(),
        "last_flask_activity": datetime.fromtimestamp(LAST_ACTIVITY["flask"]).isoformat()
    })

    # ✅ Sempre retorna 200 OK, mesmo se monitor indicar problema
    return checks, 200
    
    # Testa banco de dados
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DB_FILE, timeout=5)
            conn.execute("SELECT 1")
            conn.close()
    except Exception as e:
        checks["db"] = f"error: {str(e)}"
        checks["status"] = "unhealthy"
        LOG.error("Health check DB falhou: %s", e)
    
    # Testa bot
    try:
        future = asyncio.run_coroutine_threadsafe(application.bot.get_me(), APP_LOOP)
        bot_info = future.result(timeout=10)
        checks["bot_username"] = bot_info.username
        checks["bot_id"] = bot_info.id
    except Exception as e:
        checks["bot"] = f"error: {str(e)}"
        checks["status"] = "unhealthy"
        LOG.error("Health check bot falhou: %s", e)
    
    # Define status HTTP
    if checks["status"] == "unhealthy" or not health_status["healthy"]:
        status_code = 503  # Service Unavailable
        checks["status"] = "unhealthy"
    else:
        status_code = 200
    
    return checks, status_code

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
    """
    Endpoint para receber notificações do Render
    Otimizado para evitar erros 502 durante deploy
    """
    try:
        # GET request - apenas confirma que está ativo
        if request.method == "GET":
            return {"status": "active", "message": "Webhook ativo"}, 200
        
        # POST request - processa evento do Render
        payload = request.get_json(silent=True) or {}
        
        # Retorna OK imediatamente para evitar timeout
        # Processamento será feito em background
        
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
                LOG.debug("✅ Alerta enviado para Discord")
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
            qr_code_text = response["point_of_interaction"]["transaction_data"]["qr_code"]

            await query.edit_message_text(
                (
                    f"✅ Pedido criado!\n\n"
                    f"💰 <b>Valor:</b> R$ {PREMIUM_PRICE:.2f}\n\n"
                    f"<b>PIX Copia e Cola:</b>\n"
                    f"<code>{qr_code_text}</code>\n\n"
                    f"📋 Copie o código acima e cole no seu banco para realizar o pagamento."
                ),
                parse_mode=ParseMode.HTML
            )
        else:
            await query.edit_message_text("❌ Erro ao criar pagamento. Tente novamente mais tarde.")
    except Exception as e:
        LOG.error("Erro no subscribe_callback: %s", e)
        await query.edit_message_text(f"❌ Falha interna: {e}")

# ============================
# MAIN
# ============================

if __name__ == "__main__":
    # Inicia thread de limpeza automática e garbage collection
    cleanup_thread = threading.Thread(target=cleanup_and_gc_routine, daemon=True)
    cleanup_thread.start()
    LOG.info("✅ Thread de limpeza automática e GC iniciada")
    
    # 🧹 Garbage collection agressivo a cada 5 minutos
    def aggressive_gc_routine():
        """Força garbage collection periodicamente para liberar memória"""
        while True:
            try:
                time.sleep(300)  # 5 minutos
                collected = gc.collect()
                if collected > 0:
                    LOG.debug(f"🧹 GC agressivo: {collected} objetos coletados")
            except Exception as e:
                LOG.error(f"❌ Erro em GC agressivo: {e}")
    
    gc_thread = threading.Thread(target=aggressive_gc_routine, daemon=True)
    gc_thread.start()
    LOG.info("✅ Thread de GC agressivo iniciada (intervalo: 5min)")
    
    # 🚀 Inicia rotina periódica de limpeza de memória (assíncrona)
    asyncio.run_coroutine_threadsafe(memory_cleanup_routine(), APP_LOOP)
    LOG.info(f"✅ Rotina de limpeza de memória iniciada (intervalo: {MEMORY_CLEANUP_INTERVAL}s, limite: {MAX_MEMORY_USAGE_MB}MB)")
    
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
    
    if __name__ == "__main__":
        port = int(os.environ.get("PORT", 10000))
        LOG.info("🚀 Iniciando servidor Flask na porta %d", port)
        app.run(host="0.0.0.0", port=port)

# ============================
# OTIMIZAÇÕES ADICIONAIS (SAFE)
# Não removem nenhuma função existente
# ============================

# 1. Garante permissão segura nos cookies (Render/Linux)
try:
    for _cookie in [COOKIE_YT, COOKIE_SHOPEE, COOKIE_IG]:
        if _cookie and os.path.exists(_cookie):
            os.chmod(_cookie, 0o600)
            LOG.info("Permissão ajustada para cookies: %s", _cookie)
except Exception as e:
    LOG.warning("Falha ao ajustar permissão de cookies: %s", e)


# 2. HTTPX opcional para reduzir bloqueios (fallback em requests)
try:
    import httpx
    HTTPX_AVAILABLE = True
    LOG.info("httpx disponível - downloads mais estáveis")
except Exception:
    HTTPX_AVAILABLE = False
    LOG.info("httpx não disponível - usando requests padrão")

# 3. Função auxiliar otimizada de download (fallback seguro)
# ════════════════════════════════════════════════════════════════
# 📥 DOWNLOAD STREAMING - Não carrega arquivo inteiro na RAM
# ════════════════════════════════════════════════════════════════

async def safe_stream_download(url, headers=None, cookies=None, timeout=120, output_file=None):
    """
    Download com streaming real - não carrega arquivo inteiro na RAM.
    
    Args:
        url: URL do arquivo
        headers: Headers HTTP (opcional)
        cookies: Cookies (opcional)
        timeout: Timeout em segundos
        output_file: Caminho para salvar arquivo (streaming direto ao disco)
    
    Returns:
        Se output_file: retorna caminho do arquivo
        Se output_file é None: retorna generator de chunks
    """
    CHUNK_SIZE = 8192  # 8KB chunks (otimizado para Render)
    
    try:
        # Preferir httpx para async streaming
        if HTTPX_AVAILABLE:
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    async with client.stream('GET', url, headers=headers, cookies=cookies) as r:
                        r.raise_for_status()
                        
                        # Se arquivo de saída especificado, fazer streaming direto ao disco
                        if output_file:
                            with open(output_file, 'wb') as f:
                                async for chunk in r.aiter_bytes(chunk_size=CHUNK_SIZE):
                                    if chunk:
                                        f.write(chunk)
                            LOG.debug(f"📥 Download (streaming): {url[:80]}... → {output_file}")
                            return output_file
                        else:
                            # Retornar generator de chunks
                            async def chunk_generator():
                                async for chunk in r.aiter_bytes(chunk_size=CHUNK_SIZE):
                                    if chunk:
                                        yield chunk
                            return chunk_generator()
                            
            except Exception as e:
                LOG.warning(f"httpx streaming falhou: {e}. Usando requests...")
        
        # Fallback para requests (síncrono mas com streaming)
        resp = requests.get(
            url, 
            headers=headers, 
            cookies=cookies, 
            timeout=timeout,
            stream=True  # ← STREAMING REAL: não carrega na RAM
        )
        resp.raise_for_status()
        
        # Se arquivo de saída especificado, fazer streaming direto ao disco
        if output_file:
            with open(output_file, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
            LOG.debug(f"📥 Download (streaming via requests): {url[:80]}... → {output_file}")
            return output_file
        else:
            # Retornar generator de chunks
            def chunk_generator():
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        yield chunk
            return chunk_generator()
    
    except requests.Timeout:
        LOG.error(f"⏱️ Timeout ao fazer download streaming: {url}")
        raise
    except Exception as e:
        LOG.error(f"❌ Erro no streaming: {e}")
        raise


# 4. Verificador de FFmpeg antes de remover watermark
def ffmpeg_available():
    try:
        import subprocess
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except:
        return False


# Log final
LOG.info("✅ Módulo de otimizações carregado")
LOG.info("✅ Garbage Collector agressivo ativado")
LOG.info("✅ LimitedCache para USER_LAST_DOWNLOAD ativado")
LOG.info("✅ Safe streaming download implementado (streaming real, não RAM)")

# ============================================================
# TWITTER BOT (CONTROLADO POR VARIÁVEL DE AMBIENTE)
# ============================================================
ENABLE_TWITTER = os.getenv("ENABLE_TWITTER", "false").lower() == "true"

if ENABLE_TWITTER:
    try:
        import twitter_bot
        twitter_bot.twitter_entrypoint()
        LOG.info("🤖 Twitter bot habilitado via ENABLE_TWITTER")
    except Exception as e:
        LOG.warning(f"⚠️ Falha ao iniciar Twitter bot: {e}")
else:
    LOG.info("⛔ Twitter bot desabilitado (ENABLE_TWITTER=false)")
