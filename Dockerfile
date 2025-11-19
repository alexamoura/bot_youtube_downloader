FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Instala dependências essenciais (ffmpeg, unzip, curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Instala Deno (runtime JS para yt-dlp)
RUN curl -fsSL https://deno.land/install.sh | sh \
    && ln -s /root/.deno/bin/deno /usr/local/bin/deno

# Define diretório de trabalho
WORKDIR /srv/app

# Copia requirements e instala dependências Python com prefer-binary para acelerar
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefer-binary -r requirements.txt

# Copia código da aplicação
COPY . .

# Define porta padrão (Render usa $PORT)
ENV PORT=10000

# Comando para iniciar
CMD ["gunicorn", "bot_with_cookies:app", "--bind", "0.0.0.0:10000", "--workers", "1"]
