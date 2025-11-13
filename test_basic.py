#!/usr/bin/env python3
"""
Testes básicos para o bot YouTube downloader
"""
import unittest
import os
import sys
import tempfile
import base64


class TestAppModule(unittest.TestCase):
    """Testes para o módulo app.py"""

    def test_import_app(self):
        """Testa se o módulo app pode ser importado"""
        try:
            import app
            self.assertIsNotNone(app)
        except ImportError as e:
            self.fail(f"Falha ao importar app: {e}")

    def test_flask_app_exists(self):
        """Testa se a aplicação Flask foi criada"""
        import app
        self.assertIsNotNone(app.app)
        self.assertEqual(app.app.name, 'app')

    def test_write_cookies_from_env(self):
        """Testa a função de escrita de cookies"""
        import app

        # Testa com valor vazio
        result = app.write_cookies_from_env(None)
        self.assertIsNone(result)

        # Testa com valor base64 válido
        test_cookie = "test cookie content"
        encoded = base64.b64encode(test_cookie.encode()).decode()
        result = app.write_cookies_from_env(encoded)
        self.assertIsNotNone(result)
        self.assertTrue(os.path.exists(result))

        # Limpa
        if result and os.path.exists(result):
            os.remove(result)

    def test_check_auth_no_token(self):
        """Testa autenticação sem token configurado"""
        import app
        from flask import Flask
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request

        # Salva o token original
        original_token = app.SECRET_TOKEN
        app.SECRET_TOKEN = ""

        # Cria request fake
        builder = EnvironBuilder(method='POST')
        env = builder.get_environ()
        request = Request(env)

        result = app.check_auth(request)
        self.assertTrue(result)

        # Restaura
        app.SECRET_TOKEN = original_token

    def test_check_auth_with_token(self):
        """Testa autenticação com token configurado"""
        import app
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request

        # Salva o token original
        original_token = app.SECRET_TOKEN
        app.SECRET_TOKEN = "test_secret_token"

        # Testa sem header
        builder = EnvironBuilder(method='POST')
        env = builder.get_environ()
        request = Request(env)
        result = app.check_auth(request)
        self.assertFalse(result)

        # Testa com token correto
        builder = EnvironBuilder(
            method='POST',
            headers={'Authorization': 'Bearer test_secret_token'}
        )
        env = builder.get_environ()
        request = Request(env)
        result = app.check_auth(request)
        self.assertTrue(result)

        # Testa com token incorreto
        builder = EnvironBuilder(
            method='POST',
            headers={'Authorization': 'Bearer wrong_token'}
        )
        env = builder.get_environ()
        request = Request(env)
        result = app.check_auth(request)
        self.assertFalse(result)

        # Restaura
        app.SECRET_TOKEN = original_token


class TestDownloadYoutubeModule(unittest.TestCase):
    """Testes para o módulo download_youtube.py"""

    def test_import_download_youtube(self):
        """Testa se o módulo download_youtube pode ser importado"""
        try:
            import download_youtube
            self.assertIsNotNone(download_youtube)
        except ImportError as e:
            self.fail(f"Falha ao importar download_youtube: {e}")

    def test_youtube_downloader_class_exists(self):
        """Testa se a classe YouTubeDownloader existe"""
        import download_youtube
        self.assertTrue(hasattr(download_youtube, 'YouTubeDownloader'))


class TestWatermarkModule(unittest.TestCase):
    """Testes para o módulo watermark_simple.py"""

    def test_watermark_function_exists(self):
        """Testa se a função de remoção de watermark existe"""
        try:
            import watermark_simple
            self.assertTrue(hasattr(watermark_simple, 'remove_watermark_simple'))
        except ImportError:
            self.skipTest("OpenCV não instalado - watermark_simple não disponível")


class TestBotModule(unittest.TestCase):
    """Testes para o módulo bot_with_cookies.py"""

    def test_bot_compiles(self):
        """Testa se o bot compila sem erros de sintaxe"""
        try:
            with open('/home/user/bot_youtube_downloader/bot_with_cookies.py', 'r') as f:
                code = f.read()
                compile(code, 'bot_with_cookies.py', 'exec')
        except SyntaxError as e:
            self.fail(f"Erro de sintaxe no bot: {e}")


class TestRequirements(unittest.TestCase):
    """Testes para verificar dependências"""

    def test_flask_installed(self):
        """Verifica se Flask está instalado"""
        try:
            import flask
            self.assertIsNotNone(flask)
        except ImportError:
            self.fail("Flask não está instalado")

    def test_telegram_installed(self):
        """Verifica se python-telegram-bot está instalado"""
        try:
            import telegram
            self.assertIsNotNone(telegram)
        except ImportError:
            self.fail("python-telegram-bot não está instalado")

    def test_ytdlp_installed(self):
        """Verifica se yt-dlp está instalado"""
        try:
            import yt_dlp
            self.assertIsNotNone(yt_dlp)
        except ImportError:
            self.fail("yt-dlp não está instalado")

    def test_requests_installed(self):
        """Verifica se requests está instalado"""
        try:
            import requests
            self.assertIsNotNone(requests)
        except ImportError:
            self.fail("requests não está instalado")

    def test_beautifulsoup_installed(self):
        """Verifica se BeautifulSoup está instalado"""
        try:
            from bs4 import BeautifulSoup
            self.assertIsNotNone(BeautifulSoup)
        except ImportError:
            self.fail("beautifulsoup4 não está instalado")

    def test_pillow_installed(self):
        """Verifica se Pillow está instalado"""
        try:
            from PIL import Image
            self.assertIsNotNone(Image)
        except ImportError:
            self.fail("Pillow não está instalado")


if __name__ == '__main__':
    # Configura para executar testes com verbosidade
    unittest.main(verbosity=2)
