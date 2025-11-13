#!/usr/bin/env python3
"""
Script de teste e diagnóstico do serviço no Render
"""
import requests
import sys
import os
from datetime import datetime

RENDER_URL = "https://bot-youtube-downloader-telegram.onrender.com"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

def print_header(text):
    """Imprime cabeçalho formatado"""
    print("\n" + "="*60)
    print(f"  {text}")
    print("="*60)

def test_endpoint(url, method="GET", description=""):
    """Testa um endpoint específico"""
    print(f"\n🔍 Testando: {description or url}")
    print(f"   Método: {method}")

    try:
        if method == "GET":
            response = requests.get(url, timeout=10)
        else:
            response = requests.post(url, timeout=10)

        print(f"   Status: {response.status_code}")
        print(f"   Headers: {dict(response.headers)}")

        # Tentar mostrar conteúdo
        try:
            if response.headers.get('content-type', '').startswith('application/json'):
                print(f"   Resposta: {response.json()}")
            else:
                content = response.text[:200]
                print(f"   Resposta: {content}...")
        except:
            print(f"   Resposta: {response.text[:100]}")

        return response.status_code

    except requests.exceptions.Timeout:
        print("   ❌ TIMEOUT - Serviço não respondeu em 10s")
        return None
    except requests.exceptions.ConnectionError:
        print("   ❌ CONNECTION ERROR - Não foi possível conectar")
        return None
    except Exception as e:
        print(f"   ❌ ERRO: {e}")
        return None

def test_telegram_api():
    """Testa a API do Telegram diretamente"""
    if not TOKEN or TOKEN == "":
        print("⚠️  TELEGRAM_BOT_TOKEN não configurado - pulando teste")
        return

    print(f"\n🤖 Testando API do Telegram...")

    # Testar getMe
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getMe",
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                bot_info = data.get("result", {})
                print(f"   ✅ Bot ativo: @{bot_info.get('username')}")
                print(f"   Nome: {bot_info.get('first_name')}")
                print(f"   ID: {bot_info.get('id')}")
        else:
            print(f"   ❌ Erro ao obter info do bot: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Erro: {e}")

    # Testar webhook info
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo",
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                info = data.get("result", {})
                print(f"\n   📡 Webhook Info:")
                print(f"      URL: {info.get('url', 'Não configurado')}")
                print(f"      Pending: {info.get('pending_update_count', 0)}")

                if info.get('last_error_message'):
                    print(f"      ⚠️  Último erro: {info.get('last_error_message')}")
                    print(f"         Data: {info.get('last_error_date')}")
                else:
                    print(f"      ✅ Sem erros recentes")
    except Exception as e:
        print(f"   ❌ Erro ao obter webhook info: {e}")

def main():
    """Função principal"""
    print(f"\n{'='*60}")
    print(f"  🧪 DIAGNÓSTICO DO SERVIÇO RENDER")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    print(f"\n🌐 URL do Serviço: {RENDER_URL}")

    # Teste 1: Root endpoint
    print_header("TESTE 1: Endpoint Raiz (/)")
    test_endpoint(f"{RENDER_URL}/", "GET", "Endpoint raiz")

    # Teste 2: Health check
    print_header("TESTE 2: Health Check (/health)")
    status = test_endpoint(f"{RENDER_URL}/health", "GET", "Health check")

    # Teste 3: Diagnostics
    print_header("TESTE 3: Diagnostics (/diagnostics)")
    test_endpoint(f"{RENDER_URL}/diagnostics", "GET", "Diagnóstico completo")

    # Teste 4: API do Telegram
    print_header("TESTE 4: API do Telegram")
    test_telegram_api()

    # Resumo
    print_header("📊 RESUMO")

    if status == 200:
        print("✅ Serviço está ONLINE e respondendo")
    elif status == 403:
        print("⚠️  Serviço bloqueando acesso HTTP (403)")
        print("   Isso pode ser normal se o bot usa apenas webhook")
        print("   Teste o bot diretamente no Telegram!")
    elif status is None:
        print("❌ Serviço NÃO ESTÁ RESPONDENDO")
        print("   Verifique os logs no painel do Render")
    else:
        print(f"⚠️  Status inesperado: {status}")

    print("\n" + "="*60)
    print("\n💡 PRÓXIMOS PASSOS:\n")
    print("1. Se status = 403:")
    print("   → Teste o bot no Telegram enviando /start")
    print("   → Execute: python3 setup_webhook.py")
    print("")
    print("2. Se serviço não responde:")
    print("   → Verifique logs no Render Dashboard")
    print("   → Confirme variáveis de ambiente")
    print("   → Verifique se o deploy foi concluído")
    print("")
    print("3. Configurar webhook:")
    print("   → export TELEGRAM_BOT_TOKEN='seu_token'")
    print("   → python3 setup_webhook.py")
    print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    main()
