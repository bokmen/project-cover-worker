from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import os, pathlib, subprocess, shlex

import boto3
from botocore.config import Config

app = FastAPI()

# ---------- R2 (S3-compatible) ----------
def r2_client():
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
    sourceKey: str  # e.g. "source/<uid>/<jobId>.mp3"

# ---------- Helpers ----------
def run(cmd: list):
    print("[worker]", " ".join(map(shlex.quote, cmd)))
    subprocess.run(cmd, check=True)

# ---------- Worker ----------
def process_job(user_id: str, job_id: str, source_key: str):
    s3 = r2_client()
    bucket = os.environ["R2_BUCKET"]
    out_key = f"pre/{user_id}/{job_id}_proc.mp3"
    public = os.environ.get("R2_PUBLIC_BASE", "")

    tmp = pathlib.Path("/tmp")
    src = tmp / f"{job_id}_src.mp3"
    out = tmp / f"{job_id}_proc.mp3"

    # Pitch math (−2 semitones)
    pitch_factor = 2 ** (-2 / 12.0)    # ≈ 0.890898718
    sr = 48000

    # One-pass filter graph:
    #  - trim to 110s
    #  - pitch down, keep duration: asetrate=sr/ratio -> resample -> atempo=ratio
    #  - split to two copies
    #  - [a] mid (≈ vocals): (L+R)/2, left-only @30%
    #  - [b] side (≈ instruments): stereo side (L-R, R-L), centered
    #  - mix [v] + [i]
    filter_graph = (
        f"[0:a]atrim=0:110,asetpts=N/SR/TB,"
        f"asetrate={sr}/{pitch_factor},aresample={sr},atempo={pitch_factor},"
        f"asplit=2[a][b];"
        f"[a]aformat=channel_layouts=stereo,"
        f"pan=stereo|c0=0.30*(0.5*c0+0.5*c1)|c1=0[v];"
        f"[b]aformat=channel_layouts=stereo,"
        f"pan=stereo|c0=0.5*c0-0.5*c1|c1=0.5*c1-0.5*c0[i];"
        f"[v][i]amix=inputs=2:normalize=0[mix]"
    )

    try:
        print(f"[worker] downloading s3://{bucket}/{source_key}")
        s3.download_file(bucket, source_key, str(src))

        cmd = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-filter_complex", filter_graph,
            "-map", "[mix]",
            "-acodec", "libmp3lame", "-b:a", "192k",
            str(out),
        ]
        run(cmd)

        print(f"[worker] uploading s3://{bucket}/{out_key}")
        s3.upload_file(
            str(out),
            bucket,
            out_key,
            ExtraArgs={"ContentType": "audio/mpeg", "CacheControl": "public, max-age=31536000"},
        )
        url = f"{public}/{out_key}" if public else out_key
        print(f"[worker] done -> {url}")

    except Exception as e:
        print(f"[worker] ERROR job={job_id}: {e}")
    finally:
        try:
            if src.exists(): src.unlink()
        except: pass
        try:
            if out.exists(): out.unlink()
        except: pass

# ---------- API ----------
@app.post("/process")
def process(payload: Payload, background: BackgroundTasks):
    background.add_task(process_job, payload.userId, payload.jobId, payload.sourceKey)
    return {"ok": True, "status": "ACCEPTED"}

@app.get("/health")
def health():
    return {"ok": True}