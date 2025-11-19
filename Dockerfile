FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Instala dependências do sistema, ffmpeg, unzip e ferramentas básicas
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Instala Deno (runtime JS para yt-dlp)
RUN curl -fsSL https://deno.land/install.sh | sh \
    && ln -s /root/.deno/bin/deno /usr/local/bin/deno

WORKDIR /srv/app

COPY requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r /srv/app/requirements.txt

COPY . /srv/app

ENV PORT 10000

CMD ["gunicorn", "bot_with_cookies:app", "--bind", "0.0.0.0:10000", "--workers", "1"]
