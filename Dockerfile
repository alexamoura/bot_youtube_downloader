
# Use imagem oficial do Python 3.11
FROM python:3.11-slim

# Instala o Git (necessário para instalar pacotes via git+)
RUN apt-get update && apt-get install -y git     && apt-get clean     && rm -rf /var/lib/apt/lists/*

# Define diretório de trabalho
WORKDIR /app

# Copia os arquivos do projeto
COPY . .

# Instala as dependências
RUN pip install --upgrade pip && pip install -r requirements.txt

# Comando para iniciar o bot
CMD ["python", "bot.py"]
