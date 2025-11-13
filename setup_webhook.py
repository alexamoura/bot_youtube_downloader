#!/usr/bin/env python3
"""
Script para configurar webhook do bot Telegram
"""
import os
import requests
import sys

# Obter token do bot (defina como variável de ambiente ou edite aqui)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "SEU_TOKEN_AQUI")

# URL do seu serviço no Render
RENDER_URL = "https://bot-youtube-downloader-telegram.onrender.com"

def setup_webhook():
    """Configura o webhook do Telegram"""
    webhook_url = f"{RENDER_URL}/{TOKEN}"

    print(f"🔧 Configurando webhook...")
    print(f"   URL: {webhook_url}")

    # Endpoint da API do Telegram para configurar webhook
    api_url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"

    # Payload
    payload = {
        "url": webhook_url,
        "drop_pending_updates": True,
        "allowed_updates": ["message", "callback_query", "my_chat_member"]
    }

    try:
        response = requests.post(api_url, json=payload, timeout=30)
        response.raise_for_status()

        result = response.json()

        if result.get("ok"):
            print("✅ Webhook configurado com sucesso!")
            print(f"   Resposta: {result.get('description', 'OK')}")
        else:
            print("❌ Falha ao configurar webhook")
            print(f"   Erro: {result}")
            sys.exit(1)

    except requests.exceptions.RequestException as e:
        print(f"❌ Erro de requisição: {e}")
        sys.exit(1)

def get_webhook_info():
    """Obtém informações sobre o webhook atual"""
    api_url = f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo"

    try:
        response = requests.get(api_url, timeout=30)
        response.raise_for_status()

        result = response.json()

        if result.get("ok"):
            info = result.get("result", {})
            print("\n📊 Informações do Webhook:")
            print(f"   URL: {info.get('url', 'Nenhum')}")
            print(f"   Pending updates: {info.get('pending_update_count', 0)}")
            print(f"   Last error: {info.get('last_error_message', 'Nenhum')}")
            print(f"   Last error date: {info.get('last_error_date', 'N/A')}")

            return info
        else:
            print(f"❌ Erro ao obter info: {result}")

    except requests.exceptions.RequestException as e:
        print(f"❌ Erro de requisição: {e}")

def delete_webhook():
    """Remove o webhook atual"""
    api_url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"

    try:
        response = requests.post(api_url, timeout=30)
        response.raise_for_status()

        result = response.json()

        if result.get("ok"):
            print("✅ Webhook removido com sucesso!")
        else:
            print(f"❌ Erro ao remover webhook: {result}")

    except requests.exceptions.RequestException as e:
        print(f"❌ Erro de requisição: {e}")

if __name__ == "__main__":
    if TOKEN == "SEU_TOKEN_AQUI":
        print("❌ ERRO: Configure o TELEGRAM_BOT_TOKEN!")
        print("   Execute: export TELEGRAM_BOT_TOKEN='seu_token'")
        sys.exit(1)

    print("🤖 Configurador de Webhook do Telegram Bot\n")
    print("Escolha uma opção:")
    print("1. Configurar webhook")
    print("2. Ver informações do webhook")
    print("3. Remover webhook")

    choice = input("\nOpção (1-3): ").strip()

    if choice == "1":
        setup_webhook()
        print("\n" + "="*50)
        get_webhook_info()
    elif choice == "2":
        get_webhook_info()
    elif choice == "3":
        delete_webhook()
        get_webhook_info()
    else:
        print("❌ Opção inválida!")
