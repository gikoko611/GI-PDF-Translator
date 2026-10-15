from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
for folder in (UPLOAD_DIR, OUTPUT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "15"))
FONT_PATH = Path(os.getenv("MYANMAR_FONT_PATH", "fonts/NotoSansMyanmar-Regular.ttf"))
if not FONT_PATH.is_absolute():
    FONT_PATH = BASE_DIR / FONT_PATH

app = FastAPI(title="G.I PDF Translator API", version="1.0.0")
allowed_origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# MVP job state lives in process memory. Configure one backend instance for this starter.
JOBS: dict[str, dict[str, Any]] = {}
TASKS: dict[str, asyncio.Task] = {}

PROVIDERS = {
    "gemini": {
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-2.5-flash",
    },
    "groq": {
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_model": "openai/gpt-oss-120b",
    },
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {k: job[k] for k in (
        "id", "filename", "provider", "status", "progress", "total_pages",
        "completed_pages", "message", "error", "created_at", "updated_at"
    )}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "G.I PDF Translator API",
        "providers": {
            name: bool(os.getenv(config["key_env"]))
            for name, config in PROVIDERS.items()
        },
        "myanmar_font_ready": FONT_PATH.exists(),
        "max_upload_mb": MAX_UPLOAD_MB,
    }


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    provider: str = Form(...),
):
    if provider not in PROVIDERS:
        raise HTTPException(400, "Choose Gemini or Groq.")
    config = PROVIDERS[provider]
    if not os.getenv(config["key_env"]):
        raise HTTPException(400, f"{provider.title()} API key is not configured on the backend.")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    job_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}.pdf"
    size = 0
    try:
        with input_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(413, f"PDF exceeds the {MAX_UPLOAD_MB} MB upload limit.")
                out.write(chunk)
    except Exception:
        input_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    try:
        with fitz.open(input_path) as doc:
            if doc.is_encrypted:
                raise HTTPException(400, "Password-protected PDFs are not supported.")
            total_pages = len(doc)
            text_pages = sum(1 for page in doc if page.get_text("text").strip())
            if total_pages == 0 or text_pages == 0:
                raise HTTPException(422, "No selectable text found. Scanned/image-only PDFs are not supported because OCR is disabled.")
    except HTTPException:
        input_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        input_path.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read PDF: {exc}") from exc

    job = {
        "id": job_id,
        "filename": Path(file.filename).name,
        "provider": provider,
        "status": "queued",
        "progress": 0,
        "total_pages": total_pages,
        "completed_pages": 0,
        "message": "PDF accepted. Preparing translation…",
        "error": None,
        "created_at": now(),
        "updated_at": now(),
        "input_path": str(input_path),
        "output_path": str(OUTPUT_DIR / f"{job_id}-myanmar.pdf"),
        "page_cache": {},
    }
    JOBS[job_id] = job
    TASKS[job_id] = asyncio.create_task(process_job(job_id))
    return public_job(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found. It may have expired after a server restart.")
    return public_job(job)


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found. Upload the PDF again.")
    if job["status"] == "processing":
        raise HTTPException(409, "This job is already processing.")
    if job["status"] == "completed":
        return public_job(job)
    config = PROVIDERS[job["provider"]]
    if not os.getenv(config["key_env"]):
        raise HTTPException(400, f"{job['provider'].title()} API key is not configured on the backend.")
    job.update(status="queued", error=None, message="Retrying unfinished pages…", updated_at=now())
    TASKS[job_id] = asyncio.create_task(process_job(job_id))
    return public_job(job)


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job["status"] != "completed" or not Path(job["output_path"]).exists():
        raise HTTPException(409, "Translated PDF is not ready yet.")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job["filename"]).stem)[:80] or "translated"
    return FileResponse(job["output_path"], media_type="application/pdf", filename=f"{safe_name}-myanmar.pdf")


async def translate_page(provider: str, blocks: list[dict[str, Any]]) -> list[str]:
    config = PROVIDERS[provider]
    api_key = os.getenv(config["key_env"], "")
    model = os.getenv(config["model_env"], config["default_model"])
    payload_text = json.dumps([b["text"] for b in blocks], ensure_ascii=False)
    system_prompt = (
        "You are a professional English-to-Myanmar (Burmese) translator. Translate every input item into natural, "
        "accurate Myanmar Unicode. Preserve names, numbers, URLs, code, citations, and technical terms where appropriate. "
        "Do not summarize, omit, or combine items. Return ONLY a JSON array of strings with exactly the same number and order of items."
    )
    user_prompt = "Translate these text blocks into Myanmar. Keep one translation per input item:\n" + payload_text
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0)) as client:
        last_error = None
        for attempt in range(3):
            try:
                if provider == "gemini":
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    response = await client.post(
                        url,
                        params={"key": api_key},
                        json={"systemInstruction": {"parts": [{"text": system_prompt}]},
                              "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                              "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}},
                    )
                    response.raise_for_status()
                    data = response.json()
                    answer = data["candidates"][0]["content"]["parts"][0]["text"]
                else:
                    groq_system = system_prompt + " Return a JSON object with one key named translations; its value must be the array of translated strings."
                    response = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"model": model,
                              "messages": [{"role": "system", "content": groq_system},
                                           {"role": "user", "content": user_prompt}],
                              "temperature": 0.2,
                              "response_format": {"type": "json_object"}},
                    )
                    response.raise_for_status()
                    answer = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(answer)
                if isinstance(parsed, dict):
                    parsed = parsed.get("translations")
                if not isinstance(parsed, list) or len(parsed) != len(blocks) or not all(isinstance(x, str) for x in parsed):
                    raise ValueError("AI response did not contain the expected number of translated strings.")
                return parsed
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{provider.title()} translation failed after retries: {last_error}")


def page_text_blocks(page: fitz.Page) -> list[dict[str, Any]]:
    result = []
    for block in page.get_text("dict")["blocks"]:
        if "lines" not in block:
            continue
        text = "".join(span["text"] for line in block["lines"] for span in line["spans"]).strip()
        if not text:
            continue
        bbox = block.get("bbox")
        if not bbox:
            continue
        result.append({"text": text, "bbox": bbox})
    return result


async def process_job(job_id: str):
    job = JOBS[job_id]
    job.update(status="processing", error=None, message="Extracting text…", updated_at=now())
    source = Path(job["input_path"])
    output = Path(job["output_path"])
    temp_output = OUTPUT_DIR / f"{job_id}-working.pdf"
    try:
        if not FONT_PATH.exists():
            raise RuntimeError(
                "Myanmar font is missing. Add NotoSansMyanmar-Regular.ttf to backend/fonts/ "
                "or set MYANMAR_FONT_PATH to a valid Unicode Myanmar TTF font."
            )
        with fitz.open(source) as doc:
            total = len(doc)
            for index, page in enumerate(doc):
                if index in job["page_cache"]:
                    continue
                blocks = page_text_blocks(page)
                if not blocks:
                    job["page_cache"][index] = []
                    job["completed_pages"] = len(job["page_cache"])
                    job["progress"] = round(job["completed_pages"] / total * 100)
                    job["message"] = f"Page {index + 1}/{total}: no selectable text; keeping page unchanged."
                    job["updated_at"] = now()
                    continue
                job["message"] = f"Translating page {index + 1} of {total}…"
                job["updated_at"] = now()
                translations = await translate_page(job["provider"], blocks)
                job["page_cache"][index] = [
                    {"bbox": b["bbox"], "translation": t}
                    for b, t in zip(blocks, translations)
                ]
                job["completed_pages"] = len(job["page_cache"])
                job["progress"] = round(job["completed_pages"] / total * 100)
                job["updated_at"] = now()

        # Reopen the original and replace text blocks, preserving page size and other page content where possible.
        with fitz.open(source) as doc:
            for index, page in enumerate(doc):
                translated_blocks = job["page_cache"].get(index, [])
                if not translated_blocks:
                    continue
                # White redactions cover the original text region; images and vector graphics are retained.
                for item in translated_blocks:
                    page.add_redact_annot(fitz.Rect(item["bbox"]), fill=(1, 1, 1), cross_out=False)
                page.apply_redactions(images=0, graphics=0, text=0)
                for item in translated_blocks:
                    rect = fitz.Rect(item["bbox"])
                    rect.x0 += 1.5
                    rect.x1 -= 1.5
                    rect.y0 += 0.5
                    rect.y1 -= 0.5
                    text = item["translation"].strip()
                    # Shrink text slightly if necessary to fit the original block rectangle.
                    fontsize = 10.5
                    result = page.insert_textbox(
                        rect, text, fontname="myanmar", fontfile=str(FONT_PATH),
                        fontsize=fontsize, color=(0.08, 0.08, 0.08), align=0, lineheight=1.15,
                    )
                    while result < 0 and fontsize > 5.5:
                        # Remove the previous failed insertion is not needed: insert_textbox writes nothing when it fails.
                        fontsize -= 0.5
                        result = page.insert_textbox(
                            rect, text, fontname="myanmar", fontfile=str(FONT_PATH),
                            fontsize=fontsize, color=(0.08, 0.08, 0.08), align=0, lineheight=1.1,
                        )
                    if result < 0:
                        # Prevent silent loss: write in a larger area below the original block as a fallback.
                        fallback = fitz.Rect(rect.x0, rect.y0, min(page.rect.x1 - 8, rect.x1 + 12), min(page.rect.y1 - 8, rect.y1 + 28))
                        page.insert_textbox(fallback, text, fontname="myanmar", fontfile=str(FONT_PATH), fontsize=5.5, color=(0.08, 0.08, 0.08))
            doc.save(temp_output, garbage=4, deflate=True)
        shutil.move(str(temp_output), str(output))
        job.update(status="completed", progress=100, completed_pages=job["total_pages"], message="Translation complete. Your Myanmar PDF is ready.", error=None, updated_at=now())
    except Exception as exc:
        temp_output.unlink(missing_ok=True)
        job.update(status="failed", error=str(exc), message="Translation paused. Retry to continue unfinished pages.", updated_at=now())
    finally:
        task = TASKS.get(job_id)
        if task and task.done():
            TASKS.pop(job_id, None)


@app.on_event("shutdown")
async def cleanup_tasks():
    for task in TASKS.values():
        if not task.done():
            task.cancel()
