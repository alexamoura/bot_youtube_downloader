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


def download(urls, cookiefile=None, outtmpl="%(title)s - %(id)s.%(ext)s", extra_opts=None):
    if extra_opts is None:
        extra_opts = {}

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
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
        "http_headers": {"User-Agent": "yt-dlp (script)"},
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
    parser.add_argument("--no-cookies", action="store_true", help="Não usar cookies mesmo se a variável existir")
    parser.add_argument("--debug", action="store_true", help="Habilita debug logging")
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

    try:
        download(args.urls, cookiefile=cookie_path, outtmpl=args.out)
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