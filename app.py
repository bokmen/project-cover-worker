from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import os, shutil, pathlib, uuid
import boto3
from botocore.config import Config

app = FastAPI()

# --- R2 client (S3-compatible) ---
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
    sourceKey: str  # e.g. source/<uid>/<jobId>.mp3

def process_job(user_id: str, job_id: str, source_key: str):
    s3 = r2_client()
    bucket = os.environ["R2_BUCKET"]
    processed_key = f"pre/{user_id}/{job_id}_proc.mp3"

    tmp = pathlib.Path("/tmp")
    src_tmp = tmp / f"{job_id}_src.mp3"
    out_tmp = tmp / f"{job_id}_proc.mp3"

    try:
        print(f"[worker] downloading s3://{bucket}/{source_key}")
        s3.download_file(bucket, source_key, str(src_tmp))

        # TODO: audio processing will go here (pitch/stems/remix)
        shutil.copyfile(src_tmp, out_tmp)

        print(f"[worker] uploading s3://{bucket}/{processed_key}")
        s3.upload_file(
            str(out_tmp),
            bucket,
            processed_key,
            ExtraArgs={
                "ContentType": "audio/mpeg",
                "CacheControl": "public, max-age=31536000",
                # leave ACL empty (R2 ignores public ACLs); keep pre/ private by bucket policy
            },
        )
        public_base = os.environ.get("R2_PUBLIC_BASE", "")
        public_url = f"{public_base}/{processed_key}" if public_base else processed_key
        print(f"[worker] done -> {public_url}")
    except Exception as e:
        print(f"[worker] ERROR job={job_id}: {e}")
    finally:
        for p in (src_tmp, out_tmp):
            try:
                if p.exists(): p.unlink()
            except Exception:
                pass

@app.post("/process")
def process(payload: Payload, background: BackgroundTasks):
    background.add_task(process_job, payload.userId, payload.jobId, payload.sourceKey)
    return {"ok": True, "status": "ACCEPTED"}