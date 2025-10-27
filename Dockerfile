
FROM python:3.11-slim

RUN apt-get update && apt-get install -y     ffmpeg     && apt-get clean     && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --upgrade pip && pip install -r requirements.txt

ENV PORT=10000
EXPOSE 10000

CMD ["python", "bot.py"]
