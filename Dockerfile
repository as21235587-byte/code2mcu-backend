# --- Base: halka Python image ---
FROM python:3.11-slim

# arduino-cli install karne ke liye zaroori tools
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# arduino-cli (seedha /usr/local/bin me, taaki PATH me mil jaye)
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/usr/local/bin sh -s -- v0.35.3

# Sirf Arduino Uno ka core (online version abhi sirf Uno flash karta hai).
# ESP32/ESP8266 hata diye hain — wo build ko bahut slow karte the.
RUN arduino-cli core update-index && \
    arduino-cli core install arduino:avr

WORKDIR /app

# Pehle sirf requirements copy karo (Docker cache se build tez hota hai)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Baaki poora project
COPY . .

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]