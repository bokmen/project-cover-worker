from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel

app = FastAPI()

class Payload(BaseModel):
    userId: str
    jobId: str
    sourceKey: str  # e.g., "source/<uid>/<jobId>.mp3"

def process_job(user_id: str, job_id: str, source_key: str):
    # we'll fill this in later (download from R2 -> process -> upload to pre/)
    print(f"[worker] accepted job user={user_id} job={job_id} source={source_key}")

@app.post("/process")
def process(payload: Payload, background: BackgroundTasks):
    background.add_task(process_job, payload.userId, payload.jobId, payload.sourceKey)
    return {"ok": True, "status": "ACCEPTED"}