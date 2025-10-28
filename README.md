# Telegram yt-dlp bot (Render)

Este repositório contém um bot Telegram que usa yt-dlp para baixar vídeos do YouTube, TIK TOK, Instagram e Twitter (X) e enviar via Telegram. Ele foi preparado para rodar no Render usando Docker (com ffmpeg incluído).

ATENÇÃO: não comite cookies.txt nem tokens no repositório. Use secrets (env vars) no Render.

### Arquivos principais
- bot_with_cookies.py : código do bot / webhook
- requirements.txt : dependências Python
- Dockerfile : imagem com ffmpeg
- .gitignore : ignora cookies e arquivos locais

### Como gerar o secret de cookies (local)
1. Exporte cookies do navegador (formato Netscape cookies.txt) usando uma extensão (ex.: "Get cookies.txt").
2. No terminal (Linux/macOS):
   base64 -w0 cookies.txt > cookies.b64
   # copie o conteúdo de cookies.b64
   cat cookies.b64
   # copie toda a linha resultante
3. No Windows PowerShell:
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\caminho\cookies.txt")) > cookies.b64
   Get-Content .\cookies.b64

### Variáveis de ambiente (Render)
No painel do Render -> seu service -> Environment -> Secrets, crie os seguintes secrets:
- TELEGRAM_BOT_TOKEN  (ex: 123456:ABC-DEF)
- YT_COOKIES_B64      (cole a string base64 gerada no passo acima)

### Deploy no Render (usando Dockerfile)
1. Faça push do repositório para o GitHub.
2. No Render: New -> Web Service -> conecte ao repo -> selecione branch.
   - Escolha usar Docker (Render detecta Dockerfile automaticamente).
3. Deploy. Após o deploy, você verá a URL pública do serviço (ex: https://meu-bot.onrender.com).

### Configurar webhook do Telegram
Após o serviço estar no ar, rode (substitua valores):

```bash
# Unix / macOS
curl -X POST "https://api.telegram.org/bot<SEU_TOKEN>/setWebhook" \
  -d "url=https://<SEU_SERVICO>.onrender.com/<SEU_TOKEN>"
```

Se preferir, troque <SEU_TOKEN> por $TELEGRAM_BOT_TOKEN ao rodar localmente.

### Testes locais
Para testar localmente sem Render:
1. Exporte variáveis:
   Linux/macOS:
     export TELEGRAM_BOT_TOKEN="seu_token"
     export YT_COOKIES_B64=$(base64 -w0 cookies.txt)
     python bot_with_cookies.py
   PowerShell:
     $env:TELEGRAM_BOT_TOKEN="seu_token"
     $env:YT_COOKIES_B64=[Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))
     python bot_with_cookies.py
2. Use ngrok (ou similar) para expor localhost e configure webhook apontando para ngrok + token.

### Observações importantes
- Cookies expiram; quando o YouTube pedir autenticação novamente (erro "Sign in to confirm you’re not a bot"), gere novos cookies e atualize YT_COOKIES_B64 no Render.
- O filesystem do container é efêmero — se precisar persistir vídeos, envie-os para um storage (S3/GCS).
- Não publique cookies ou tokens. Se algum cookie/token vazar, revogue ou troque a senha imediatamente.
- O Dockerfile já instala ffmpeg; se você optar por não usar Docker no Render, garanta que ffmpeg esteja disponível no ambiente.

Se quiser, depois eu te passo o comando exato para criar o serviço no Render passo a passo (com screenshots textuais) ou um script para configurar o webhook automaticamente.  
