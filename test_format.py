#!/usr/bin/env python3
"""
Script de teste para verificar formatos disponíveis de um vídeo do YouTube
"""
import yt_dlp
import sys

def test_format(video_id):
    """Testa os formatos disponíveis para um vídeo"""
    url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"\n🔍 Testando vídeo: {url}\n")

    # Testa o formato atual
    format_string = "bestvideo[height<=1080]+bestaudio/best"

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

        # Lista formatos disponíveis
        print("\n📋 Listando formatos disponíveis:")
        ydl_opts_list = {
            "quiet": True,
            "no_warnings": True,
            "listformats": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts_list) as ydl:
                ydl.extract_info(url, download=False)
        except:
            pass

        return False

if __name__ == "__main__":
    # Testa com o vídeo do erro: -JMWnoPQk68
    video_ids = [
        "-JMWnoPQk68",  # Vídeo que deu erro
        "IxrTozTZMzA",  # Outro vídeo que deu erro
    ]

    for vid in video_ids:
        test_format(vid)
        print("\n" + "="*60 + "\n")
