#!/usr/bin/env python3
"""
bot_with_cookies_and_price.py

Telegram bot (webhook) que:
- detecta links enviados diretamente ou em grupo quando mencionado (@SeuBot + link),
- pergunta "quer baixar?" com botão,
- ao confirmar, inicia o download e mostra uma barra de progresso atualizada,
- envia partes se necessário (ffmpeg) e mostra mensagem final,
- permite busca de preços em sites populares com scraping.

Requisitos:
- TELEGRAM_BOT_TOKEN (env)
- YT_COOKIES_B64 (opcional; base64 do cookies.txt em formato Netscape)
- pip install requests bs4
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
import requests
from bs4 import BeautifulSoup

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

# Logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger("ytbot")

# Token
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    LOG.error("TELEGRAM_BOT_TOKEN não definido.")
    sys.exit(1)

# Flask app
app = Flask(__name__)

# Construir aplicação telegram
application = ApplicationBuilder().token(TOKEN).build()

# Loop asyncio persistente
APP_LOOP = asyncio.new_event_loop()
threading.Thread(target=lambda: APP_LOOP.run_forever(), daemon=True).start()

URL_RE = re.compile(r"(https?://[^\s]+)")
PENDING = {}

# ---------- Helpers ----------
def prepare_cookies_from_env(env_var="YT_COOKIES_B64"):
    b64 = os.environ.get(env_var)
    if not b64:
        return None
    raw = base64.b64decode(b64)
    fd, path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(raw)
    return path

COOKIE_PATH = prepare_cookies_from_env()

# ---------- Handlers de download (mantido igual ao seu código anterior) ----------
# ... Aqui você mantém todo o código do download de vídeos sem alterações ...

# ---------- Handlers de busca de preço ----------
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
}

async def buscar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use /buscar <produto> para pesquisar preços.")
        return

    query = ' '.join(context.args)
    LOG.info(f"Buscando produto: {query}")

    results = await asyncio.to_thread(search_all_sites, query)

    # Montar mensagem e quebrar se > 4000 caracteres
    final_msg = ''
    for site, items in results.items():
        final_msg += f'*{site.title()}*:\n'
        for i, item in enumerate(items[:5]):  # só top 5 por site
            final_msg += f'{i+1}. {item}\n'
        final_msg += '\n'

    CHUNK_SIZE = 4000
    for i in range(0, len(final_msg), CHUNK_SIZE):
        await update.message.reply_text(final_msg[i:i+CHUNK_SIZE], parse_mode='Markdown', disable_web_page_preview=True)

# ---------- Funções de scraping ----------
def search_all_sites(query):
    results = {}
    try:
        results['amazon'] = scrape_amazon(query)
    except Exception as e:
        LOG.error(f"Erro scraping Amazon: {e}")
        results['amazon'] = ['Erro ao buscar']

    try:
        results['buscape'] = scrape_buscape(query)
    except Exception as e:
        LOG.error(f"Erro scraping Buscapé: {e}")
        results['buscape'] = ['Erro ao buscar']

    try:
        results['magazineluiza'] = scrape_magalu(query)
    except Exception as e:
        LOG.error(f"Erro scraping Magalu: {e}")
        results['magazineluiza'] = ['Erro ao buscar']

    return results

# Scrapers simples com headers

def scrape_amazon(query):
    url = f'https://www.amazon.com.br/s?k={query.replace(" ", "+")}'
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    items = []
    for div in soup.select('div.s-result-item')[:5]:
        title = div.select_one('h2 a span')
        price = div.select_one('span.a-price span.a-offscreen')
        if title:
            items.append(f'{title.text} - {price.text if price else "Preço não disponível"}')
    return items or ['Nenhum resultado']

def scrape_buscape(query):
    url = f'https://www.buscape.com.br/search?q={query.replace(" ", "+")}'
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    items = []
    for div in soup.select('div.SearchCard')[:5]:
        title = div.select_one('h2.SearchCard_Title')
        price = div.select_one('span.SearchCard_Price')
        if title:
            items.append(f'{title.text.strip()} - {price.text.strip() if price else "Preço não disponível"}')
    return items or ['Nenhum resultado']

def scrape_magalu(query):
    url = f'https://www.magazineluiza.com.br/busca/{query.replace(" ", "-")}/'
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    items = []
    for div in soup.select('div.productCard')[:5]:
        title = div.select_one('h3.productCardTitle')
        price = div.select_one('span.productCardPrice')
        if title:
            items.append(f'{title.text.strip()} - {price.text.strip() if price else "Preço não disponível"}')
    return items or ['Nenhum resultado']

# ---------- Registro de handlers ----------
application.add_handler(CommandHandler('buscar', buscar_cmd))
# ... manter todos os handlers de download do seu bot ...

# ---------- Flask webhook ----------
@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update_data = request.get_json(force=True)
    update = Update.de_json(update_data, application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), APP_LOOP)
    return 'ok'

@app.route('/')
def index():
    return 'Bot rodando'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
