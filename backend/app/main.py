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
CHECKPOINT_DIR = DATA_DIR / "checkpoints"
for folder in (UPLOAD_DIR, OUTPUT_DIR, CHECKPOINT_DIR):
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


def save_job_checkpoint(job: dict[str, Any]) -> None:
    """Atomically save job metadata without duplicating page translations."""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKPOINT_DIR / f"{job['id']}.json"
    temp = path.with_suffix(".tmp")
    metadata = {k: v for k, v in job.items() if k != "page_cache"}
    temp.write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(path)


def save_page_checkpoint(job: dict[str, Any], index: int) -> None:
    """Atomically persist one translated page (zero-based page index)."""
    folder = CHECKPOINT_DIR / job["id"] / "pages"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{index}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(job["page_cache"][index], ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(path)


def restore_checkpoint_jobs() -> None:
    """Restore jobs after a process restart when their source PDFs still exist."""
    for metadata_path in CHECKPOINT_DIR.glob("*.json"):
        try:
            job = json.loads(metadata_path.read_text(encoding="utf-8"))
            job_id = job.get("id")
            if not job_id or not Path(job.get("input_path", "")).is_file():
                continue

            page_folder = CHECKPOINT_DIR / job_id / "pages"
            cache: dict[int, Any] = {}
            if page_folder.is_dir():
                for page_file in page_folder.glob("*.json"):
                    try:
                        index = int(page_file.stem)
                        if 0 <= index < int(job["total_pages"]):
                            cache[index] = json.loads(
                                page_file.read_text(encoding="utf-8")
                            )
                    except (ValueError, OSError, json.JSONDecodeError):
                        continue

            job["page_cache"] = cache
            job["completed_pages"] = len(cache)
            total = max(1, int(job["total_pages"]))
            job["progress"] = round(len(cache) / total * 100)

            if job.get("status") in ("queued", "processing"):
                job.update(
                    status="failed",
                    error="Backend restarted before the job finished.",
                    message="Checkpoint restored. Retry to resume unfinished pages.",
                    updated_at=now(),
                )

            if job.get("status") == "completed":
                if not Path(job.get("output_path", "")).is_file():
                    job.update(
                        status="failed",
                        error="Completed output is missing.",
                        message="Checkpoint restored. Retry to rebuild the PDF.",
                        updated_at=now(),
                    )

            JOBS[job_id] = job
            save_job_checkpoint(job)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue


restore_checkpoint_jobs()


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
    save_job_checkpoint(job)
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
    save_job_checkpoint(job)
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
    if not blocks:
        return []

    config = PROVIDERS[provider]
    api_key = os.getenv(config["key_env"], "")
    model = os.getenv(config["model_env"], config["default_model"])

    if not api_key:
        raise RuntimeError(f"{config['key_env']} is not configured.")

    system_prompt = (
        "You are a professional English-to-Myanmar translator. "
        "Translate every input item into natural Myanmar Unicode. "
        "Preserve names, numbers, URLs, code and citations. "
        "Never omit, merge, split, or reorder items. "
        "Return only valid JSON containing one translated string per input."
    )

    def parse_answer(answer: str, expected: int) -> list[str]:
        answer = answer.strip()
        if answer.startswith("```"):
            lines = answer.splitlines()
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines.pop()
            answer = "\\n".join(lines).strip()

        parsed = json.loads(answer)

        if isinstance(parsed, dict):
            parsed = parsed.get("translations")

        if expected == 1 and isinstance(parsed, str):
            parsed = [parsed]

        if (
            not isinstance(parsed, list)
            or len(parsed) != expected
            or not all(isinstance(item, str) for item in parsed)
        ):
            raise ValueError(
                f"Expected {expected} translated strings; response count did not match."
            )

        return [item.strip() for item in parsed]

    class RateLimitError(RuntimeError):
        pass

    async def request_batch(
        client: httpx.AsyncClient,
        texts: list[str],
    ) -> list[str]:
        prompt = (
            f"Translate exactly {len(texts)} items into Myanmar. "
            "Return a JSON array with exactly one string per input, in order.\n"
            + json.dumps(texts, ensure_ascii=False)
        )

        last_error = None

        for attempt in range(3):
            try:
                if provider == "gemini":
                    url = (
                        "https://generativelanguage.googleapis.com/"
                        f"v1beta/models/{model}:generateContent"
                    )
                    response = await client.post(
                        url,
                        params={"key": api_key},
                        json={
                            "systemInstruction": {
                                "parts": [{"text": system_prompt}]
                            },
                            "contents": [
                                {"role": "user", "parts": [{"text": prompt}]}
                            ],
                            "generationConfig": {
                                "temperature": 0.1,
                                "responseMimeType": "application/json",
                            },
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise ValueError("Gemini returned no candidates.")
                    parts = candidates[0].get("content", {}).get("parts", [])
                    answer = "".join(p.get("text", "") for p in parts)
                else:
                    response = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": system_prompt
                                    + " Return a JSON object with key translations containing the array.",
                                },
                                {"role": "user", "content": prompt},
                            ],
                            "temperature": 0.1,
                            "response_format": {"type": "json_object"},
                        },
                    )
                    response.raise_for_status()
                    answer = response.json()["choices"][0]["message"]["content"]

                return parse_answer(answer, len(texts))

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    delay = None

                    retry_after = exc.response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(1.0, float(retry_after))
                        except ValueError:
                            pass

                    if delay is None:
                        try:
                            error_data = exc.response.json().get("error", {})
                            for detail in error_data.get("details", []):
                                retry_delay = detail.get("retryDelay", "")
                                if retry_delay.endswith("s"):
                                    delay = max(
                                        1.0, float(retry_delay[:-1])
                                    )
                                    break
                        except (ValueError, TypeError, AttributeError):
                            pass

                    if delay is None:
                        delay = min(60.0, 10.0 * (2 ** attempt))

                    if attempt < 2:
                        print(
                            f"Gemini rate limit (429). "
                            f"Waiting {delay:.0f} seconds before retry "
                            f"{attempt + 2}/3.",
                            flush=True,
                        )
                        await asyncio.sleep(delay)
                        continue

                    raise RateLimitError(
                        "Gemini API returned 429 after retries. "
                        "Check project quota and rate limits, then retry later."
                    ) from exc

                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))

            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))

        raise RuntimeError(str(last_error))

    translated: list[str] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(90.0, connect=20.0)
    ) as client:
        batch_size = 5

        for offset in range(0, len(blocks), batch_size):
            texts = [
                block["text"]
                for block in blocks[offset:offset + batch_size]
            ]

            try:
                translated.extend(await request_batch(client, texts))
            except RateLimitError:
                raise
            except Exception:
                for index, text in enumerate(texts, start=offset + 1):
                    try:
                        translated.extend(await request_batch(client, [text]))
                    except Exception as exc:
                        raise RuntimeError(
                            f"Translation failed at text block {index}: {exc}"
                        ) from exc

    if len(translated) != len(blocks):
        raise RuntimeError("Internal translation count mismatch.")

    return translated


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
    job.update(
        status="processing",
        error=None,
        message="Restoring checkpoint and preparing translation…",
        updated_at=now(),
    )
    save_job_checkpoint(job)

    source = Path(job["input_path"])
    output = Path(job["output_path"])
    temp_output = OUTPUT_DIR / f"{job_id}-working.pdf"

    try:
        if not source.is_file():
            raise RuntimeError(
                "Original PDF is missing. Upload the PDF again; "
                "the saved checkpoint alone cannot rebuild the document."
            )
        if not FONT_PATH.exists():
            raise RuntimeError(
                "Myanmar font is missing. Add NotoSansMyanmar-Regular.ttf "
                "to backend/fonts/ or configure MYANMAR_FONT_PATH."
            )

        # Load any saved page translations before making API requests.
        page_folder = CHECKPOINT_DIR / job_id / "pages"
        if page_folder.is_dir():
            for page_file in page_folder.glob("*.json"):
                try:
                    index = int(page_file.stem)
                    if 0 <= index < int(job["total_pages"]):
                        if index not in job["page_cache"]:
                            job["page_cache"][index] = json.loads(
                                page_file.read_text(encoding="utf-8")
                            )
                except (ValueError, OSError, json.JSONDecodeError):
                    continue

        with fitz.open(source) as doc:
            total = len(doc)
            job["total_pages"] = total

            for index, page in enumerate(doc):
                if index in job["page_cache"]:
                    continue

                blocks = page_text_blocks(page)
                if not blocks:
                    job["page_cache"][index] = []
                    save_page_checkpoint(job, index)
                    job["completed_pages"] = len(job["page_cache"])
                    job["progress"] = round(
                        job["completed_pages"] / max(1, total) * 100
                    )
                    job["message"] = (
                        f"Page {index + 1}/{total}: no selectable text; "
                        "keeping page unchanged."
                    )
                    job["updated_at"] = now()
                    save_job_checkpoint(job)
                    continue

                job["message"] = f"Translating page {index + 1} of {total}…"
                job["updated_at"] = now()
                save_job_checkpoint(job)

                translations = await translate_page(job["provider"], blocks)

                job["page_cache"][index] = [
                    {"bbox": b["bbox"], "translation": t}
                    for b, t in zip(blocks, translations)
                ]

                # Persist the page before moving to the next one.
                save_page_checkpoint(job, index)
                job["completed_pages"] = len(job["page_cache"])
                job["progress"] = round(
                    job["completed_pages"] / max(1, total) * 100
                )
                job["updated_at"] = now()
                save_job_checkpoint(job)

        # Rebuild the final PDF from the original plus all saved translations.
        with fitz.open(source) as doc:
            for index, page in enumerate(doc):
                translated_blocks = job["page_cache"].get(index, [])
                if not translated_blocks:
                    continue

                for item in translated_blocks:
                    page.add_redact_annot(
                        fitz.Rect(item["bbox"]),
                        fill=(1, 1, 1),
                        cross_out=False,
                    )
                page.apply_redactions(images=0, graphics=0, text=0)

                for item in translated_blocks:
                    rect = fitz.Rect(item["bbox"])
                    rect.x0 += 1.5
                    rect.x1 -= 1.5
                    rect.y0 += 0.5
                    rect.y1 -= 0.5
                    text = item["translation"].strip()
                    fontsize = 10.5
                    result = page.insert_textbox(
                        rect,
                        text,
                        fontname="myanmar",
                        fontfile=str(FONT_PATH),
                        fontsize=fontsize,
                        color=(0.08, 0.08, 0.08),
                        align=0,
                        lineheight=1.15,
                    )
                    while result < 0 and fontsize > 5.5:
                        fontsize -= 0.5
                        result = page.insert_textbox(
                            rect,
                            text,
                            fontname="myanmar",
                            fontfile=str(FONT_PATH),
                            fontsize=fontsize,
                            color=(0.08, 0.08, 0.08),
                            align=0,
                            lineheight=1.1,
                        )
                    if result < 0:
                        fallback = fitz.Rect(
                            rect.x0,
                            rect.y0,
                            min(page.rect.x1 - 8, rect.x1 + 12),
                            min(page.rect.y1 - 8, rect.y1 + 28),
                        )
                        page.insert_textbox(
                            fallback,
                            text,
                            fontname="myanmar",
                            fontfile=str(FONT_PATH),
                            fontsize=5.5,
                            color=(0.08, 0.08, 0.08),
                        )

            doc.save(temp_output, garbage=4, deflate=True)

        shutil.move(str(temp_output), str(output))
        job.update(
            status="completed",
            progress=100,
            completed_pages=job["total_pages"],
            message="Translation complete. Your Myanmar PDF is ready.",
            error=None,
            updated_at=now(),
        )
        save_job_checkpoint(job)

    except Exception as exc:
        temp_output.unlink(missing_ok=True)
        job.update(
            status="failed",
            error=str(exc),
            message="Translation paused. Retry to continue from saved pages.",
            updated_at=now(),
        )
        save_job_checkpoint(job)

    finally:
        task = TASKS.get(job_id)
        if task and task.done():
            TASKS.pop(job_id, None)


@app.on_event("shutdown")
async def cleanup_tasks():
    for task in TASKS.values():
        if not task.done():
            task.cancel()
