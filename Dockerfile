FROM python:3.11-slim

# Audio tools for processing
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg sox libsox-fmt-mp3 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY app.py .

# Railway/Heroku style PORT
ENV PORT=8000
EXPOSE 8000
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
