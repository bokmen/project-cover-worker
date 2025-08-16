FROM python:3.11-slim

# System deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps for API + R2
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Heavy ML deps (CPU)
# (builds a larger image; allow a few minutes on Railway)
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision torchaudio \
 && pip install --no-cache-dir demucs soundfile

# App
COPY app.py .

ENV PORT=8000
EXPOSE 8000
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
