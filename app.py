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

class Payload(BaseModel):
    userId: str
    jobId: str
    sourceKey: str  # e.g. "source/<uid>/<jobId>.mp3"

def run(cmd: list):
    print("[worker]", " ".join(map(shlex.quote, cmd)))
    subprocess.run(cmd, check=True)

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

    # Filter graph:
    #  1) Trim to 110s
    #  2) Pitch down, keep duration: asetrate -> resample -> atempo
    #  3) Split to [a] and [b]
    #  4) [a] make MID (≈ vocals): (FL+FR)/2, send to LEFT only at 30%, mute RIGHT
    #  5) [b] make SIDE (≈ instruments): (FL-FR, FR-FL), keep stereo
    #  6) Mix [v] + [i] without normalization
    #
    # Notes:
    #  - Use FL/FR everywhere (no c0/c1) to avoid "mix named and numbered channels".
    #  - Avoid bare constants; when we need zero, use 0*FR (accepted by strict builds).
    filter_graph = (
        f"[0:a]"
        f"atrim=0:110,asetpts=N/SR/TB,"
        f"asetrate={sr}/{pitch_factor},aresample={sr},atempo={pitch_factor},"
        f"aformat=channel_layouts=stereo,asplit=2[a][b];"
        # Vocals branch: 0.30 * ((FL+FR)/2) -> LEFT; RIGHT = 0
        # 0.30 * 0.5 = 0.15, multiply each channel then sum
        f"[a]pan=stereo|FL=0.15*FL+0.15*FR|FR=0*FR[v];"
        # Instruments branch: SIDE (L-R, R-L)
        f"[b]pan=stereo|FL=0.5*FL-0.5*FR|FR=0.5*FR-0.5*FL[i];"
        # Mix the two stereo streams
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
        for p in (src, out):
            try:
                if p.exists():
                    p.unlink()
            except:
                pass

@app.post("/process")
def process(payload: Payload, background: BackgroundTasks):
    background.add_task(process_job, payload.userId, payload.jobId, payload.sourceKey)
    return {"ok": True, "status": "ACCEPTED"}

@app.get("/health")
def health():
    return {"ok": True}
