# 🎬 Telegram Media Downloader Bot

<div align="center">

![Python](https://img.shields.io/badge/python-3.9+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg?logo=telegram)

**Bot profissional para download de mídias de múltiplas plataformas**

</div>

---

## 📋 Sobre

Bot Telegram para download de vídeos e áudios de diversas plataformas, com sistema de controle de usuários e planos de acesso.

### ✨ Funcionalidades

- 🎥 Download de vídeos e áudios
- 📱 Múltiplas plataformas suportadas
- 💎 Sistema de planos (gratuito e premium)
- 🔒 Controle de limites por usuário
- ⚡ Processamento assíncrono com filas
- 📊 Estatísticas de uso

---

## 🛠️ Tecnologias

```python
Python 3.9+
python-telegram-bot 20.6
yt-dlp
Flask 3.0
Gunicorn 21.2
SQLite 3
```

---

## 📦 Instalação

### Pré-requisitos

- Python 3.9+
- Conta no Telegram Bot ([@BotFather](https://t.me/BotFather))

### Instalação Local

```bash
# Clone o repositório
git clone https://github.com/seu-usuario/telegram-media-bot.git
cd telegram-media-bot

# Crie um ambiente virtual
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Instale as dependências
pip install -r requirements.txt

# Configure as variáveis de ambiente
cp .env.example .env
nano .env
```

### requirements.txt

```txt
python-telegram-bot==20.6
flask==3.0.0
gunicorn==21.2.0
yt-dlp==2024.10.22
requests==2.31.0
beautifulsoup4==4.12.2
Pillow==10.1.0
```

---

## ⚙️ Configuração

### Variáveis de Ambiente

Crie um arquivo `.env`:

```bash
# Bot Telegram
TELEGRAM_BOT_TOKEN=seu_token_aqui

# Limites
FREE_DOWNLOADS_LIMIT=10
MAX_CONCURRENT_DOWNLOADS=3

# Banco de Dados
DB_FILE=/data/users.db

# Servidor
PORT=10000
```

### Obter Token do Telegram

1. Fale com [@BotFather](https://t.me/BotFather)
2. Envie `/newbot`
3. Escolha nome e username
4. Copie o token fornecido

---

## 🚀 Execução

### Local

```bash
# Ativar ambiente virtual
source venv/bin/activate

# Executar bot
python bot.py

# Ou com Gunicorn
gunicorn bot:app --bind 0.0.0.0:10000
```

### Deploy

O bot pode ser hospedado em plataformas como:

- **Render** (recomendado para iniciantes)
- **Heroku**
- **Railway**
- **VPS** (Digital Ocean, AWS, etc)

#### Exemplo de Configuração

```yaml
# render.yaml
services:
  - type: web
    name: telegram-bot
    env: python
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn bot:app --bind 0.0.0.0:$PORT
```

---

## 📱 Uso

### Comandos

```
/start   - Inicializar bot
/help    - Ajuda
/status  - Ver estatísticas
```

### Exemplo

```
Usuário: [envia link de vídeo]
Bot: [mostra opções de qualidade]
Usuário: [escolhe qualidade]
Bot: [processa e envia vídeo]
```

---

## 🏗️ Arquitetura

```
┌─────────────────┐
│  Telegram API   │
└────────┬────────┘
         │
┌────────▼────────┐
│  Flask Server   │
├─────────────────┤
│  • Webhooks     │
│  • Routing      │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌────────┐ ┌──────────┐
│Download│ │ Database │
│Manager │ │ SQLite   │
└────────┘ └──────────┘
```

---

## 📊 Banco de Dados

### Estrutura

```sql
-- Usuários
CREATE TABLE user_downloads (
    user_id INTEGER PRIMARY KEY,
    downloads_count INTEGER DEFAULT 0,
    last_reset TEXT,
    is_premium INTEGER DEFAULT 0
);
```

---

## 🔒 Segurança

### Boas Práticas

- ✅ Nunca commite credenciais
- ✅ Use variáveis de ambiente
- ✅ Mantenha dependências atualizadas
- ✅ Implemente rate limiting
- ✅ Valide inputs do usuário
- ✅ Use HTTPS em produção

### .gitignore

```bash
# Credenciais
.env
.env.local

# Database
*.db

# Python
__pycache__/
*.pyc

# Logs
*.log
```

---

## 🤝 Contribuindo

Contribuições são bem-vindas!

### Como Contribuir

1. Fork o projeto
2. Crie uma branch (`git checkout -b feature/nova-funcionalidade`)
3. Commit suas mudanças (`git commit -m 'feat: adiciona funcionalidade'`)
4. Push para a branch (`git push origin feature/nova-funcionalidade`)
5. Abra um Pull Request

### Padrões de Código

- Siga PEP 8
- Use type hints
- Documente funções
- Escreva testes quando possível

---

## ⚠️ Disclaimer

Este bot é fornecido apenas para fins educacionais. Certifique-se de respeitar os termos de serviço das plataformas. O uso inadequado é de responsabilidade do usuário.

---

## 📞 Suporte

- 🐛 [Reportar Bug](https://github.com/seu-usuario/telegram-media-bot/issues)
- 💡 [Sugerir Funcionalidade](https://github.com/seu-usuario/telegram-media-bot/issues)
- 📖 [Documentação](https://github.com/seu-usuario/telegram-media-bot/wiki)
