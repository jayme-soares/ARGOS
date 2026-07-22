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
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot_monitor_religacao.py"]
