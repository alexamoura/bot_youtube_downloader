#!/usr/bin/env python3
"""
bot_with_cookies.py (versão com buscador de preços + comparar)

Funcionalidades:
- Download de vídeos (fluxo já existente)
- /buscar <produto> -> busca por scraping em Shopee, Mercado Livre e Amazon
- /comparar <produto> -> busca e indica o menor preço entre as lojas
- fallback automático quando seletores mudam
- comandos admin para ajustar seletores persistentes
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
import yt_dlp
import json
from typing import List, Tuple, Optional

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

import requests
from bs4 import BeautifulSoup

# ------------------------- Config & Logging -------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger("ytbot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido. Defina o secret TELEGRAM_BOT_TOKEN e redeploy.")
    sys.exit(1)
LOG.info("TELEGRAM_BOT_TOKEN presente (len=%d).", len(TOKEN))

# Admin (opcional)
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
if ADMIN_USER_ID:
    try:
        ADMIN_USER_ID = int(ADMIN_USER_ID)
        LOG.info("ADMIN_USER_ID definido: %d", ADMIN_USER_ID)
    except Exception:
        LOG.warning("ADMIN_USER_ID inválido. Ignorando.")
        ADMIN_USER_ID = None

# Flask app
app = Flask(__name__)

# Telegram app
try:
    application = ApplicationBuilder().token(TOKEN).build()
except Exception:
    LOG.exception("Erro ao construir ApplicationBuilder().")
    sys.exit(1)

# Async loop background
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
    LOG.info("Application inicializada no loop de background.")
except Exception:
    LOG.exception("Falha ao inicializar a Application no loop de background.")
    sys.exit(1)

URL_RE = re.compile(r"(https?://[^\s]+)")
PENDING = {}  # token -> metadata (in-memory)

# ------------------------- Cookies (opcional) -------------------------
def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.info("Nenhuma variável %s encontrada — rodando sem cookies.", env_var)
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:
        LOG.exception("Falha ao decodificar %s.", env_var)
        return None

    fd, path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except Exception:
        LOG.exception("Falha ao escrever cookies em %s", path)
        return None

    LOG.info("Cookies gravados em %s", path)
    return path


COOKIE_PATH = prepare_cookies_from_env()

# ------------------------- Mention helper -------------------------
def is_bot_mentioned(update: Update) -> bool:
    try:
        bot_username = application.bot.username
        bot_id = application.bot.id
    except Exception:
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
                    if getattr(ent, "user", None) and getattr(ent.user, "id", None) == bot_id:
                        return True
        if msg.text and f"@{bot_username}" in msg.text:
            return True

    if getattr(msg, "entities", None):
        for ent in msg.entities:
            if getattr(ent, "type", "") == "text_mention":
                if getattr(ent, "user", None) and getattr(ent.user, "id", None) == bot_id:
                    return True
    return False

# ------------------------- Selectors persistence -------------------------
SELECTORS_FILE = os.path.join(os.path.dirname(__file__), "selectors.json")
DEFAULT_SELECTORS = {
    "shopee": {
        "product_container": "div.shopee-search-item-result__item",
        "name": "div._10Wbs-",
        "price": "span._29R_un"
    },
    "mercadolivre": {
        "product_container": "li.ui-search-layout__item",
        "name": "h2.ui-search-item__title",
        "price": "span.price-tag-fraction"
    },
    "amazon": {
        "product_container": "div.s-main-slot div[data-asin]",
        "name": "h2 a.a-link-normal span",
        "price": "span.a-price-whole"
    }
}


def load_selectors():
    try:
        if os.path.exists(SELECTORS_FILE):
            with open(SELECTORS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                LOG.info("Selectors carregados de %s", SELECTORS_FILE)
                return data
    except Exception:
        LOG.exception("Falha ao carregar selectors.json")
    return DEFAULT_SELECTORS.copy()


def save_selectors(selectors):
    try:
        with open(SELECTORS_FILE, "w", encoding="utf-8") as f:
            json.dump(selectors, f, ensure_ascii=False, indent=2)
        LOG.info("Selectors salvos em %s", SELECTORS_FILE)
    except Exception:
        LOG.exception("Falha ao salvar selectors.json")


SELECTORS = load_selectors()

# ------------------------- Scraping helpers -------------------------
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"}

def auto_detect_name_price(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    keywords = ["iphone", "samsung", "notebook", "celular", "fone", "smart", "tv", "geladeira", "monitor", "notebook"]
    name_tag = None
    price_tag = None
    for tag in soup.find_all(True, limit=400):
        txt = (tag.get_text(separator=" ", strip=True) or "").lower()
        if any(k in txt for k in keywords) and 10 < len(txt) < 200:
            name_tag = tag.name
            break
    for tag in soup.find_all(True, limit=400):
        txt = (tag.get_text(strip=True) or "")
        if "R$" in txt and len(txt) < 40:
            price_tag = tag.name
            break
    return name_tag, price_tag

def clean_price(text: str) -> str:
    return text.strip().replace("\n", " ").replace("\t", " ").strip()

def parse_price_to_float(price_str: str) -> Optional[float]:
    """
    Tenta extrair valor numérico de uma string que pode conter 'R$' e formatação BR.
    Retorna float (ex: 1234.56) ou None se não conseguiu.
    """
    if not price_str:
        return None
    # remover espaços e 'R$'
    s = price_str.replace("R$", "").replace("r$", "").strip()
    # extrai a primeira sequência de dígitos, pontos e vírgulas
    m = re.search(r"[\d\.,]+", s)
    if not m:
        return None
    num = m.group(0)
    # normalizar: remover pontos de milhar, trocar vírgula por ponto
    if num.count(",") > 0 and num.count(".") > 0:
        # ex: '1.234,56' -> remove pontos
        num = num.replace(".", "")
        num = num.replace(",", ".")
    else:
        # se só contém virgula, troca por ponto
        num = num.replace(",", ".")
    try:
        return float(num)
    except Exception:
        return None

# ------------------- Site-specific scrapers -------------------
def scrape_shopee(query: str) -> List[Tuple[str, str, str]]:
    url = f"https://shopee.com.br/search?keyword={requests.utils.requote_uri(query)}"
    LOG.info("Scraping Shopee: %s", url)
    r = requests.get(url, headers=HEADERS, timeout=12)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    sel = SELECTORS.get("shopee", {})
    container = sel.get("product_container")
    name_sel = sel.get("name")
    price_sel = sel.get("price")

    results = []
    items = soup.select(container) if container else []
    if not items:
        LOG.warning("Shopee: container vazio — tentando autodetecção")
        name_tag, price_tag = auto_detect_name_price(soup)
        if not name_tag:
            LOG.warning("Shopee autodetec falhou")
            return []
        for tag in soup.find_all(name_tag)[:6]:
            name = tag.get_text(strip=True)
            ptag = tag.find_next(price_tag) if price_tag else None
            price = ptag.get_text(strip=True) if ptag else "N/D"
            results.append((name, clean_price(price), url))
        return results[:3]

    for item in items[:6]:
        nome = item.select_one(name_sel).get_text(strip=True) if item.select_one(name_sel) else "N/D"
        preco = item.select_one(price_sel).get_text(strip=True) if item.select_one(price_sel) else "N/D"
        link_tag = item.select_one("a")
        link = f"https://shopee.com.br{link_tag['href']}" if link_tag and link_tag.get("href") else url
        results.append((nome, clean_price(preco), link))
    return results[:3]

def scrape_mercadolivre(query: str) -> List[Tuple[str, str, str]]:
    url = f"https://lista.mercadolivre.com.br/{requests.utils.requote_uri(query)}"
    LOG.info("Scraping MercadoLivre: %s", url)
    r = requests.get(url, headers=HEADERS, timeout=12)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    sel = SELECTORS.get("mercadolivre", {})
    container = sel.get("product_container")
    name_sel = sel.get("name")
    price_sel = sel.get("price")

    results = []
    items = soup.select(container) if container else []
    if not items:
        LOG.warning("MercadoLivre: container vazio — tentando autodetecção")
        name_tag, price_tag = auto_detect_name_price(soup)
        if not name_tag:
            LOG.warning("MercadoLivre autodetec falhou")
            return []
        for tag in soup.find_all(name_tag)[:6]:
            name = tag.get_text(strip=True)
            ptag = tag.find_next(price_tag) if price_tag else None
            price = ptag.get_text(strip=True) if ptag else "N/D"
            a = tag.find_parent("a")
            link = a["href"] if a and a.get("href") else url
            results.append((name, clean_price(price), link))
        return results[:3]

    for item in items[:6]:
        nome = item.select_one(name_sel).get_text(strip=True) if item.select_one(name_sel) else "N/D"
        preco = item.select_one(price_sel).get_text(strip=True) if item.select_one(price_sel) else "N/D"
        link_tag = item.select_one("a")
        link = link_tag["href"] if link_tag and link_tag.get("href") else url
        results.append((nome, clean_price(preco), link))
    return results[:3]

def scrape_amazon(query: str) -> List[Tuple[str, str, str]]:
    url = f"https://www.amazon.com.br/s?k={requests.utils.requote_uri(query)}"
    LOG.info("Scraping Amazon: %s", url)
    r = requests.get(url, headers=HEADERS, timeout=12)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    sel = SELECTORS.get("amazon", {})
    container = sel.get("product_container")
    name_sel = sel.get("name")
    price_sel = sel.get("price")

    results = []
    items = soup.select(container) if container else []
    if not items:
        LOG.warning("Amazon: container vazio — tentando autodetecção")
        name_tag, price_tag = auto_detect_name_price(soup)
        if not name_tag:
            LOG.warning("Amazon autodetec falhou")
            return []
        for tag in soup.find_all(name_tag)[:6]:
            name = tag.get_text(strip=True)
            ptag = tag.find_next(price_tag) if price_tag else None
            price = ptag.get_text(strip=True) if ptag else "N/D"
            a = tag.find_parent("a")
            link = f"https://www.amazon.com.br{a['href']}" if a and a.get("href") else url
            results.append((name, clean_price(price), link))
        return results[:3]

    for item in items[:8]:
        nome = "N/D"
        if item.select_one(name_sel):
            nome = item.select_one(name_sel).get_text(strip=True)
        else:
            a = item.select_one("a.a-link-normal")
            if a:
                nome = a.get_text(strip=True) or nome
        preco = "N/D"
        if item.select_one(price_sel):
            preco = item.select_one(price_sel).get_text(strip=True)
        else:
            p_whole = item.select_one("span.a-price-whole")
            p_frac = item.select_one("span.a-price-fraction")
            if p_whole:
                preco = (p_whole.get_text(strip=True) + ("," + p_frac.get_text(strip=True) if p_frac else ""))
        a_tag = item.select_one("a.a-link-normal")
        link = f"https://www.amazon.com.br{a_tag['href']}" if a_tag and a_tag.get("href") else url
        results.append((nome, clean_price(preco), link))
    return results[:3]

# ------------------- Unified search -------------------
def search_all_sites(query: str) -> dict:
    results = {}
    try:
        results["shopee"] = scrape_shopee(query)
    except Exception:
        LOG.exception("Erro scraping Shopee")
        results["shopee"] = []

    try:
        results["mercadolivre"] = scrape_mercadolivre(query)
    except Exception:
        LOG.exception("Erro scraping MercadoLivre")
        results["mercadolivre"] = []

    try:
        results["amazon"] = scrape_amazon(query)
    except Exception:
        LOG.exception("Erro scraping Amazon")
        results["amazon"] = []

    return results

# ------------------- Comparison helper -------------------
def find_lowest_price(search_results: dict) -> Optional[Tuple[str, str, float, str]]:
    """
    Recebe dict {site: [(name, price_str, link), ...], ...}
    Retorna (site, name, price_float, link) do menor preço, ou None.
    """
    best = None  # tuple (site, name, price_float, link)
    for site, items in search_results.items():
        for name, price_str, link in items:
            price_val = parse_price_to_float(price_str)
            if price_val is None:
                continue
            if best is None or price_val < best[2]:
                best = (site, name, price_val, link)
    return best

# ------------------- Bot Handlers (start, messages, confirm) -------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        payload = context.args[0]
        try:
            padding = "=" * (-len(payload) % 4)
            url = base64.urlsafe_b64decode(payload + padding).decode()
        except Exception:
            await update.message.reply_text("Payload inválido.")
            return

        token = uuid.uuid4().hex
        confirm_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
                    InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
                ]
            ]
        )
        confirm_msg = await update.message.reply_text(f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard)
        PENDING[token] = {
            "url": url,
            "chat_id": update.message.chat_id,
            "from_user_id": update.message.from_user.id,
            "confirm_msg_id": confirm_msg.message_id,
            "progress_msg": None,
        }
        return

    await update.message.reply_text(
        "Olá! Me envie um link do YouTube (ou mencione-me com @seubot + link) e eu te pergunto se quer baixar.\n\n"
        "Use /buscar <produto> para procurar preços nas lojas ou /comparar <produto> para ver o menor preço."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not getattr(update, "message", None) or not update.message.text:
        return

    text = update.message.text.strip()
    chat_type = update.message.chat.type

    if chat_type != "private":
        if not is_bot_mentioned(update):
            return

    url = None
    if getattr(update.message, "entities", None):
        for ent in update.message.entities:
            if ent.type in ("url", "text_link"):
                if getattr(ent, "url", None):
                    url = ent.url
                else:
                    try:
                        url = update.message.text[ent.offset : ent.offset + ent.length]
                    except Exception:
                        url = None
                break

    if not url:
        m = URL_RE.search(text)
        if m:
            url = m.group(1)

    if not url:
        if chat_type != "private" and is_bot_mentioned(update):
            try:
                await update.message.reply_text("Envie o link do vídeo junto com a menção, por exemplo: @MeuBot https://...")
            except Exception:
                pass
        return

    token = uuid.uuid4().hex
    confirm_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 Baixar", callback_data=f"dl:{token}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
            ]
        ]
    )

    try:
        confirm_msg = await update.message.reply_text(f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard)
    except Exception:
        confirm_msg = await context.bot.send_message(chat_id=update.message.chat_id, text=f"Você quer baixar este link?\n{url}", reply_markup=confirm_keyboard)

    PENDING[token] = {
        "url": url,
        "chat_id": update.message.chat_id,
        "from_user_id": update.message.from_user.id,
        "confirm_msg_id": confirm_msg.message_id,
        "progress_msg": None,
    }

async def callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("dl:"):
        token = data.split("dl:", 1)[1]
        entry = PENDING.get(token)
        if not entry:
            await query.edit_message_text("Esse pedido expirou ou é inválido.")
            return
        if query.from_user.id != entry["from_user_id"]:
            await query.edit_message_text("Apenas quem solicitou pode confirmar o download.")
            return

        try:
            await query.edit_message_text("Iniciando download... 🎬")
        except Exception:
            pass

        progress_msg = await context.bot.send_message(chat_id=entry["chat_id"], text="📥 Baixando: 0% [────────────────────]")
        entry["progress_msg"] = {"chat_id": progress_msg.chat_id, "message_id": progress_msg.message_id}

        asyncio.run_coroutine_threadsafe(start_download_task(token), APP_LOOP)

    elif data.startswith("cancel:"):
        token = data.split("cancel:", 1)[1]
        entry = PENDING.pop(token, None)
        if not entry:
            await query.edit_message_text("Cancelamento: pedido já expirou.")
            return
        await query.edit_message_text("Cancelado ✅")

# ------------------- Download task & helpers (mesmo comportamento) -------------------
async def start_download_task(token: str):
    entry = PENDING.get(token)
    if not entry:
        LOG.info("start_download_task: token não encontrado")
        return

    url = entry["url"]
    chat_id = entry["chat_id"]
    pm = entry["progress_msg"]
    if not pm:
        LOG.info("start_download_task: progress_msg não encontrado")
        return

    tmpdir = tempfile.mkdtemp(prefix="ytbot_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

    last_percent = -1
    last_update_ts = time.time()
    WATCHDOG_TIMEOUT = 180  # segundos sem progresso para notificar

    def progress_hook(d):
        nonlocal last_percent, last_update_ts
        try:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    percent = int(downloaded * 100 / total)
                    if percent != last_percent:
                        last_percent = percent
                        last_update_ts = time.time()
                        blocks = int(percent / 5)
                        bar = "█" * blocks + "─" * (20 - blocks)
                        text = f"📥 Baixando: {percent}% [{bar}]"
                        try:
                            asyncio.run_coroutine_threadsafe(
                                application.bot.edit_message_text(
                                    text=text, chat_id=pm["chat_id"], message_id=pm["message_id"]
                                ),
                                APP_LOOP,
                            )
                        except Exception:
                            pass
            elif status == "finished":
                last_update_ts = time.time()
                try:
                    asyncio.run_coroutine_threadsafe(
                        application.bot.edit_message_text(
                            text="✅ Download concluído, processando o envio...", chat_id=pm["chat_id"], message_id=pm["message_id"]
                        ),
                        APP_LOOP,
                    )
                except Exception:
                    pass
        except Exception:
            LOG.exception("Erro no progress_hook")

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
        **({"cookiefile": COOKIE_PATH} if COOKIE_PATH else {}),
    }

    try:
        await asyncio.to_thread(lambda: _run_ydl(ydl_opts, [url]))
    except Exception as e:
        LOG.exception("Erro no yt-dlp: %s", e)
        try:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text=f"⚠️ Erro no download: {str(e)}", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
        except Exception:
            pass
        PENDING.pop(token, None)
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass
        return

    if time.time() - last_update_ts > WATCHDOG_TIMEOUT:
        try:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="⚠️ Download travou (sem progresso). O yt-dlp continuará tentando; se quiser, tente novamente mais tarde.",
                    chat_id=pm["chat_id"],
                    message_id=pm["message_id"],
                ),
                APP_LOOP,
            )
        except Exception:
            pass

    arquivos = [f for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))]
    if not arquivos:
        try:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="⚠️ Nenhum arquivo gerado.", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
        except Exception:
            pass
        PENDING.pop(token, None)
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass
        return

    sent_any = False
    try:
        for f in arquivos:
            path = os.path.join(tmpdir, f)
            tamanho = os.path.getsize(path)
            if tamanho > 50 * 1024 * 1024:
                partes_dir = os.path.join(tmpdir, "partes")
                os.makedirs(partes_dir, exist_ok=True)
                cmd = f'ffmpeg -y -i "{path}" -c copy -map 0 -fs 45M "{partes_dir}/part%03d.mp4"'
                LOG.info("Split: %s", cmd)
                os.system(cmd)
                partes = sorted(os.listdir(partes_dir))
                for p in partes:
                    ppath = os.path.join(partes_dir, p)
                    try:
                        with open(ppath, "rb") as fh:
                            await application.bot.send_video(chat_id=chat_id, video=fh)
                        sent_any = True
                    except Exception:
                        LOG.exception("Erro ao enviar parte %s", ppath)
            else:
                try:
                    with open(path, "rb") as fh:
                        await application.bot.send_video(chat_id=chat_id, video=fh)
                    sent_any = True
                except Exception:
                    LOG.exception("Erro ao enviar arquivo %s", path)
    finally:
        try:
            for root, dirs, files in os.walk(tmpdir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(tmpdir)
        except Exception:
            pass

    try:
        if sent_any:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="✅ Download finalizado e enviado!", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
        else:
            asyncio.run_coroutine_threadsafe(
                application.bot.edit_message_text(
                    text="⚠️ Falha ao enviar o arquivo.", chat_id=pm["chat_id"], message_id=pm["message_id"]
                ),
                APP_LOOP,
            )
    except Exception:
        pass

    PENDING.pop(token, None)

def _run_ydl(options, urls):
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)

# ------------------- Price commands: /buscar and /comparar -------------------
async def buscar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use assim: `/buscar nome do produto`", parse_mode="Markdown")
        return

    produto = " ".join(context.args).strip()
    await update.message.reply_text(f"🔎 Buscando preços para: *{produto}* ...", parse_mode="Markdown")

    try:
        results = await asyncio.to_thread(search_all_sites, produto)
    except Exception:
        LOG.exception("Erro ao executar busca")
        await update.message.reply_text("⚠️ Erro ao buscar. Tente novamente mais tarde.")
        return

    msgs = []
    for site, items in results.items():
        if not items:
            msgs.append(f"🔸 *{site.title()}*: sem resultados / erro.")
            continue
        block = f"🔸 *{site.title()}*\n"
        for nome, preco, link in items:
            block += f"• {nome}\n  💰 {preco}\n  🔗 [Ver]({link})\n"
        msgs.append(block)

    final_msg = "\n\n".join(msgs)
    await update.message.reply_text(final_msg, parse_mode="Markdown", disable_web_page_preview=True)

async def comparar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use assim: `/comparar nome do produto`", parse_mode="Markdown")
        return

    produto = " ".join(context.args).strip()
    status_msg = await update.message.reply_text(f"🔎 Comparando preços para: *{produto}* ...", parse_mode="Markdown")

    try:
        results = await asyncio.to_thread(search_all_sites, produto)
    except Exception:
        LOG.exception("Erro ao executar busca para comparar")
        await status_msg.edit_text("⚠️ Erro ao buscar. Tente novamente mais tarde.")
        return

    best = find_lowest_price(results)
    if not best:
        await status_msg.edit_text("😕 Não foi possível encontrar preços válidos para comparar.")
        return

    site, name, price_val, link = best
    price_str = f"R$ {price_val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")  # formata BR
    reply = (
        f"✅ *Melhor preço encontrado:*\n\n"
        f"🏷️ {name}\n"
        f"🏪 *{site.title()}*\n"
        f"💰 *{price_str}*\n"
        f"🔗 [Ver na loja]({link})"
    )
    await status_msg.edit_text(reply, parse_mode="Markdown", disable_web_page_preview=True)

# ------------------- Admin commands: set/test/get selectors -------------------
def is_admin(user_id: int) -> bool:
    if ADMIN_USER_ID:
        return user_id == ADMIN_USER_ID
    return True

async def setselectors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Apenas administradores podem usar este comando.")
        return

    if len(context.args) < 4:
        await update.message.reply_text("Use: /setselectors <site> <product_container> <name_selector> <price_selector>")
        return

    site = context.args[0].lower()
    product_container = context.args[1]
    name_sel = context.args[2]
    price_sel = context.args[3]

    SELECTORS.setdefault(site, {})
    SELECTORS[site]["product_container"] = product_container
    SELECTORS[site]["name"] = name_sel
    SELECTORS[site]["price"] = price_sel
    save_selectors(SELECTORS)
    await update.message.reply_text(f"Selectors atualizados para *{site}*.", parse_mode="Markdown")

async def testselectors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Apenas administradores podem usar este comando.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Use: /testselectors <site> <termo>")
        return

    site = context.args[0].lower()
    termo = " ".join(context.args[1:]).strip()
    await update.message.reply_text(f"Testando selectors para *{site}* com termo `{termo}` ...", parse_mode="Markdown")

    func_map = {
        "shopee": scrape_shopee,
        "mercadolivre": scrape_mercadolivre,
        "amazon": scrape_amazon
    }
    func = func_map.get(site)
    if not func:
        await update.message.reply_text("Site desconhecido. Opções: shopee, mercadolivre, amazon")
        return

    try:
        results = await asyncio.to_thread(func, termo)
    except Exception:
        LOG.exception("Erro no testselectors")
        await update.message.reply_text("Erro ao testar selectors (ver logs).")
        return

    if not results:
        await update.message.reply_text("Nenhum resultado com os selectors atuais (ou erro). Verifique logs.")
        return

    msg = f"Resultados (teste) para *{site}*:\n\n"
    for nome, preco, link in results:
        msg += f"• {nome}\n  💰 {preco}\n  🔗 {link}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def getselectors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Apenas administradores podem usar este comando.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Use: /getselectors <site>")
        return

    site = context.args[0].lower()
    s = SELECTORS.get(site)
    if not s:
        await update.message.reply_text("Site não configurado.")
        return

    pretty = json.dumps(s, ensure_ascii=False, indent=2)
    await update.message.reply_text(f"Selectors para *{site}*:\n<pre>{pretty}</pre>", parse_mode="HTML")

# ------------------- Handlers registration -------------------
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^(dl:|cancel:)"))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

application.add_handler(CommandHandler("buscar", buscar_cmd))
application.add_handler(CommandHandler("comparar", comparar_cmd))
application.add_handler(CommandHandler("setselectors", setselectors_cmd))
application.add_handler(CommandHandler("testselectors", testselectors_cmd))
application.add_handler(CommandHandler("getselectors", getselectors_cmd))

# ------------------- Webhook endpoint -------------------
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True)
    update = Update.de_json(update_data, application.bot)
    try:
        asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    except Exception:
        LOG.exception("Falha ao agendar process_update")
    return "ok"

@app.route("/")
def index():
    return "Bot rodando (com buscador e comparar)"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
