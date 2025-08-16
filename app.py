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

def ffmpeg_has_rubberband() -> bool:
    try:
        out = subprocess.check_output(["ffmpeg", "-hide_banner", "-filters"], text=True)
        return "rubberband" in out
    except Exception:
        return False

# ---------- Worker ----------
def process_job(user_id: str, job_id: str, source_key: str):
    s3 = r2_client()
    bucket = os.environ["R2_BUCKET"]
    out_key = f"pre/{user_id}/{job_id}_proc.mp3"
    public = os.environ.get("R2_PUBLIC_BASE", "")

    tmp = pathlib.Path("/tmp")
    src   = tmp / f"{job_id}_src.mp3"
    t110  = tmp / f"{job_id}_t110.mp3"
    pitch = tmp / f"{job_id}_dn2.mp3"
    voc   = tmp / f"{job_id}_vocals.wav"
    inst  = tmp / f"{job_id}_no_vocals.wav"
    out   = tmp / f"{job_id}_proc.mp3"

    try:
        # 0) Download source
        print(f"[worker] downloading s3://{bucket}/{source_key}")
        s3.download_file(bucket, source_key, str(src))

        # 1) Trim to 110s (stream copy)
        run(["ffmpeg", "-y", "-i", str(src), "-t", "110", "-c", "copy", str(t110)])

        # 2) Pitch -2 semitones
        if ffmpeg_has_rubberband():
            # rubberband expects ratio; keep duration with tempo=1
            semitones = -2.0
            ratio = 2 ** (semitones / 12.0)  # ≈ 0.890898718
            af = f"rubberband=pitch={ratio}:tempo=1"
            run(["ffmpeg", "-y", "-i", str(t110), "-af", af, "-c:a", "libmp3lame", "-q:a", "2", str(pitch)])
        else:
            # Fallback: asetrate/atempo trick
            pitch_factor = 2 ** (-2 / 12)       # ~0.890898718
            af = f"asetrate=48000/{pitch_factor},aresample=48000,atempo={pitch_factor}"
            run(["ffmpeg", "-y", "-i", str(t110), "-vn", "-af", af, "-acodec", "libmp3lame", "-b:a", "192k", str(pitch)])

        # 3) Demucs (CPU) -> vocals / no_vocals (faster model + CPU-friendly settings)
        demucs_out = tmp / "demucs_out"
        if demucs_out.exists():
            for p in demucs_out.rglob("*"):
                try: p.unlink()
                except: pass
            try: demucs_out.rmdir()
            except: pass

        run([
            "python", "-m", "demucs",
            "-n", "mdx",          # faster than htdemucs
            "--two-stems", "vocals",
            "--device", "cpu",
            "--jobs", "1",
            "--shifts", "0",
            "--segment", "10",
            "-o", str(demucs_out),
            str(pitch),
        ])

        # Locate Demucs outputs (model dir can vary)
        base = pitch.stem
        model_dirs = [d for d in demucs_out.iterdir() if d.is_dir()]
        voc_path = inst_path = None
        for d in model_dirs:
            p_v = d / base / "vocals.wav"
            p_i = d / base / "no_vocals.wav"
            if p_v.exists() and p_i.exists():
                voc_path, inst_path = p_v, p_i
                break
        if not (voc_path and inst_path):
            # fallback: search recursively
            hits_v = list(demucs_out.rglob("vocals.wav"))
            hits_i = list(demucs_out.rglob("no_vocals.wav"))
            if hits_v and hits_i:
                voc_path, inst_path = hits_v[0], hits_i[0]
        if not (voc_path and inst_path):
            raise RuntimeError("Demucs output not found")

        voc.write_bytes(voc_path.read_bytes())
        inst.write_bytes(inst_path.read_bytes())

        # 4) Mix: vocals left @30% (mute right), instruments centered
        filt = (
            "[0:a]aformat=channel_layouts=stereo,volume=0.30,pan=stereo|c0=c0|c1=0[v];"
            "[1:a]aformat=channel_layouts=stereo,pan=stereo|c0=c0|c1=c1[i];"
            "[v][i]amix=inputs=2:normalize=0[mix]"
        )
        run([
            "ffmpeg", "-y",
            "-i", str(voc), "-i", str(inst),
            "-filter_complex", filt,
            "-map", "[mix]", "-c:a", "libmp3lame", "-q:a", "4",
            str(out)
        ])

        # 5) Upload result
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
        for p in (src, t110, pitch, voc, inst, out):
            try:
                if p.exists(): p.unlink()
            except: pass

# ---------- API ----------
@app.post("/process")
def process(payload: Payload, background: BackgroundTasks):
    background.add_task(process_job, payload.userId, payload.jobId, payload.sourceKey)
    return {"ok": True, "status": "ACCEPTED"}

@app.get("/health")
def health():
    return {"ok": True}
