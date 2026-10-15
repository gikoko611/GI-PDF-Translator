# G.I PDF Translator — Golden Edition

Golden black-and-metallic-gold React web app + FastAPI API for translating selectable English PDF text into Myanmar Unicode with Gemini or Groq. OCR is intentionally disabled.

## Important limitations

- The starter replaces extracted text blocks inside their original bounding boxes. It attempts to preserve page dimensions, images, vector graphics and general layout, but exact visual fidelity is not guaranteed; long Myanmar translations can overflow or need smaller type.
- Add a Unicode Myanmar TTF font at `backend/fonts/NotoSansMyanmar-Regular.ttf`. Download Noto Sans Myanmar from the official Google Fonts repository and check its license. Because `backend/fonts/*.ttf` is ignored by Git, if its license permits redistribution, force-add the font so the deployed backend receives it: `git add -f backend/fonts/NotoSansMyanmar-Regular.ttf`. If you do not include a font in the deployed service, PDF export will fail rather than silently create unreadable Burmese text.
- Job state and translated page cache are in process memory. A backend restart loses in-progress job state; completed files on ephemeral hosting may also disappear. Use a persistent disk plus a database/queue before production or multi-instance deployment.
- Configure API keys only as backend environment variables. Never add keys to `VITE_*` variables or commit `.env`.
- Free-tier quotas and model availability depend on the provider and can change.

## Local development in Termux

Prerequisites: Node.js, npm, Python, Git. On Termux:

```bash
pkg update
pkg install git nodejs-lts python clang make rust -y
mkdir -p ~/projects
cd ~/projects
# Put this project folder here, or clone your GitHub repo.
```

### 1. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add `GEMINI_API_KEY` and/or `GROQ_API_KEY`. Add the Myanmar font file to `backend/fonts/NotoSansMyanmar-Regular.ttf`. Then:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. Frontend (second Termux session)

```bash
cd frontend
npm install
cp .env.example .env
npm run dev -- --host 0.0.0.0
```

Open the Vite URL shown in Termux. On the same phone, `http://127.0.0.1:5173` should reach the frontend and `http://127.0.0.1:8000/api/health` the backend.

### 3. GitHub repository

Create an empty repository on GitHub first, then from the project root:

```bash
git init
git add .
git commit -m "feat: create G.I PDF Translator Golden Edition"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

Do not commit `.env`, API keys, uploaded PDFs, output PDFs or font files.

## Deploy

### Backend on Render

1. Push the repository to GitHub.
2. In Render, create a new Blueprint from the repository and use `render.yaml`, or create a Python web service with root directory `backend`.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Set secret environment variables `GEMINI_API_KEY` and/or `GROQ_API_KEY` in Render. Set `CORS_ORIGINS` to the exact deployed frontend origin, without a trailing slash. Add the font file to the deployed service or configure `MYANMAR_FONT_PATH` to its location.
6. Verify `https://YOUR-BACKEND.onrender.com/api/health`.

### Frontend

Deploy `frontend` as a static site (for example on Render or Netlify). Build command: `npm install && npm run build`; publish directory: `dist`. Set `VITE_API_BASE_URL` to the backend origin, e.g. `https://YOUR-BACKEND.onrender.com`. Rebuild after changing this value. Set the backend `CORS_ORIGINS` to the exact frontend URL.

## API endpoints

- `GET /api/health` — provider/font configuration status
- `POST /api/jobs` — multipart fields `file` and `provider` (`gemini` or `groq`)
- `GET /api/jobs/{id}` — live progress
- `POST /api/jobs/{id}/retry` — resume pages cached in the running process
- `GET /api/jobs/{id}/download` — translated PDF when complete

## Security checklist before public launch

Add per-user authentication, request limits, abuse protection, persistent job storage, cleanup/retention policy, a persistent task queue, and a proper server-side secret manager. The current starter is a single-instance MVP, not a hardened multi-user production service.
