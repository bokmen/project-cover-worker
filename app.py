from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import os, pathlib, subprocess, shlex

import boto3
from botocore.config import Config

app = FastAPI()

# ---------- R2 ----------
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

def ffmpeg_has_rubberband() -> bool:
    try:
        out = subprocess.check_output(["ffmpeg","-hide_banner","-filters"], text=True)
        return "rubberband" in out
    except Exception:
        return False

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
        # 0) download
        print(f"[worker] downloading s3://{bucket}/{source_key}")
        s3.download_file(bucket, source_key, str(src))

        # 1) Trim to 110s (stream copy)
        run(["ffmpeg","-y","-i",str(src),"-t","110","-c","copy",str(t110)])

        # 2) Pitch -2 semitones
        if ffmpeg_has_rubberband():
            # Your preferred path (higher quality)
            af = "rubberband=pitch=-2"
            run(["ffmpeg","-y","-i",str(t110),"-af",af,"-c:a","libmp3lame","-q:a","2",str(pitch)])
        else:
            # Fallback: sample-rate trick + atempo
            pitch_factor = 2 ** (-2/12)       # ~0.890898718
            af = f"asetrate=48000/{pitch_factor},aresample=48000,atempo={pitch_factor}"
            run(["ffmpeg","-y","-i",str(t110),"-vn","-af",af,"-acodec","libmp3lame","-b:a","192k",str(pitch)])

        # 3) Demucs (CPU) -> vocals / no_vocals
        demucs_out = tmp / "demucs_out"
        if demucs_out.exists():
            # clean stale runs
            for p in demucs_out.rglob("*"):
                try: p.unlink()
                except: pass
            try: demucs_out.rmdir()
            except: pass
        run(["python","-m","demucs","--two-stems=vocals","-o",str(demucs_out),str(pitch)])

        # Paths demucs uses: demucs_out/htdemucs/<basename>/{vocals,no_vocals}.wav
        base = pitch.stem
        voc_path  = demucs_out / "htdemucs" / base / "vocals.wav"
        inst_path = demucs_out / "htdemucs" / base / "no_vocals.wav"
        if not (voc_path.exists() and inst_path.exists()):
            raise RuntimeError("Demucs output not found")

        # Copy to stable tmp names
        voc.write_bytes(voc_path.read_bytes())
        inst.write_bytes(inst_path.read_bytes())

        # 4) Mix: vocals L@30% (mute R), instruments centered; export MP3 @ q=4
        #   [voc] volume 0.30, pan: L=c0, R=0 (mute)
        #   [inst] pan: L=c0, R=c1 (center)
        filt = (
            "[0:a]aformat=channel_layouts=stereo,volume=0.30,pan=stereo|c0=c0|c1=0[v];"
            "[1:a]aformat=channel_layouts=stereo,pan=stereo|c0=c0|c1=c1[i];"
            "[v][i]amix=inputs=2:normalize=0[mix]"
        )
        run([
            "ffmpeg","-y",
            "-i",str(voc),"-i",str(inst),
            "-filter_complex",filt,
            "-map","[mix]","-c:a","libmp3lame","-q:a","4",
            str(out)
        ])

        # 5) upload
        print(f"[worker] uploading s3://{bucket}/{out_key}")
        s3.upload_file(
            str(out),
            bucket,
            out_key,
            ExtraArgs={"ContentType":"audio/mpeg","CacheControl":"public, max-age=31536000"},
        )
        url = f"{public}/{out_key}" if public else out_key
        print(f"[worker] done -> {url}")

    except Exception as e:
        print(f"[worker] ERROR job={job_id}: {e}")

    finally:
        # cleanup tmp
        for p in (src,t110,pitch,voc,inst,out):
            try:
                if p.exists(): p.unlink()
            except: pass

@app.post("/process")
def process(payload: Payload, background: BackgroundTasks):
    background.add_task(process_job, payload.userId, payload.jobId, payload.sourceKey)
    return {"ok": True, "status": "ACCEPTED"}

@app.get("/health")
def health():
    return {"ok": True}
