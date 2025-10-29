#!/usr/bin/env python3
"""
bot_with_cookies.py - Versão Melhorada com Suporte Shopee

Telegram bot (webhook) que:
- detecta links enviados diretamente ou em grupo quando mencionado (@SeuBot + link),
- pergunta "quer baixar?" com botão,
- ao confirmar, inicia o download e mostra uma barra de progresso atualizada,
- envia partes se necessário (ffmpeg) e mostra mensagem final.
- track de usuários mensais via SQLite.
- suporte customizado para Shopee Video

Melhorias implementadas:
- Cleanup automático de arquivos temporários
- Proteção contra race conditions no SQLite
- Watchdog timeout para downloads travados
- Validação de URLs
- Expiração automática de requests pendentes
- Tratamento de erros robusto
- Mensagens de erro amigáveis
- Health check endpoint
- Extrator customizado para Shopee Video

Requisitos:
- TELEGRAM_BOT_TOKEN (env)
- YT_COOKIES_B64 (opcional; base64 do cookies.txt em formato Netscape)
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
from urllib.parse import urlparse, parse_qs, unquote
import yt_dlp

try:
    import requests
    from bs4 import BeautifulSoup
    SHOPEE_SUPPORT = True
except ImportError:
    SHOPEE_SUPPORT = False
    logging.warning("requests ou beautifulsoup4 não instalados. Suporte Shopee limitado.")

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

# ==================== CONFIGURAÇÃO ====================

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ytbot")

# Token
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido. Defina o secret TELEGRAM_BOT_TOKEN e redeploy.")
    sys.exit(1)

LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# Constantes
URL_RE = re.compile(r"(https?://[^\s]+)")
DB_FILE = "users.db"
PENDING_MAX_SIZE = 1000
PENDING_EXPIRE_SECONDS = 600  # 10 minutos
WATCHDOG_TIMEOUT = 300  # 5 minutos (maior para Shopee)
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
SPLIT_SIZE = 45 * 1024 * 1024  # 45MB

# Estado global
PENDING = OrderedDict()  # token -> metadata
DB_LOCK = threading.Lock()

# Mensagens de erro
ERROR_MESSAGES = {
    "timeout": "⏱️ O download demorou muito e foi cancelado.",
    "invalid_url": "⚠️ Esta URL não é válida ou não é suportada.",
    "file_too_large": "📦 O arquivo é muito grande para processar.",
    "network_error": "🌐 Erro de conexão. Tente novamente em alguns minutos.",
    "ffmpeg_error": "🎬 Erro ao processar o vídeo.",
    "upload_error": "📤 Erro ao enviar o arquivo.",
    "shopee_extract_error": "🛍️ Não consegui extrair o vídeo da Shopee. O link pode estar incorreto ou o vídeo pode estar privado.",
    "unknown": "❌ Ocorreu um erro inesperado. Tente novamente.",
    "expired": "⏰ Este pedido expirou. Envie o link novamente.",
}

# Flask app
app = Flask(__name__)

# ==================== TELEGRAM APPLICATION ====================

try:
    application = ApplicationBuilder().token(TOKEN).build()
    LOG.info("ApplicationBuilder criado com sucesso.")
except Exception as e:
    LOG.exception("Erro ao construir ApplicationBuilder: %s", str(e))
    LOG.error("Tipo: %s, Args: %s", type(e).__name__, e.args)
    sys.exit(1)

# Cria loop asyncio persistente em background
APP_LOOP = asyncio.new_event_loop()

def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

LOG.info("Iniciando event loop de background...")
loop_thread = threading.Thread(target=_start_loop, args=(APP_LOOP,), daemon=True)
loop_thread.start()

# Inicializa a application no loop de background
try:
    fut = asyncio.run_coroutine_threadsafe(application.initialize(), APP_LOOP)
    fut.result(timeout=30)
    LOG.info("Application inicializada no loop de background.")
except Exception as e:
    LOG.exception("Falha ao inicializar a Application no loop de background: %s", str(e))
    sys.exit(1)

# ==================== SQLITE DATABASE ====================

def init_db():
    """Inicializa o banco de dados."""
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
            LOG.info("Banco de dados inicializado.")
        except sqlite3.Error as e:
            LOG.error("Erro ao inicializar banco de dados: %s", e)
            raise
        finally:
            conn.close()

def update_user(user_id: int):
    """Atualiza a tabela com o usuário atual."""
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
        except sqlite3.Error as e:
            LOG.error("Erro SQLite ao atualizar user %s: %s", user_id, e)
        finally:
            conn.close()

def get_monthly_users_count() -> int:
    """Retorna a contagem de usuários únicos do mês atual."""
    month = time.strftime("%Y-%m")
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM monthly_users WHERE last_month=?", (month,))
            count = c.fetchone()[0]
            return count
        except sqlite3.Error as e:
            LOG.error("Erro ao obter contagem de usuários: %s", e)
            return 0
        finally:
            conn.close()

init_db()

# ==================== COOKIES ====================

def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    """Prepara arquivo de cookies a partir de variável de ambiente."""
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("Nenhuma variável %s encontrada – rodando sem cookies.", env_var)
        return None
    
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        LOG.error("Falha ao decodificar %s: %s", env_var, e)
        return None

    try:
        fd, path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(raw)
        LOG.info("Cookies gravados em %s", path)
        return path
    except Exception as e:
        LOG.error("Falha ao escrever cookies: %s", e)
        return None

COOKIE_PATH = prepare_cookies_from_env()

# ==================== SHOPEE EXTRACTOR ====================

class ShopeeExtractor:
    """Extrator customizado para vídeos da Shopee."""
    
    def __init__(self, url: str):
        self.original_url = url
        self.url = self._resolve_universal_link(url)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': 'https://shopee.com.br/',
        })
    
    def _resolve_universal_link(self, url: str) -> str:
        """Resolve universal links da Shopee para URL real."""
        try:
            # Verifica se é um universal link
            if 'universal-link' in url or 'deep_and_web' in url:
                LOG.info("ShopeeExtractor: Detectado universal link, extraindo URL real...")
                
                # Extrai a URL do parâmetro redir
                from urllib.parse import parse_qs, urlparse, unquote
                parsed = urlparse(url)
                params = parse_qs(parsed.query)
                
                if 'redir' in params:
                    real_url = unquote(params['redir'][0])
                    LOG.info("ShopeeExtractor: URL real extraída: %s", real_url[:100])
                    return real_url
                
                # Tenta seguir redirecionamentos
                LOG.info("ShopeeExtractor: Tentando seguir redirecionamentos...")
                response = requests.get(
                    url, 
                    allow_redirects=True, 
                    timeout=10,
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                final_url = response.url
                LOG.info("ShopeeExtractor: URL final após redirecionamento: %s", final_url[:100])
                return final_url
            
            return url
        except Exception as e:
            LOG.error("ShopeeExtractor: Erro ao resolver universal link: %s", e)
            return url
    
    def extract_video_url(self) -> dict:
        """
        Extrai URL do vídeo da Shopee.
        Retorna dict com: {'url': str, 'title': str, 'success': bool, 'error': str}
        """
        LOG.info("ShopeeExtractor: Iniciando extração de %s", self.url)
        
        try:
            # Verifica se é Shopee Video (sv.shopee.com.br)
            if 'sv.shopee' in self.url or 'share-video' in self.url:
                result = self._extract_shopee_video()
                if result['success']:
                    return result
            
            # Método 1: Tentar extrair do HTML
            result = self._extract_from_html()
            if result['success']:
                return result
            
            # Método 2: Tentar extrair de API/JSON embarcado
            result = self._extract_from_api()
            if result['success']:
                return result
            
            # Método 3: Tentar encontrar vídeo direto em meta tags
            result = self._extract_from_meta_tags()
            if result['success']:
                return result
            
            LOG.error("ShopeeExtractor: Todos os métodos falharam")
            return {
                'success': False,
                'error': 'Não foi possível extrair o vídeo',
                'url': None,
                'title': 'shopee_video'
            }
            
        except Exception as e:
            LOG.exception("ShopeeExtractor: Erro durante extração: %s", e)
            return {
                'success': False,
                'error': str(e),
                'url': None,
                'title': 'shopee_video'
            }
    
    def _extract_shopee_video(self) -> dict:
        """Extrai vídeo da plataforma Shopee Video (sv.shopee)."""
        try:
            LOG.info("ShopeeExtractor: Detectado Shopee Video, usando método específico")
            
            response = self.session.get(self.url, timeout=30, allow_redirects=True)
            response.raise_for_status()
            
            # Shopee Video geralmente tem a URL do vídeo em JSON na página
            # Procura por padrões específicos
            patterns = [
                r'"videoUrl"\s*:\s*"([^"]+)"',
                r'"video_url"\s*:\s*"([^"]+)"',
                r'"url"\s*:\s*"(https://[^"]*\.mp4[^"]*)"',
                r'"playAddr"\s*:\s*"([^"]+)"',
                r'playAddr["\']:\s*["\']([^"\']+)',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, response.text)
                for match in matches:
                    video_url = match.replace('\\/', '/')
                    if 'mp4' in video_url or 'video' in video_url:
                        # Decodifica se necessário
                        if '\\u' in video_url:
                            video_url = video_url.encode().decode('unicode_escape')
                        
                        LOG.info("ShopeeExtractor: Vídeo Shopee Video encontrado: %s", video_url[:100])
                        
                        # Tenta extrair título
                        title_match = re.search(r'"description"\s*:\s*"([^"]+)"', response.text)
                        title = title_match.group(1) if title_match else 'shopee_video'
                        
                        return {
                            'success': True,
                            'url': video_url,
                            'title': self._clean_title(title),
                            'error': None
                        }
            
            # Tenta buscar no HTML também
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Procura scripts com dados JSON
            scripts = soup.find_all('script', type='application/json')
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    video_url = self._find_video_in_json(data)
                    if video_url:
                        LOG.info("ShopeeExtractor: Vídeo encontrado em script JSON: %s", video_url[:100])
                        return {
                            'success': True,
                            'url': video_url,
                            'title': self._find_title_in_json(data) or 'shopee_video',
                            'error': None
                        }
                except:
                    continue
            
            # Procura por tags video
            video_tag = soup.find('video')
            if video_tag:
                video_url = video_tag.get('src') or video_tag.get('data-src')
                if video_url:
                    if not video_url.startswith('http'):
                        video_url = 'https:' + video_url if video_url.startswith('//') else 'https://sv.shopee.com.br' + video_url
                    
                    LOG.info("ShopeeExtractor: Vídeo encontrado em tag video: %s", video_url[:100])
                    return {
                        'success': True,
                        'url': video_url,
                        'title': 'shopee_video',
                        'error': None
                    }
            
            return {'success': False, 'error': 'Vídeo não encontrado no Shopee Video', 'url': None, 'title': None}
            
        except Exception as e:
            LOG.error("ShopeeExtractor: Erro no método Shopee Video: %s", e)
            return {'success': False, 'error': str(e), 'url': None, 'title': None}
    
    def _extract_from_html(self) -> dict:
        """Extrai vídeo parseando HTML."""
        try:
            response = self.session.get(self.url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Procura por tag video
            video_tag = soup.find('video')
            if video_tag:
                video_url = video_tag.get('src') or video_tag.get('data-src')
                if video_url:
                    if not video_url.startswith('http'):
                        video_url = 'https:' + video_url if video_url.startswith('//') else 'https://shopee.com.br' + video_url
                    
                    title = soup.find('title')
                    title_text = title.text.strip() if title else 'shopee_video'
                    
                    LOG.info("ShopeeExtractor: Vídeo encontrado via HTML tag: %s", video_url[:100])
                    return {
                        'success': True,
                        'url': video_url,
                        'title': self._clean_title(title_text),
                        'error': None
                    }
            
            # Procura por source dentro de video
            sources = soup.find_all('source')
            for source in sources:
                video_url = source.get('src') or source.get('data-src')
                if video_url and ('mp4' in video_url.lower() or 'video' in video_url.lower()):
                    if not video_url.startswith('http'):
                        video_url = 'https:' + video_url if video_url.startswith('//') else 'https://shopee.com.br' + video_url
                    
                    LOG.info("ShopeeExtractor: Vídeo encontrado via source tag: %s", video_url[:100])
                    return {
                        'success': True,
                        'url': video_url,
                        'title': 'shopee_video',
                        'error': None
                    }
            
            return {'success': False, 'error': 'Nenhuma tag de vídeo encontrada', 'url': None, 'title': None}
            
        except Exception as e:
            LOG.error("ShopeeExtractor: Erro no método HTML: %s", e)
            return {'success': False, 'error': str(e), 'url': None, 'title': None}
    
    def _extract_from_api(self) -> dict:
        """Extrai vídeo de dados JSON embarcados na página."""
        try:
            response = self.session.get(self.url, timeout=30)
            response.raise_for_status()
            
            # Procura por dados JSON embarcados
            patterns = [
                r'window\.__INITIAL_STATE__\s*=\s*({.+?});',
                r'window\.App\s*=\s*({.+?});',
                r'__NEXT_DATA__\s*=\s*({.+?})</script>',
                r'data-state="({.+?})"',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, response.text)
                for match in matches:
                    try:
                        data = json.loads(match)
                        video_url = self._find_video_in_json(data)
                        if video_url:
                            LOG.info("ShopeeExtractor: Vídeo encontrado via JSON: %s", video_url[:100])
                            return {
                                'success': True,
                                'url': video_url,
                                'title': self._find_title_in_json(data) or 'shopee_video',
                                'error': None
                            }
                    except json.JSONDecodeError:
                        continue
            
            return {'success': False, 'error': 'Nenhum JSON válido encontrado', 'url': None, 'title': None}
            
        except Exception as e:
            LOG.error("ShopeeExtractor: Erro no método API: %s", e)
            return {'success': False, 'error': str(e), 'url': None, 'title': None}
    
    def _extract_from_meta_tags(self) -> dict:
        """Extrai vídeo de meta tags Open Graph."""
        try:
            response = self.session.get(self.url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Procura meta tags de vídeo
            meta_patterns = [
                {'property': 'og:video'},
                {'property': 'og:video:url'},
                {'property': 'og:video:secure_url'},
                {'name': 'twitter:player:stream'},
            ]
            
            for pattern in meta_patterns:
                meta = soup.find('meta', attrs=pattern)
                if meta:
                    video_url = meta.get('content')
                    if video_url:
                        LOG.info("ShopeeExtractor: Vídeo encontrado via meta tag: %s", video_url[:100])
                        
                        title_meta = soup.find('meta', property='og:title')
                        title = title_meta.get('content') if title_meta else 'shopee_video'
                        
                        return {
                            'success': True,
                            'url': video_url,
                            'title': self._clean_title(title),
                            'error': None
                        }
            
            return {'success': False, 'error': 'Nenhuma meta tag de vídeo encontrada', 'url': None, 'title': None}
            
        except Exception as e:
            LOG.error("ShopeeExtractor: Erro no método meta tags: %s", e)
            return {'success': False, 'error': str(e), 'url': None, 'title': None}
    
    def _find_video_in_json(self, data, depth=0, max_depth=10):
        """Busca recursivamente por URLs de vídeo em estrutura JSON."""
        if depth > max_depth:
            return None
        
        if isinstance(data, dict):
            # Procura por chaves comuns de vídeo
            for key in ['video_url', 'videoUrl', 'video', 'url', 'src', 'source', 'file', 'stream']:
                if key in data:
                    value = data[key]
                    if isinstance(value, str) and ('mp4' in value.lower() or 'video' in value.lower()):
                        if value.startswith('http'):
                            return value
            
            # Busca recursiva
            for value in data.values():
                result = self._find_video_in_json(value, depth + 1, max_depth)
                if result:
                    return result
        
        elif isinstance(data, list):
            for item in data:
                result = self._find_video_in_json(item, depth + 1, max_depth)
                if result:
                    return result
        
        return None
    
    def _find_title_in_json(self, data, depth=0, max_depth=5):
        """Busca recursivamente por título em estrutura JSON."""
        if depth > max_depth:
            return None
        
        if isinstance(data, dict):
            for key in ['title', 'name', 'video_title', 'videoTitle']:
                if key in data and isinstance(data[key], str):
                    return data[key]
            
            for value in data.values():
                result = self._find_title_in_json(value, depth + 1, max_depth)
                if result:
                    return result
        
        elif isinstance(data, list):
            for item in data:
                result = self._find_title_in_json(item, depth + 1, max_depth)
                if result:
                    return result
        
        return None
    
    def _clean_title(self, title: str) -> str:
        """Limpa o título removendo caracteres inválidos."""
        # Remove caracteres inválidos para nome de arquivo
        title = re.sub(r'[<>:"/\\|?*]', '', title)
        title = title.strip()
        return title[:100] if title else 'shopee_video'  # Limita a 100 caracteres

# ==================== UTILITIES ====================

def is_valid_url(url: str) -> bool:
    """Valida se a string é uma URL HTTP/HTTPS válida."""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False

def is_shopee_url(url: str) -> bool:
    """Verifica se a URL é da Shopee."""
    try:
        return 'shopee' in url.lower()
    except Exception:
        return False

@contextmanager
def temp_download_dir():
    """Context manager para criar e limpar diretório temporário."""
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
    """Divide vídeo em partes usando ffmpeg. Retorna lista de caminhos."""
    os.makedirs(output_dir, exist_ok=True)
    output_pattern = os.path.join(output_dir, "part%03d.mp4")
    
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c", "copy", "-map", "0",
        "-fs", f"{SPLIT_SIZE}",
        output_pattern
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=True
        )
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
    """Verifica se o bot foi mencionado na mensagem."""
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

# ==================== PENDING MANAGEMENT ====================

def add_pending(token: str, data: dict):
    """Adiciona um pedido pendente com expiração automática."""
    # Remove mais antigo se atingir limite
    if len(PENDING) >= PENDING_MAX_SIZE:
        oldest = next(iter(PENDING))
        PENDING.pop(oldest)
        LOG.warning("PENDING cheio, removido token: %s", oldest)
    
    data["created_at"] = time.time()
    PENDING[token] = data
    
    # Agenda expiração
    asyncio.run_coroutine_threadsafe(
        _expire_pending(token),
        APP_LOOP
    )

async def _expire_pending(token: str):
    """Expira um pedido pendente após timeout."""
    await asyncio.sleep(PENDING_EXPIRE_SECONDS)
    entry = PENDING.pop(token, None)
    if entry:
        LOG.info("Token expirado: %s", token)
        # Tenta notificar o usuário
        try:
            await application.bot.edit_message_text(
                text=ERROR_MESSAGES["expired"],
                chat_id=entry["chat_id"],
                message_id=entry["confirm_msg_id"]
            )
        except Exception:
            pass  # Mensagem pode ter sido deletada

# ==================== TELEGRAM HANDLERS ====================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler do comando /start."""
    try:
        count = get_monthly_users_count()
        shopee_status = "✅ Ativo" if SHOPEE_SUPPORT else "⚠️ Limitado (instale: requests beautifulsoup4)"
        await update.message.reply_text(
            f"Olá! 👋\n\n"
            f"Me envie um link do YouTube, Shopee Video ou outro vídeo, e eu te pergunto se quer baixar.\n\n"
            f"🛍️ Suporte Shopee: {shopee_status}\n"
            f"📊 Usuários mensais: {count}"
        )
    except Exception as e:
        LOG.error("Erro no comando /start: %s", e)
        await update.message.reply_text("Erro ao processar comando. Tente novamente.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler do comando /stats."""
    try:
        count = get_monthly_users_count()
        pending_count = len(PENDING)
        shopee_status = "✅" if SHOPEE_SUPPORT else "❌"
        await update.message.reply_text(
            f"📊 **Estatísticas**\n\n"
            f"👥 Usuários mensais: {count}\n"
            f"⏳ Downloads pendentes: {pending_count}\n"
            f"🛍️ Shopee: {shopee_status}",
            parse_mode="Markdown"
        )
    except Exception as e:
        LOG.error("Erro no comando /stats: %s", e)
        await update.message.reply_text("Erro ao obter estatísticas.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler de mensagens com links."""
    try:
        if not getattr(update, "message", None) or not update.message.text:
            return

        # Track user
        try:
            update_user(update.message.from_user.id)
        except Exception as e:
            LOG.error("Erro ao atualizar usuário: %s", e)

        text = update.message.text.strip()
        chat_type = update.message.chat.type
        
        # Em grupos, só responde se mencionado
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

        # Valida URL
        if not is_valid_url(url):
            await update.message.reply_text(ERROR_MESSAGES["invalid_url"])
            return

        # Verifica se é Shopee e avisa
        is_shopee = is_shopee_url(url)
        if is_shopee and not SHOPEE_SUPPORT:
            await update.message.reply_text(
                "⚠️ Suporte completo para Shopee requer as bibliotecas: requests e beautifulsoup4\n"
                "Tentarei usar método genérico, mas pode não funcionar."
            )

        # Cria pedido pendente
        token = uuid.uuid4().hex
        emoji = "🛍️" if is_shopee else "📥"
        confirm_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"{emoji} Baixar", callback_data=f"dl:{token}"),
                    InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
                ]
            ]
        )

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
            "is_shopee": is_shopee,
        })
        
    except Exception as e:
        LOG.exception("Erro no handle_message: %s", e)
        try:
            await update.message.reply_text(ERROR_MESSAGES["unknown"])
        except Exception:
            pass

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler de callbacks dos botões."""
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
                await query.answer("⚠️ Apenas quem solicitou pode confirmar o download.", show_alert=True)
                return

            is_shopee = entry.get("is_shopee", False)
            emoji = "🛍️" if is_shopee else "🎬"
            
            try:
                await query.edit_message_text(f"Iniciando download... {emoji}")
            except Exception as e:
                LOG.error("Erro ao editar mensagem de confirmação: %s", e)

            # Cria mensagem de progresso
            progress_msg = await context.bot.send_message(
                chat_id=entry["chat_id"], 
                text="📥 Preparando download... 0%"
            )
            entry["progress_msg"] = {
                "chat_id": progress_msg.chat_id, 
                "message_id": progress_msg.message_id
            }
            
            # Inicia download em background
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

# ==================== DOWNLOAD TASK ====================

async def start_download_task(token: str):
    """Executa o download e envia arquivos."""
    entry = PENDING.get(token)
    if not entry:
        LOG.warning("Token não encontrado: %s", token)
        return
    
    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry["progress_msg"]
    is_shopee = entry.get("is_shopee", False)
    
    if not pm:
        LOG.warning("progress_msg não encontrado para token: %s", token)
        return

    # Inicia watchdog
    watchdog_task = asyncio.create_task(_watchdog(token, WATCHDOG_TIMEOUT))
    
    try:
        with temp_download_dir() as tmpdir:
            if is_shopee and SHOPEE_SUPPORT:
                # Usa extrator customizado para Shopee
                await _download_shopee(token, url, tmpdir, chat_id, pm)
            else:
                # Usa yt-dlp padrão
                await _do_download(token, url, tmpdir, chat_id, pm, is_shopee)
    except asyncio.CancelledError:
        LOG.info("Download cancelado pelo watchdog: %s", token)
        await _notify_error(pm, "timeout")
    except Exception as e:
        LOG.exception("Erro no download: %s", e)
        await _notify_error(pm, "unknown")
    finally:
        watchdog_task.cancel()
        PENDING.pop(token, None)

async def _download_shopee(token: str, url: str, tmpdir: str, chat_id: int, pm: dict):
    """Download usando extrator customizado Shopee."""
    LOG.info("Usando ShopeeExtractor para: %s", url)
    
    try:
        # Atualiza status
        await application.bot.edit_message_text(
            text="🛍️ Extraindo vídeo da Shopee...",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception:
        pass
    
    # Extrai URL do vídeo
    extractor = ShopeeExtractor(url)
    result = await asyncio.to_thread(extractor.extract_video_url)
    
    if not result['success']:
        LOG.error("ShopeeExtractor falhou: %s", result['error'])
        await _notify_error(pm, "shopee_extract_error")
        return
    
    video_url = result['url']
    title = result['title']
    
    LOG.info("ShopeeExtractor sucesso! URL: %s, Título: %s", video_url[:100], title)
    
    try:
        await application.bot.edit_message_text(
            text="📥 Baixando vídeo da Shopee... 0%",
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception:
        pass
    
    # Baixa o vídeo direto
    output_path = os.path.join(tmpdir, f"{title}.mp4")
    
    try:
        response = await asyncio.to_thread(
            lambda: requests.get(video_url, stream=True, timeout=120, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://shopee.com.br/'
            })
        )
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        last_percent = -1
        
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if total_size:
                        percent = int(downloaded * 100 / total_size)
                        if percent != last_percent and percent % 10 == 0:
                            last_percent = percent
                            blocks = int(percent / 5)
                            bar = "█" * blocks + "─" * (20 - blocks)
                            try:
                                await application.bot.edit_message_text(
                                    text=f"🛍️ Baixando: {percent}% [{bar}]",
                                    chat_id=pm["chat_id"],
                                    message_id=pm["message_id"]
                                )
                            except Exception:
                                pass
        
        LOG.info("Download Shopee concluído: %s", output_path)
        
        # Atualiza mensagem
        try:
            await application.bot.edit_message_text(
                text="✅ Download concluído, enviando...",
                chat_id=pm["chat_id"],
                message_id=pm["message_id"]
            )
        except Exception:
            pass
        
        # Envia o arquivo
        await _send_video_files([output_path], chat_id, pm)
        
    except Exception as e:
        LOG.exception("Erro ao baixar vídeo Shopee: %s", e)
        await _notify_error(pm, "network_error")
        return

async def _watchdog(token: str, timeout: int):
    """Cancela download após timeout."""
    await asyncio.sleep(timeout)
    entry = PENDING.pop(token, None)
    if entry and entry.get("progress_msg"):
        pm = entry["progress_msg"]
        await _notify_error(pm, "timeout")
    LOG.warning("Watchdog timeout para token: %s", token)

async def _notify_error(pm: dict, error_type: str):
    """Notifica usuário sobre erro."""
    try:
        message = ERROR_MESSAGES.get(error_type, ERROR_MESSAGES["unknown"])
        await application.bot.edit_message_text(
            text=message,
            chat_id=pm["chat_id"],
            message_id=pm["message_id"]
        )
    except Exception as e:
        LOG.error("Erro ao notificar erro: %s", e)

async def _do_download(token: str, url: str, tmpdir: str, chat_id: int, pm: dict, is_shopee: bool = False):
    """Realiza o download usando yt-dlp."""
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
                    if percent != last_percent and percent % 5 == 0:  # Atualiza a cada 5%
                        last_percent = percent
                        blocks = int(percent / 5)
                        bar = "█" * blocks + "─" * (20 - blocks)
                        emoji = "🛍️" if is_shopee else "📥"
                        text = f"{emoji} Baixando: {percent}% [{bar}]"
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
    
    # Configurações especiais para Shopee (fallback se extrator customizado falhar)
    if is_shopee:
        ydl_opts.update({
            "nocheckcertificate": True,
            "extract_flat": False,
            "default_search": "auto",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
    
    if COOKIE_PATH:
        ydl_opts["cookiefile"] = COOKIE_PATH

    # Download
    try:
        await asyncio.to_thread(lambda: _run_ydl(ydl_opts, [url]))
    except Exception as e:
        LOG.exception("Erro no yt-dlp: %s", e)
        if is_shopee:
            await _notify_error(pm, "shopee_extract_error")
        else:
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

    await _send_video_files(arquivos, chat_id, pm)

async def _send_video_files(arquivos: list, chat_id: int, pm: dict):
    """Envia arquivos de vídeo, dividindo se necessário."""
    for path in arquivos:
        try:
            tamanho = os.path.getsize(path)
            
            if tamanho > MAX_FILE_SIZE:
                # Dividir arquivo
                partes_dir = os.path.join(os.path.dirname(path), "partes")
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
                # Enviar arquivo diretamente
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
    """Executa yt-dlp de forma síncrona."""
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

# ==================== HANDLERS REGISTRATION ====================

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# ==================== FLASK ROUTES ====================

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Webhook do Telegram."""
    try:
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    except Exception as e:
        LOG.exception("Falha ao processar webhook: %s", e)
    return "ok"

@app.route("/")
def index():
    """Rota principal."""
    return "Bot rodando ✅"

@app.route("/health")
def health():
    """Health check endpoint."""
    checks = {
        "bot": "ok",
        "db": "ok",
        "shopee_support": SHOPEE_SUPPORT,
        "pending_count": len(PENDING),
        "timestamp": time.time()
    }
    
    # Verifica DB
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DB_FILE, timeout=5)
            conn.execute("SELECT 1")
            conn.close()
    except Exception as e:
        checks["db"] = f"error: {str(e)}"
        LOG.error("Health check DB falhou: %s", e)
    
    # Verifica bot
    try:
        bot_info = application.bot.get_me()
        checks["bot_username"] = bot_info.username
    except Exception as e:
        checks["bot"] = f"error: {str(e)}"
        LOG.error("Health check bot falhou: %s", e)
    
    status = 200 if checks["bot"] == "ok" and checks["db"] == "ok" else 503
    return checks, status

# ==================== MAIN ====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    LOG.info("Iniciando servidor Flask na porta %d", port)
    LOG.info("Suporte Shopee: %s", "Ativo" if SHOPEE_SUPPORT else "Limitado")
    app.run(host="0.0.0.0", port=port)
