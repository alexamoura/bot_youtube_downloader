
# Bot YouTube Downloader

Este projeto é um bot do Telegram que permite baixar vídeos do YouTube e enviá-los diretamente para o chat.

## 🚀 Funcionalidades
- Recebe links do YouTube via chat
- Baixa o vídeo em MP4
- Envia o vídeo de volta ao usuário

## 📦 Instalação
1. Clone o repositório:
   ```bash
   git clone https://github.com/seu-usuario/bot_youtube_downloader.git
   cd bot_youtube_downloader
   ```

2. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```

## 🔐 Configuração
Crie um arquivo `.env` com a seguinte variável:
```env
BOT_TOKEN=seu_token_do_telegram
```

Ou defina a variável diretamente no Railway em **Variables**:
- `BOT_TOKEN`: seu token do bot do Telegram

## ▶️ Execução
Para rodar o bot localmente:
```bash
python bot.py
```

No Railway, o bot será iniciado automaticamente com o comando definido no `Procfile`:
```bash
worker: python bot.py
```

## 🛠 Requisitos
- Python 3.8+
- Conta no Telegram
- Token de bot do Telegram (criado via @BotFather)

---

Feito com ❤️ por Alex
