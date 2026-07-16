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

# Define porta padrão
ENV PORT=10000

# Comando para iniciar
# IMPORTANTE: roda o módulo direto (não via gunicorn) porque a inicialização
# real (threads de limpeza/GC, keepalive, watchdog, webhook/long-polling)
# vive dentro do bloco `if __name__ == "__main__":` — sob gunicorn (que
# importa o módulo) esse bloco nunca executava.
CMD ["python", "bot_with_cookies.py"]
