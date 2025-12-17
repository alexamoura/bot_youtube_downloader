"""
BotDownloader - Twitter/X Adapter
Este arquivo conecta o bot do Twitter ao motor de download existente.
"""

# Importa o seu bot atual (SEM MEXER NELE)
import bot_with_cookies

def twitter_entrypoint():
    """
    Ponto de entrada do bot do Twitter.
    Por enquanto, só confirma que o código principal foi importado.
    """
    print("🤖 Twitter Bot iniciado com sucesso")
    print("📦 Motor de download carregado:", bot_with_cookies.__name__)

if __name__ == "__main__":
    twitter_entrypoint()