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

def get_youtube_format_by_quality(quality: str) -> str:
    """
    Retorna string de formato yt-dlp baseado na qualidade escolhida
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

def ytdlp_download(url, outtmpl=None, cookiefile=None, quality="720p"):
    """Download de vídeo com yt-dlp

    Args:
        url: URL do vídeo
        outtmpl: Template de saída
        cookiefile: Arquivo de cookies
        quality: Qualidade para YouTube (360p, 480p, 720p, 1080p, best)
    
    Returns:
        dict: Informações do vídeo baixado
    """
    # Detecta se é YouTube
    is_youtube = 'youtube' in url.lower() or 'youtu.be' in url.lower()

    if is_youtube:
        format_string = get_youtube_format_by_quality(quality)
        LOG.info("YouTube detectado - usando qualidade: %s", quality)
        LOG.debug("Format string: %s", format_string)
    else:
        # Para outras plataformas, usa formato genérico
        format_string = "best"

    opts = {
        "format": format_string,
        "merge_output_format": "mp4",
        "noplaylist": False,
        "retries": 5,
        "logger": LOG,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
        # Opções adicionais para maior compatibilidade
        "prefer_free_formats": False,
        "no_check_certificate": False,
    }
    
    if outtmpl:
        opts["outtmpl"] = outtmpl
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with yt_dlp_lib.YoutubeDL(opts) as ydl:
        result = ydl.extract_info(url, download=True)
    return result

def ytdlp_get_info(url, cookiefile=None):
    """Obtém informações do vídeo sem baixar

    Args:
        url: URL do vídeo
        cookiefile: Arquivo de cookies
    
    Returns:
        dict: Informações do vídeo
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "logger": LOG,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    }
    
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with yt_dlp_lib.YoutubeDL(opts) as ydl:
        result = ydl.extract_info(url, download=False)
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
    """Endpoint para download de vídeos"""
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    outtmpl = data.get("out")  # opcional: template de saída do yt-dlp
    quality = data.get("quality", "720p")  # opcional: qualidade do vídeo

    if not url:
        return jsonify({"error": "missing url"}), 400
    
    # Valida qualidade
    valid_qualities = ["360p", "480p", "720p", "1080p", "best"]
    if quality not in valid_qualities:
        return jsonify({
            "error": "invalid_quality",
            "message": f"Quality must be one of: {', '.join(valid_qualities)}"
        }), 400

    LOG.info("Recebido pedido de download para: %s (quality: %s)", url, quality)
    try:
        info = ytdlp_download(url, outtmpl=outtmpl, cookiefile=COOKIE_PATH, quality=quality)
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
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "quality": quality,
        "requested_url": url
    }), 200

@app.route("/info", methods=["POST"])
def info_endpoint():
    """Endpoint para obter informações do vídeo sem baixar"""
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")

    if not url:
        return jsonify({"error": "missing url"}), 400

    LOG.info("Recebido pedido de informações para: %s", url)
    try:
        info = ytdlp_get_info(url, cookiefile=COOKIE_PATH)
    except yt_dlp_lib.utils.DownloadError as e:
        LOG.error("Erro ao obter informações: %s", e)
        return jsonify({"error": "info_extraction_failed", "detail": str(e)}), 500
    except Exception as e:
        LOG.exception("Erro inesperado")
        return jsonify({"error": "internal_error", "detail": str(e)}), 500

    # Formata informações relevantes
    return jsonify({
        "status": "ok",
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "description": info.get("description", "")[:500],  # Limita descrição
        "thumbnail": info.get("thumbnail"),
        "requested_url": url
    }), 200

@app.route("/formats", methods=["POST"])
def formats_endpoint():
    """Endpoint para listar formatos disponíveis"""
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")

    if not url:
        return jsonify({"error": "missing url"}), 400

    LOG.info("Recebido pedido de formatos para: %s", url)
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "listformats": True,
            "logger": LOG,
        }
        
        if COOKIE_PATH:
            opts["cookiefile"] = COOKIE_PATH
        
        with yt_dlp_lib.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        # Extrai informações dos formatos
        formats = []
        for f in info.get("formats", []):
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution", "N/A"),
                "fps": f.get("fps"),
                "filesize": f.get("filesize"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
            })
        
        return jsonify({
            "status": "ok",
            "title": info.get("title"),
            "formats": formats,
            "requested_url": url
        }), 200
        
    except Exception as e:
        LOG.exception("Erro ao listar formatos")
        return jsonify({"error": "formats_list_failed", "detail": str(e)}), 500

@app.route("/healthz", methods=["GET"])
def healthz():
    """Endpoint de health check"""
    cookies_status = "available" if COOKIE_PATH else "not_configured"
    return jsonify({
        "status": "healthy",
        "cookies": cookies_status,
        "version": "1.0.1"
    }), 200

@app.route("/", methods=["GET"])
def root():
    """Endpoint raiz com documentação básica"""
    return jsonify({
        "service": "YouTube Downloader API",
        "version": "1.0.1",
        "endpoints": {
            "/download": "POST - Download video",
            "/info": "POST - Get video information",
            "/formats": "POST - List available formats",
            "/healthz": "GET - Health check"
        },
        "authentication": "Bearer token in Authorization header" if SECRET_TOKEN else "Not configured"
    }), 200

if __name__ == "__main__":
    # Para desenvolvimento local:
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("DEBUG", "false").lower() == "true"
    
    LOG.info(f"Starting server on port {port} (debug={debug_mode})")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
