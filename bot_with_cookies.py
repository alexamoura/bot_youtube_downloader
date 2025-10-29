#!/usr/bin/env python3
"""
bot_with_cookies.py - Versão Melhorada

Telegram bot (webhook) que:
- detecta links enviados diretamente ou em grupo quando mencionado (@SeuBot + link),
- pergunta "quer baixar?" com botão,
- ao confirmar, inicia o download e mostra uma barra de progresso atualizada,
- envia partes se necessário (ffmpeg) e mostra mensagem final.
- track de usuários mensais via SQLite.

Melhorias implementadas:
- Cleanup automático de arquivos temporários
- Proteção contra race conditions no SQLite
- Watchdog timeout para downloads travados
- Validação de URLs
- Expiração automática de requests pendentes
- Tratamento de erros robusto
- Mensagens de erro amigáveis
- Health check endpoint

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
from collections import OrderedDict
from contextlib import contextmanager
from urllib.parse import urlparse
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
WATCHDOG_TIMEOUT = 180  # 3 minutos
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

# ==================== UTILITIES ====================

def is_valid_url(url: str) -> bool:
    """Valida se a string é uma URL HTTP/HTTPS válida."""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
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
        await update.message.reply_text(
            f"Olá! 👋\n\n"
            f"Me envie um link do YouTube ou outro vídeo, e eu te pergunto se quer baixar.\n\n"
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
        await update.message.reply_text(
            f"📊 **Estatísticas**\n\n"
            f"👥 Usuários mensais: {count}\n"
            f"⏳ Downloads pendentes: {pending_count}",
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

        # Cria pedido pendente
        token = uuid.uuid4().hex
        confirm_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
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

            try:
                await query.edit_message_text("Iniciando download... 🎬")
            except Exception as e:
                LOG.error("Erro ao editar mensagem de confirmação: %s", e)

            # Cria mensagem de progresso
            progress_msg = await context.bot.send_message(
                chat_id=entry["chat_id"], 
                text="📥 Baixando: 0% [────────────────────]"
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
    
    if not pm:
        LOG.warning("progress_msg não encontrado para token: %s", token)
        return

    # Inicia watchdog
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

async def _do_download(token: str, url: str, tmpdir: str, chat_id: int, pm: dict):
    """Realiza o download e envio."""
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
        "format": "bestvideo[height<=720]+bestaudio/best/best",
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 1,
        "force_ipv4": True,
        "socket_timeout": 30,
        "http_chunk_size": 1048576,
        "retries": 20,
        "fragment_retries": 20,
    }
    
    if COOKIE_PATH:
        ydl_opts["cookiefile"] = COOKIE_PATH

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
                # Dividir arquivo
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
    app.run(host="0.0.0.0", port=port)
