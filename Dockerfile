FROM python:3.11-slim

# ---- System deps (FFmpeg for audio; libsndfile for demucs/soundfile) ----
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Python deps for API + R2 client ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Heavy ML deps (CPU wheels) for Demucs ----
# Uses PyTorch CPU index; installs demucs + soundfile + diffq (needed by mdx_q)
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision torchaudio \
 && pip install --no-cache-dir demucs soundfile diffq

# ---- App ----
COPY app.py .

# Keep CPU usage in check on small instances
ENV TORCH_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT}"]