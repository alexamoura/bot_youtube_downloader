# Dockerfile mínimo com ffmpeg + Deno para rodar no Render
FROM python:3.11-slim

# Evita prompts interativos
ENV DEBIAN_FRONTEND=noninteractive

# Instala dependências do sistema, ffmpeg e ferramentas básicas
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Instala Deno (runtime JS para yt-dlp)
RUN curl -fsSL https://deno.land/install.sh | sh \
    && ln -s /root/.deno/bin/deno /usr/local/bin/deno

# Cria diretório de trabalho
WORKDIR /srv/app

# Copia requirements e instala dependências Python
COPY requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r /srv/app/requirements.txt

# Copia o código da aplicação
COPY . /srv/app

# Expõe a porta (Render fornece $PORT)
ENV PORT 10000

# Comando padrão para iniciar (Render sobrescreve com seu Start Command)
CMD ["gunicorn", "bot_with_cookies:app", "--bind", "0.0.0.0:10000", "--workers", "1"]
