from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import os, pathlib, subprocess, shlex

import boto3
from botocore.config import Config

app = FastAPI()

# -------- R2 (S3-compatible) --------
def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

# Default HQ toggle via env; request can override
HQ_DEFAULT = os.getenv("HQ_DEFAULT", "0") == "1"

class Payload(BaseModel):
    userId: str
    jobId: str
    sourceKey: str              # e.g. "source/<uid>/<jobId>.mp3"
    hq: bool = False            # pass true to enable Demucs HQ

def run(cmd: list, timeout: int | None = None):
    print("[worker]", " ".join(map(shlex.quote, cmd)))
    subprocess.run(cmd, check=True, timeout=timeout)

def has_rubberband() -> bool:
    try:
        out = subprocess.check_output(["ffmpeg", "-hide_banner", "-filters"], text=True)
        return "rubberband" in out
    except Exception:
        return False

# ---------- FAST path (single-pass FFmpeg) ----------
def fast_ffmpeg(src_path: pathlib.Path, out_path: pathlib.Path):
    # -2 semitones
    pitch = 2 ** (-2 / 12.0)     # ~0.890898718
    sr = 48000

    # One-pass:
    #   trim 0..110s -> pitch down (duration preserved) ->
    #   split -> build MID (≈ vocals) and SIDE (≈ instruments) -> mix
    #
    # Use FL/FR only; avoid parentheses and bare constants.
    filter_graph = (
        f"[0:a]"
        f"atrim=0:110,asetpts=N/SR/TB,"
        f"asetrate={sr}/{pitch},aresample={sr},atempo={pitch},"
        f"aformat=channel_layouts=stereo,asplit=2[a][b];"
        # Vocals ~ MID = (FL+FR)/2; send left @ 30% (0.15*FL + 0.15*FR); right muted (0*FR)
        f"[a]pan=stereo|FL=0.15*FL+0.15*FR|FR=0*FR[v];"
        # Instruments ~ SIDE = (FL-FR, FR-FL)
        f"[b]pan=stereo|FL=0.5*FL-0.5*FR|FR=0.5*FR-0.5*FL[i];"
        f"[v][i]amix=inputs=2:normalize=0[mix]"
    )

    run([
        "ffmpeg","-y",
        "-i", str(src_path),
        "-filter_complex", filter_graph,
        "-map","[mix]",
        "-acodec","libmp3lame","-b:a","192k",
        str(out_path)
    ])

# ---------- HQ path (Demucs on pitched audio) ----------
def hq_demucs(src_path: pathlib.Path, out_path: pathlib.Path):
    tmp = src_path.parent
    t110  = tmp / f"{src_path.stem}_t110.mp3"
    pitch = tmp / f"{src_path.stem}_dn2.mp3"

    # 1) Trim to 110s (stream copy)
    run(["ffmpeg","-y","-i",str(src_path),"-t","110","-c","copy",str(t110)])

    # 2) Pitch −2 semitones (prefer rubberband if present)
    if has_rubberband():
        ratio = 2 ** (-2 / 12.0)  # ~0.890898718
        af = f"rubberband=pitch={ratio}:tempo=1"
        run(["ffmpeg","-y","-i",str(t110),"-af",af,"-c:a","libmp3lame","-q:a","2",str(pitch)])
    else:
        factor = 2 ** (-2 / 12.0)
        af = f"asetrate=48000/{factor},aresample=48000,atempo={factor}"
        run(["ffmpeg","-y","-i",str(t110),"-vn","-af",af,"-acodec","libmp3lame","-b:a","192k",str(pitch)])

    # 3) Demucs mdx (CPU-friendly), timeout & fallback-friendly
    demucs_out = tmp / "demucs_out"
    if demucs_out.exists():
        for p in demucs_out.rglob("*"):
            try: p.unlink()
            except: pass
        try: demucs_out.rmdir()
        except: pass

    demucs_cmd = [
        "python","-m","demucs",
        "-n","mdx",                 # no diffq dependency
        "--two-stems","vocals",
        "--device","cpu",
        "--jobs","1",
        "--shifts","0",
        "--segment","8",           # smaller segments -> less RAM
        "-o",str(demucs_out),
        str(pitch),
    ]
    try:
        run(demucs_cmd, timeout=480)   # up to 8 minutes on small CPU
    except Exception as e:
        print(f"[worker] demucs failed, fallback to FAST: {e}")
        fast_ffmpeg(src_path, out_path)
        return

    # Locate outputs (model dir name can vary)
    base = pitch.stem
    voc_path = inst_path = None
    for d in [p for p in demucs_out.iterdir() if p.is_dir()]:
        pv = d / base / "vocals.wav"
        pi = d / base / "no_vocals.wav"
        if pv.exists() and pi.exists():
            voc_path, inst_path = pv, pi
            break
    if not (voc_path and inst_path):
        hits_v = list(demucs_out.rglob("vocals.wav"))
        hits_i = list(demucs_out.rglob("no_vocals.wav"))
        if hits_v and hits_i:
            voc_path, inst_path = hits_v[0], hits_i[0]
    if not (voc_path and inst_path):
        print("[worker] demucs outputs missing, fallback to FAST")
        fast_ffmpeg(src_path, out_path)
        return

    # 4) Mix: vocals LEFT @ 30%, instruments centered
    # Use FL/FR only; no bare constants.
    filt = (
        "[0:a]aformat=channel_layouts=stereo,"
        "pan=stereo|FL=0.3*FL|FR=0*FR[v];"
        "[1:a]aformat=channel_layouts=stereo,"
        "pan=stereo|FL=FL|FR=FR[i];"
        "[v][i]amix=inputs=2:normalize=0[mix]"
    )
    run([
        "ffmpeg","-y",
        "-i",str(voc_path),"-i",str(inst_path),
        "-filter_complex",filt,
        "-map","[mix]","-c:a","libmp3lame","-q:a","4",
        str(out_path)
    ])

def process_job(user_id: str, job_id: str, source_key: str, hq: bool):
    s3 = r2_client()
    bucket = os.environ["R2_BUCKET"]
    mode = "HQ" if hq else "FAST"
    out_key = f"pre/{user_id}/{job_id}_proc_{mode.lower()}.mp3"
    public = os.environ.get("R2_PUBLIC_BASE","")

    tmp = pathlib.Path("/tmp")
    src = tmp / f"{job_id}_src.mp3"
    out = tmp / f"{job_id}_proc.mp3"

    try:
        print(f"[worker] downloading s3://{bucket}/{source_key}")
        s3.download_file(bucket, source_key, str(src))

        if hq:
            hq_demucs(src, out)
        else:
            fast_ffmpeg(src, out)

        print(f"[worker] uploading s3://{bucket}/{out_key}")
        s3.upload_file(
            str(out), bucket, out_key,
            ExtraArgs={"ContentType":"audio/mpeg","CacheControl":"public, max-age=31536000"},
        )
        url = f"{public}/{out_key}" if public else out_key
        print(f"[worker] done -> {url}")

    except Exception as e:
        print(f"[worker] ERROR job={job_id}: {e}")
    finally:
        for p in (src, out):
            try:
                if p.exists(): p.unlink()
            except: pass

# ---------- API ----------
@app.post("/process")
def process(payload: Payload, background: BackgroundTasks):
    use_hq = payload.hq or HQ_DEFAULT
    background.add_task(process_job, payload.userId, payload.jobId, payload.sourceKey, use_hq)
    return {"ok": True, "status": "ACCEPTED", "mode": "HQ" if use_hq else "FAST"}

@app.get("/health")
def health():
    return {"ok": True}