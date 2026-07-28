"""Thin HTTP front-end for the unlimited-ocr batch Job.

POST a PDF to /v1/ocr; this creates a Kubernetes Job identical in shape to the
one templated by charts/unlimited-ocr/templates/job.yaml, waits for it to
finish, converts the grounding output to clean Markdown (reusing the same
converter the local `ocrpdf` wrapper uses), and returns it.
"""
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from kubernetes import client, config

from grounding_to_markdown import load_pages, to_markdown

NAMESPACE = os.environ.get("OCR_NAMESPACE", "default")
IMAGE = os.environ["OCR_IMAGE"]
RUNTIME_CLASS = os.environ.get("OCR_RUNTIME_CLASS", "")
GPU_COUNT = os.environ.get("OCR_GPU_COUNT", "")
GPU_RESOURCE_NAME = os.environ.get("OCR_GPU_RESOURCE_NAME", "nvidia.com/gpu")
HOST_DATA_DIR = os.environ["OCR_HOST_DATA_DIR"]
HOST_OUTPUTS_DIR = os.environ["OCR_HOST_OUTPUTS_DIR"]
HOST_LOG_DIR = os.environ["OCR_HOST_LOG_DIR"]
HOST_MODELS_DIR = os.environ["OCR_HOST_MODELS_DIR"]
DEFAULT_CONCURRENCY = os.environ.get("OCR_CONCURRENCY", "8")
DEFAULT_IMAGE_MODE = os.environ.get("OCR_IMAGE_MODE", "gundam")
JOB_TTL_SECONDS = int(os.environ.get("OCR_JOB_TTL_SECONDS", "600"))
JOB_ACTIVE_DEADLINE = int(os.environ.get("OCR_JOB_ACTIVE_DEADLINE", "1800"))
POLL_TIMEOUT_SECONDS = int(os.environ.get("OCR_POLL_TIMEOUT_SECONDS", "900"))
API_KEY = os.environ.get("OCR_API_KEY", "")

DATA_DIR = Path("/data")
OUTPUTS_DIR = Path("/outputs")

config.load_incluster_config()
batch_v1 = client.BatchV1Api()

app = FastAPI(title="unlimited-ocr-api")


class JobFailed(Exception):
    pass


class JobTimeout(Exception):
    pass


def check_api_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def build_job(request_id: str, image_mode: str, concurrency: int):
    args = [
        "--pdf", f"/data/api-{request_id}/input.pdf",
        "--output_dir", f"/workspace/outputs/api-{request_id}",
        "--image_mode", image_mode,
    ]
    if RUNTIME_CLASS:
        args += [
            "--concurrency", str(concurrency),
            "--gpu", "0",
            "--server_log", f"/workspace/log/sglang-api-{request_id}.log",
        ]

    container = client.V1Container(
        name="unlimited-ocr",
        image=IMAGE,
        image_pull_policy="IfNotPresent",
        args=args,
        volume_mounts=[
            client.V1VolumeMount(name="data", mount_path="/data", read_only=True),
            client.V1VolumeMount(name="outputs", mount_path="/workspace/outputs"),
            client.V1VolumeMount(name="log", mount_path="/workspace/log"),
            client.V1VolumeMount(name="models", mount_path="/models"),
        ],
    )
    if RUNTIME_CLASS and GPU_COUNT:
        container.resources = client.V1ResourceRequirements(
            limits={GPU_RESOURCE_NAME: GPU_COUNT}
        )

    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        runtime_class_name=RUNTIME_CLASS or None,
        containers=[container],
        volumes=[
            client.V1Volume(
                name="data",
                host_path=client.V1HostPathVolumeSource(path=HOST_DATA_DIR, type="Directory"),
            ),
            client.V1Volume(
                name="outputs",
                host_path=client.V1HostPathVolumeSource(path=HOST_OUTPUTS_DIR, type="DirectoryOrCreate"),
            ),
            client.V1Volume(
                name="log",
                host_path=client.V1HostPathVolumeSource(path=HOST_LOG_DIR, type="DirectoryOrCreate"),
            ),
            client.V1Volume(
                name="models",
                host_path=client.V1HostPathVolumeSource(path=HOST_MODELS_DIR, type="DirectoryOrCreate"),
            ),
        ],
    )
    job_name = f"ocr-api-{request_id}"
    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=job_name,
            labels={"app.kubernetes.io/name": "unlimited-ocr-api-job"},
        ),
        spec=client.V1JobSpec(
            backoff_limit=0,
            ttl_seconds_after_finished=JOB_TTL_SECONDS,
            active_deadline_seconds=JOB_ACTIVE_DEADLINE,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"app.kubernetes.io/name": "unlimited-ocr-api-job"}
                ),
                spec=pod_spec,
            ),
        ),
    )
    return job, job_name


def wait_for_job(job_name: str, timeout: int = POLL_TIMEOUT_SECONDS, interval: float = 2.0):
    start = time.time()
    while time.time() - start < timeout:
        status = batch_v1.read_namespaced_job_status(name=job_name, namespace=NAMESPACE).status
        if status.succeeded:
            return
        if status.failed:
            raise JobFailed(f"job {job_name} failed")
        time.sleep(interval)
    raise JobTimeout(f"job {job_name} timed out after {timeout}s")


def cleanup_job(job_name: str):
    try:
        batch_v1.delete_namespaced_job(
            name=job_name,
            namespace=NAMESPACE,
            body=client.V1DeleteOptions(propagation_policy="Background"),
        )
    except client.exceptions.ApiException:
        pass


@app.get("/v1/health")
def health():
    return {"status": "ok"}


@app.post("/v1/ocr")
def ocr(
    file: UploadFile = File(...),
    image_mode: str = Query(DEFAULT_IMAGE_MODE, pattern="^(gundam|base)$"),
    concurrency: int = Query(int(DEFAULT_CONCURRENCY), ge=1, le=32),
    raw: bool = Query(False, description="return raw grounding output instead of cleaned GFM markdown"),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "only .pdf uploads are supported")

    request_id = uuid.uuid4().hex[:12]
    in_dir = DATA_DIR / f"api-{request_id}"
    out_dir = OUTPUTS_DIR / f"api-{request_id}"
    in_dir.mkdir(parents=True, exist_ok=True)

    with (in_dir / "input.pdf").open("wb") as f:
        shutil.copyfileobj(file.file, f)

    job_manifest, job_name = build_job(request_id, image_mode, concurrency)

    try:
        batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job_manifest)
        wait_for_job(job_name)

        page_files = sorted(out_dir.glob("*_page_*.md"))
        if not page_files:
            raise HTTPException(500, "job completed but produced no output")

        if raw:
            text = "\n\n".join(p.read_text(encoding="utf-8") for p in page_files)
        else:
            pages = load_pages([str(out_dir)])
            text = to_markdown(pages, gfm=True)

        return PlainTextResponse(
            text, media_type="text/markdown", headers={"X-Request-Id": request_id}
        )
    except JobFailed as e:
        raise HTTPException(502, f"OCR job failed: {e}")
    except JobTimeout as e:
        raise HTTPException(504, str(e))
    finally:
        cleanup_job(job_name)
        shutil.rmtree(in_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
