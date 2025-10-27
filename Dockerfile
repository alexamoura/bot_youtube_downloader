# Dockerfile mínimo com ffmpeg para rodar no Render
FROM python:3.11-slim

# Instala dependências do sistema e ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Cria diretório de trabalho
WORKDIR /srv/app

# Copia requirements e instala
COPY requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r /srv/app/requirements.txt

# Copia o código da aplicação
COPY . /srv/app

# Expõe a porta (Render fornece $PORT)
ENV PORT 10000

# Comando padrão para iniciar (o Render sobrescreve com seu Start Command)
CMD ["gunicorn", "bot_with_cookies:app", "--bind", "0.0.0.0:10000", "--workers", "1"]