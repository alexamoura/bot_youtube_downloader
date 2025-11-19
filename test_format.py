#!/usr/bin/env python3
"""
Script de teste para verificar formatos disponíveis de um vídeo do YouTube
com diferentes qualidades
"""
import yt_dlp
import sys

def get_youtube_format_by_quality(quality: str) -> str:
    """Retorna string de formato yt-dlp baseado na qualidade escolhida

    Formatos otimizados para máxima compatibilidade com fallbacks robustos
    """
    quality_formats = {
        "360p": "best[height<=360]/bestvideo[height<=360]+bestaudio/worst",
        "480p": "best[height<=480]/bestvideo[height<=480]+bestaudio/best[height<=360]",
        "720p": "best[height<=720]/bestvideo[height<=720]+bestaudio/best[height<=480]",
        "1080p": "best[height<=1080]/bestvideo[height<=1080]+bestaudio/best",
        "best": "bestvideo+bestaudio/best",
    }
    return quality_formats.get(quality, quality_formats["720p"])

def test_format(video_id, quality="720p"):
    """Testa os formatos disponíveis para um vídeo"""
    url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"\n🔍 Testando vídeo: {url}")
    print(f"📺 Qualidade: {quality}\n")

    # Testa o formato da qualidade escolhida
    format_string = get_youtube_format_by_quality(quality)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_string,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            print(f"✅ SUCESSO com formato: {format_string}")
            print(f"📹 Título: {info.get('title')}")
            print(f"🎬 Formato selecionado: {info.get('format')}")
            print(f"📊 Resolução: {info.get('width')}x{info.get('height')}")
            print(f"⏱️  Duração: {info.get('duration')}s")
            return True
    except Exception as e:
        print(f"❌ ERRO com formato {format_string}: {e}")
        return False

if __name__ == "__main__":
    # Testa com diferentes vídeos e qualidades
    video_ids = [
        "-JMWnoPQk68",  # Vídeo que deu erro
        "IxrTozTZMzA",  # Outro vídeo que deu erro
    ]

    qualities = ["360p", "480p", "720p", "1080p"]

    for vid in video_ids:
        print(f"\n{'='*60}")
        print(f"Testando vídeo: {vid}")
        print('='*60)

        for quality in qualities:
            test_format(vid, quality)
            print()

        print("\n" + "="*60 + "\n")
