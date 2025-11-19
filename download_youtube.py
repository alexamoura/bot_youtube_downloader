#!/usr/bin/env python3
"""
Script simples para usar yt_dlp em um ambiente remoto (ex: Render) com cookies
exportados do navegador e guardados numa variável de ambiente base64 (YT_COOKIES_B64).

Uso:
  - Exporte cookies do seu navegador (cookies.txt, formato Netscape).
  - No seu terminal local: cat cookies.txt | base64
  - Cole o resultado como variável de ambiente secreta YT_COOKIES_B64 no painel do Render.
  - Start command no Render (ou local): python download_youtube.py <URL> [<URL2> ...]
"""

import os
import sys
import argparse
import base64
import tempfile
import logging

try:
    import yt_dlp as yt_dlp_lib
except Exception as e:
    print("Erro: yt_dlp não encontrado. Instale com: pip install yt-dlp", file=sys.stderr)
    raise

LOG = logging.getLogger("yt_downloader")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def get_youtube_format_by_quality(quality: str) -> str:
    """
    Retorna string de formato yt-dlp baseado na qualidade escolhida
    Estratégia atualizada para máxima compatibilidade com mudanças recentes do YouTube
    """
    # Formatos mais robustos com múltiplos fallbacks
    # Prioriza mp4 e usa fallbacks progressivos
    quality_formats = {
        "360p": (
            "best[height<=360][ext=mp4]/best[height<=360]/"
            "worst[height>=240][ext=mp4]/worst[height>=240]/"
            "best[height<=480]/worst"
        ),
        "480p": (
            "best[height<=480][ext=mp4]/best[height<=480]/"
            "best[height<=360][ext=mp4]/best[height<=360]/"
            "best[height<=720]/best"
        ),
        "720p": (
            "best[height<=720][ext=mp4]/best[height<=720]/"
            "best[height<=480][ext=mp4]/best[height<=480]/"
            "best[height<=1080]/best"
        ),
        "1080p": (
            "best[height<=1080][ext=mp4]/best[height<=1080]/"
            "best[height<=720][ext=mp4]/best[height<=720]/"
            "best"
        ),
        "best": "best[ext=mp4]/best"
    }
    
    # Se a qualidade não for encontrada, usa 720p como padrão
    return quality_formats.get(quality, quality_formats["720p"])


def write_cookies_from_env(env_var="YT_COOKIES_B64", dest_path=None):
    """
    Decodifica a variável de ambiente base64 e grava em dest_path.
    Retorna o caminho do arquivo escrito, ou None se a variável não existir.
    """
    b64 = os.environ.get(env_var)
    if not b64:
        LOG.warning("Variável de ambiente %s não encontrada. Tentando sem cookies.", env_var)
        return None

    if dest_path is None:
        fd, dest_path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
        os.close(fd)

    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        LOG.error("Falha ao decodificar %s: %s", env_var, e)
        raise

    try:
        with open(dest_path, "wb") as f:
            f.write(raw)
    except Exception as e:
        LOG.error("Falha ao gravar cookies em %s: %s", dest_path, e)
        raise

    LOG.info("Cookies gravados em %s", dest_path)
    return dest_path


def download(urls, cookiefile=None, outtmpl="%(title)s - %(id)s.%(ext)s", extra_opts=None, quality="720p"):
    """Download de vídeos com yt-dlp

    Args:
        urls: Lista de URLs para baixar
        cookiefile: Arquivo de cookies
        outtmpl: Template de saída
        extra_opts: Opções extras do yt-dlp
        quality: Qualidade para YouTube (360p, 480p, 720p, 1080p, best)
    """
    if extra_opts is None:
        extra_opts = {}

    # Detecta se é YouTube para aplicar seleção de qualidade
    is_youtube = any('youtube' in url.lower() or 'youtu.be' in url.lower() for url in urls)

    if is_youtube:
        format_string = get_youtube_format_by_quality(quality)
        LOG.info("YouTube detectado - usando qualidade: %s", quality)
        LOG.debug("Format string: %s", format_string)
    else:
        # Para outras plataformas, usa formato genérico mais robusto
        format_string = "best[ext=mp4]/best"

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": format_string,
        "merge_output_format": "mp4",
        "noplaylist": False,
        # Aumente retries para maior robustez em infra remota:
        "retries": 10,
        # Passa o cookiefile se disponível:
        **({"cookiefile": cookiefile} if cookiefile else {}),
        # Evita interrupções por prints do yt-dlp; usamos logging:
        "logger": LOG,
        "progress_hooks": [lambda d: LOG.debug("progress: %s", d)],
        # Evita checagem interativa
        "nopart": False,
        # Opcional: user agent custom (algumas vezes ajuda)
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        # Opções adicionais para maior compatibilidade
        "prefer_free_formats": False,
        "no_check_certificate": False,
    }

    # Merge any user-provided overrides
    ydl_opts.update(extra_opts)

    LOG.info("Iniciando download de %d URL(s).", len(urls))
    with yt_dlp_lib.YoutubeDL(ydl_opts) as ydl:
        ydl.download(urls)


def main():
    parser = argparse.ArgumentParser(description="Baixa vídeos do YouTube usando cookies via env var base64 (YT_COOKIES_B64).")
    parser.add_argument("urls", nargs="+", help="URLs do YouTube a baixar")
    parser.add_argument("--cookies-env", default="YT_COOKIES_B64", help="Nome da variável de ambiente com cookies em base64")
    parser.add_argument("--out", default="%(title)s - %(id)s.%(ext)s", help="Template de saída (yt-dlp outtmpl)")
    parser.add_argument("--quality", default="720p", choices=["360p", "480p", "720p", "1080p", "best"],
                        help="Qualidade do vídeo para YouTube (padrão: 720p)")
    parser.add_argument("--no-cookies", action="store_true", help="Não usar cookies mesmo se a variável existir")
    parser.add_argument("--debug", action="store_true", help="Habilita debug logging")
    parser.add_argument("--list-formats", action="store_true", help="Lista formatos disponíveis ao invés de baixar")
    args = parser.parse_args()

    if args.debug:
        LOG.setLevel(logging.DEBUG)

    cookie_path = None
    if not args.no_cookies:
        try:
            cookie_path = write_cookies_from_env(env_var=args.cookies_env)
        except Exception:
            LOG.exception("Não foi possível preparar cookies. Abortando.")
            sys.exit(2)

    if cookie_path is None:
        LOG.warning("Executando sem cookies. Se o YouTube pedir verificação, o download pode falhar.")

    # Se --list-formats foi passado, lista formatos ao invés de baixar
    if args.list_formats:
        ydl_opts = {
            "listformats": True,
            **({"cookiefile": cookie_path} if cookie_path else {}),
        }
        with yt_dlp_lib.YoutubeDL(ydl_opts) as ydl:
            for url in args.urls:
                ydl.extract_info(url, download=False)
        sys.exit(0)

    try:
        download(args.urls, cookiefile=cookie_path, outtmpl=args.out, quality=args.quality)
    except yt_dlp_lib.utils.DownloadError as e:
        LOG.error("Erro de download: %s", e)
        sys.exit(3)
    except KeyboardInterrupt:
        LOG.info("Interrompido pelo usuário.")
        sys.exit(130)
    except Exception:
        LOG.exception("Erro inesperado durante o download.")
        sys.exit(1)
    finally:
        # Apagar arquivo temporário de cookies se criado
        if cookie_path and cookie_path.startswith(tempfile.gettempdir()):
            try:
                os.remove(cookie_path)
                LOG.debug("Arquivo de cookies temporário removido: %s", cookie_path)
            except Exception:
                LOG.debug("Não foi possível remover o arquivo de cookies temporário: %s", cookie_path)


if __name__ == "__main__":
    main()
