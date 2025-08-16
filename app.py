from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import os, pathlib, shutil, subprocess, shlex

import boto3
from botocore.config import Config

app = FastAPI()

# ---------- R2 (S3-compatible) ----------
def r2_client():
    """
    Creates an S3 client for Cloudflare R2 using env vars:
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    """
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

# ---------- Payload ----------
class Payload(BaseModel):
    userId: str
    jobId: str
    sourceKey: str   # e.g. "source/<uid>/<jobId>.mp3"

# ---------- Worker ----------
def process_job(user_id: str, job_id: str, source_key: str):
    s3 = r2_client()
    bucket = os.environ["R2_BUCKET"]
    processed_key = f"pre/{user_id}/{job_id}_proc.mp3"

    tmp = pathlib.Path("/tmp")
    src_tmp = tmp / f"{job_id}_src.mp3"
    wav_tmp = tmp / f"{job_id}_pitch.wav"
    out_tmp = tmp / f"{job_id}_proc.mp3"

    try:
        # 1) Download source from R2
        print(f"[worker] downloading s3://{bucket}/{source_key}")
        s3.download_file(bucket, source_key, str(src_tmp))

        # 2) Pitch shift -2 semitones (≈ -200 cents) with sox
        cmd1 = ["sox", str(src_tmp), str(wav_tmp), "pitch", "-200"]
        print("[worker] sox:", " ".join(map(shlex.quote, cmd1)))
        subprocess.run(cmd1, check=True)

        # 3) Encode to MP3 (libmp3lame @ 192kbps) with ffmpeg
        cmd2 = [
            "ffmpeg", "-y", "-i", str(wav_tmp),
            "-vn", "-acodec", "libmp3lame", "-b:a", "192k",
            str(out_tmp)
        ]
        print("[worker] ffmpeg:", " ".join(map(shlex.quote, cmd2)))
        subprocess.run(cmd2, check=True)

        # 4) Upload processed file back to R2 under pre/<uid>/...
        print(f"[worker] uploading s3://{bucket}/{processed_key}")
        s3.upload_file(
            str(out_tmp),
            bucket,
            processed_key,
            ExtraArgs={
                "ContentType": "audio/mpeg",
                "CacheControl": "public, max-age=31536000",
            },
        )

        public_base = os.environ.get("R2_PUBLIC_BASE", "")
        public_url = f"{public_base}/{processed_key}" if public_base else processed_key
        print(f"[worker] done -> {public_url}")

    except Exception as e:
        print(f"[worker] ERROR job={job_id}: {e}")
    finally:
        # Cleanup tmp files
        for p in (src_tmp, wav_tmp, out_tmp):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

# ---------- API ----------
@app.post("/process")
def process(payload: Payload, background: BackgroundTasks):
    """
    Accepts a job and returns immediately with 200.
    Actual work runs in the background.
    """
    background.add_task(process_job, payload.userId, payload.jobId, payload.sourceKey)
    return {"ok": True, "status": "ACCEPTED"}

@app.get("/health")
def health():
    return {"ok": True}
