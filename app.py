#!/usr/bin/env python3
"""
app.py - Exemplo simples em Flask para usar yt-dlp com cookies passados via
variável de ambiente YT_COOKIES_B64 (base64). Protegido por token (SECRET_TOKEN).
Rode com: gunicorn app:app --bind 0.0.0.0:$PORT
"""
import os
import base64
import tempfile
import logging
from flask import Flask, request, jsonify, abort
import yt_dlp as yt_dlp_lib

LOG = logging.getLogger("yt_downloader")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = Flask(__name__)

# Nome da variável de ambiente com os cookies em base64
COOKIES_ENV = os.environ.get("YT_COOKIES_B64")
COOKIE_PATH = None

# Token simples para proteger o endpoint (defina SECRET_TOKEN no Render)
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")

def write_cookies_from_env(env_var_value):
    if not env_var_value:
        LOG.warning("Nenhuma variável de cookies encontrada (YT_COOKIES_B64).")
        return None
    # grava em arquivo temporário persistente durante a execução
    fd, path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
    os.close(fd)
    try:
        raw = base64.b64decode(env_var_value)
    except Exception as e:
        LOG.exception("Falha ao decodificar cookies base64: %s", e)
        raise
    with open(path, "wb") as f:
        f.write(raw)
    LOG.info("Cookies gravados em %s", path)
    return path

# Inicializa cookies no startup
try:
    COOKIE_PATH = write_cookies_from_env(COOKIES_ENV)
except Exception:
    LOG.exception("Erro ao preparar cookies. O endpoint ainda pode tentar sem cookies.")

def ytdlp_download(url, outtmpl=None, cookiefile=None):
    opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
        "noplaylist": False,
        "retries": 5,
        "logger": LOG,
        "http_headers": {"User-Agent": "yt-dlp (script)"},
    }
    if outtmpl:
        opts["outtmpl"] = outtmpl
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with yt_dlp_lib.YoutubeDL(opts) as ydl:
        result = ydl.extract_info(url, download=True)
    return result

def check_auth(request):
    token = request.headers.get("Authorization", "")
    if not SECRET_TOKEN:
        LOG.warning("SECRET_TOKEN não configurado; endpoint não protegido!")
        return True
    if not token.startswith("Bearer "):
        return False
    return token.split(" ", 1)[1] == SECRET_TOKEN

@app.route("/download", methods=["POST"])
def download_endpoint():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    outtmpl = data.get("out")  # opcional: template de saída do yt-dlp

    if not url:
        return jsonify({"error": "missing url"}), 400

    LOG.info("Recebido pedido de download para: %s", url)
    try:
        info = ytdlp_download(url, outtmpl=outtmpl, cookiefile=COOKIE_PATH)
    except yt_dlp_lib.utils.DownloadError as e:
        LOG.error("Erro no download: %s", e)
        return jsonify({"error": "download_failed", "detail": str(e)}), 500
    except Exception as e:
        LOG.exception("Erro inesperado")
        return jsonify({"error": "internal_error", "detail": str(e)}), 500

    return jsonify({
        "status": "ok",
        "id": info.get("id"),
        "title": info.get("title"),
        "requested_url": url
    }), 200

@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200

if __name__ == "__main__":
    # Para desenvolvimento local:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)