# 🎬 Bot Downloader (Telegram)

<div align="center">

![Python](https://img.shields.io/badge/python-3.11-blue.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg?logo=telegram)
![Status](https://img.shields.io/badge/status-parado%2Fabandonado-lightgrey.svg)

</div>

> **Status:** projeto parado/abandonado, não está em produção no momento.

---

## 📋 Sobre

Bot de Telegram para download de vídeos (YouTube via `yt-dlp` e Shopee via extração direta), com sistema de plano premium pago via **PIX** (Mercado Pago), remoção de marca d'água em vídeos e um assistente de IA (Groq) integrado.

O ponto de entrada real da aplicação é **`bot_with_cookies.py`** — um módulo único (Flask + python-telegram-bot rodando lado a lado) que concentra toda a lógica do projeto.

### ✨ Funcionalidades

- 🎥 Download de vídeos do YouTube (`yt-dlp`, com suporte a cookies)
- 🛍️ Extração e download de vídeos do Shopee (link universal + extração direta)
- 💎 Plano premium com cobrança via **PIX** (Mercado Pago) e ativação automática por webhook
- 🧼 Remoção de marca d'água em vídeos (`WatermarkRemover`)
- 🤖 Assistente de IA via Groq (`/ai`, `/buscar`)
- 📊 Relatórios de uso (`/stats`, `/mensal`)
- ❤️ Monitoramento de saúde/memória do processo e watchdog de reconexão do webhook

---

## 🛠️ Tecnologias

```
Python 3.11
python-telegram-bot 22.5
Flask 3.x + Gunicorn
yt-dlp
Mercado Pago SDK (PIX)
Groq (IA)
SQLite (persistência local)
```

---

## 📦 Instalação local

```bash
git clone https://github.com/alexamoura/bot_youtube_downloader.git
cd bot_youtube_downloader

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

Também é necessário ter o `ffmpeg` instalado no sistema (usado para processar/comprimir vídeo). Veja o `Dockerfile` para a lista completa de dependências de sistema (inclui também Deno, usado pelo `yt-dlp` para alguns desafios de extração).

### Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | sim | Token do bot, obtido com [@BotFather](https://t.me/BotFather) |
| `MERCADOPAGO_ACCESS_TOKEN` | sim (para premium) | Access token do Mercado Pago para gerar cobranças PIX |
| `GROQ_API_KEY` | sim (para IA) | Chave da API Groq usada pelos comandos `/ai` e `/buscar` |
| `WEBHOOK_URL` | sim (produção) | URL pública para onde o Telegram envia updates (webhook) |
| `RENDER_EXTERNAL_URL` | opcional | URL externa quando hospedado no Render, usada pelo keepalive |
| `PORT` | opcional (padrão `10000`) | Porta em que o Gunicorn sobe o servidor Flask |
| `DB_FILE` | opcional | Caminho do arquivo SQLite de persistência |
| `PREMIUM_PRICE` | opcional | Valor cobrado pelo plano premium |
| `PREMIUM_DURATION_DAYS` | opcional | Duração do plano premium em dias |
| `KEEPALIVE_ENABLED` / `KEEPALIVE_INTERVAL` | opcional | Liga/ajusta a rotina de keepalive (evita hibernação em free tiers) |

---

## 🚀 Execução

### Local

```bash
gunicorn bot_with_cookies:app --bind 0.0.0.0:10000 --workers 1
```

### Docker (recomendado — é como o projeto foi pensado para rodar)

```bash
docker build -t bot-downloader .
docker run -p 10000:10000 --env-file .env bot-downloader
```

O `Dockerfile` já instala `ffmpeg`, `curl`, `unzip` e `Deno`, e sobe a aplicação com Gunicorn usando um único worker (importante: o estado do bot — caches, filas de pagamento pendente — vive em memória do processo, então não use múltiplos workers sem revisar essa parte primeiro).

---

## 📱 Uso

### Comandos do Telegram

```
/start    - Inicializar o bot
/status   - Status/uso do usuário atual
/stats    - Estatísticas de uso
/premium  - Informações e compra do plano premium (PIX)
/ai       - Conversar com o assistente de IA
/buscar   - Comando de busca via IA
/mensal   - Relatório mensal (uso administrativo)
```

Fora dos comandos, o fluxo principal é: o usuário envia um link (YouTube ou Shopee) → o bot detecta a plataforma → baixa/extrai o vídeo → aplica remoção de marca d'água quando configurado → envia o resultado.

### Rotas HTTP (Flask)

| Rota | Descrição |
|---|---|
| `POST /<TELEGRAM_BOT_TOKEN>` | Webhook do Telegram (recebe updates) |
| `GET /` | Rota raiz / ping |
| `GET /health` | Health check (usado por plataformas de deploy) |
| `GET /diagnostics` | Diagnóstico de estado interno (memória, filas, etc.) |
| `POST /webhook/pix` | Webhook do Mercado Pago — confirma pagamento e ativa o premium |
| `GET/POST /render-webhook` | Endpoint auxiliar usado no keepalive/deploy no Render |

---

## 🏗️ Arquitetura (real)

Tudo roda dentro do processo único `bot_with_cookies.py`:

```
┌────────────────────┐        ┌───────────────────────┐
│   Telegram API      │──────▶│  Flask (webhook route) │
└────────────────────┘        └──────────┬────────────┘
                                          │
                       ┌──────────────────┼──────────────────┐
                       ▼                  ▼                  ▼
              ┌────────────────┐ ┌────────────────┐ ┌─────────────────┐
              │ yt-dlp /       │ │ Mercado Pago    │ │ Groq (IA)       │
              │ Shopee extractor│ │ PIX (premium)   │ │ /ai, /buscar    │
              └────────┬───────┘ └────────┬────────┘ └─────────────────┘
                       ▼                  ▼
              ┌────────────────┐ ┌────────────────┐
              │ WatermarkRemover│ │ SQLite (users, │
              │                 │ │ pagamentos)     │
              └────────────────┘ └────────────────┘
```

Esse desenho de "tudo em um módulo só" funciona, mas o arquivo já passou de 4700 linhas — uma futura divisão em módulos (bot/, payments/, downloaders/, ai/) é recomendada antes de qualquer nova funcionalidade grande, mas está **fora do escopo** desta limpeza.

---

## 🔒 Segurança

- Nunca commite `TELEGRAM_BOT_TOKEN`, `MERCADOPAGO_ACCESS_TOKEN`, `GROQ_API_KEY` ou `cookies.txt` — todos já estão no `.gitignore`.
- O webhook do Mercado Pago (`/webhook/pix`) processa confirmação de pagamento; qualquer alteração nessa rota deve ser revisada com cuidado antes de ir para produção.
- Use HTTPS em produção (obrigatório para o webhook do Telegram funcionar).

---

## ⚠️ Disclaimer

Este projeto está **parado/abandonado** no momento. O download de vídeos de terceiros deve respeitar os termos de serviço de cada plataforma; o uso é de responsabilidade de quem operar o bot.
