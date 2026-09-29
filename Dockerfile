FROM python:3.11-slim

# chromium + chromium-driver do Debian ficam sempre em versões compatíveis
# entre si (evita o problema clássico de mismatch Chrome/chromedriver).
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    ARGOS_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY argos ./argos

# Estado (programáveis vistos, alertas enviados), snapshot, downloads e
# screenshots de debug. Precisa ser volume para sobreviver a restart.
VOLUME ["/data"]

CMD ["python", "-m", "argos.main"]
