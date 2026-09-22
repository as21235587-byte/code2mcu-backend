# --- Base: halka Python image ---
FROM python:3.11-slim

# System tools jo arduino-cli install karne aur usko chalane ke liye chahiye
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- arduino-cli install karo ---
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh -s -- v0.35.3
ENV PATH="/root/bin:${PATH}"

# --- Board "cores" install karo (Uno / ESP32 / ESP8266 ke liye) ---
RUN arduino-cli core update-index && \
    arduino-cli core install arduino:avr && \
    arduino-cli config init && \
    arduino-cli config add board_manager.additional_urls https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json https://arduino.esp8266.com/stable/package_esp8266com_index.json && \
    arduino-cli core update-index && \
    arduino-cli core install esp32:esp32 && \
    arduino-cli core install esp8266:esp8266

# --- App code ke liye folder ---
WORKDIR /app

# Pehle sirf requirements copy karo (Docker caching fast build ke liye)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Baaki poora project copy karo
COPY . .

# Render/Railway jis bhi port par bhejein, usi par chalna chahiye
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]