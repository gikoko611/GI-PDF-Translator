import { useEffect, useRef, useState } from 'react'
import { Activity, ArrowDownToLine, Check, CheckCircle2, ChevronDown, CircleAlert, FileText, Languages, LoaderCircle, LockKeyhole, ShieldCheck, Sparkles, UploadCloud, X } from 'lucide-react'

const API = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')
type Job = { id: string; filename: string; provider: string; status: string; progress: number; total_pages: number; completed_pages: number; message: string; error: string | null }
type Health = { ok: boolean; providers: { gemini: boolean; groq: boolean }; myanmar_font_ready: boolean; max_upload_mb: number }

export default function App() {
  const [provider, setProvider] = useState<'gemini' | 'groq'>('gemini')
  const [file, setFile] = useState<File | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  async function loadHealth() {
    try { const r = await fetch(`${API}/api/health`); if (r.ok) setHealth(await r.json()) } catch { /* backend may not be started yet */ }
  }
  useEffect(() => { void loadHealth() }, [])

  useEffect(() => {
    if (!job || !['queued', 'processing'].includes(job.status)) return
    let cancelled = false
    const poll = async () => {
      try {
        const r = await fetch(`${API}/api/jobs/${job.id}`)
        const data = await r.json()
        if (!r.ok) throw new Error(data.detail || 'Could not read job status.')
        if (!cancelled) setJob(data)
      } catch (e) { if (!cancelled) setError(e instanceof Error ? e.message : 'Connection error.') }
    }
    const timer = window.setInterval(() => { void poll() }, 1800)
    void poll()
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [job?.id, job?.status])

  async function upload() {
    if (!file) { setError('Please choose a PDF file first.'); return }
    setBusy(true); setError(''); setJob(null)
    try {
      const form = new FormData(); form.append('file', file); form.append('provider', provider)
      const r = await fetch(`${API}/api/jobs`, { method: 'POST', body: form })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || 'Upload failed.')
      setJob(data)
    } catch (e) { setError(e instanceof Error ? e.message : 'Upload failed. Check backend URL and connection.') }
    finally { setBusy(false) }
  }

  async function retry() {
    if (!job) return
    setError(''); setBusy(true)
    try {
      const r = await fetch(`${API}/api/jobs/${job.id}/retry`, { method: 'POST' }); const data = await r.json()
      if (!r.ok) throw new Error(data.detail || 'Retry failed.')
      setJob(data)
    } catch (e) { setError(e instanceof Error ? e.message : 'Retry failed.') }
    finally { setBusy(false) }
  }

  async function download() {
    if (!job) return
    setError('')
    try {
      const r = await fetch(`${API}/api/jobs/${job.id}/download`)
      if (!r.ok) { const data = await r.json(); throw new Error(data.detail || 'Download failed.') }
      const blob = await r.blob(); const url = URL.createObjectURL(blob); const a = document.createElement('a')
      a.href = url; a.download = `${file?.name.replace(/\.pdf$/i, '') || 'translated'}-myanmar.pdf`; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url)
    } catch (e) { setError(e instanceof Error ? e.message : 'Download failed.') }
  }

  const working = job ? ['queued', 'processing'].includes(job.status) : false
  const providerReady = health?.providers?.[provider]
  const fileSize = file ? `${(file.size / (1024 * 1024)).toFixed(2)} MB` : ''

  return <main className="min-h-screen px-4 pb-12 pt-5 sm:px-6">
    <div className="mx-auto max-w-5xl">
      <header className="flex items-center justify-between gap-3 border-b border-white/10 pb-5">
        <div className="flex items-center gap-3">
          <div className="brand-mark"><span>G.I</span></div>
          <div><p className="text-[11px] font-semibold uppercase tracking-[.32em] text-gold">Golden Edition</p><h1 className="text-base font-semibold tracking-wide sm:text-lg">PDF Translator</h1></div>
        </div>
        <div className="secure-pill"><ShieldCheck size={14}/><span>Private by design</span></div>
      </header>

      <section className="hero py-10 text-center sm:py-14">
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl border border-gold/30 bg-gold/10 text-gold shadow-gold"><Languages size={23}/></div>
        <p className="mb-3 text-xs font-semibold uppercase tracking-[.35em] text-gold">English to Myanmar</p>
        <h2 className="mx-auto max-w-2xl text-3xl font-semibold leading-tight sm:text-5xl">Every page, in your <span className="gold-text">language.</span></h2>
        <p className="mx-auto mt-4 max-w-xl text-sm leading-6 text-zinc-400 sm:text-base">Translate text-based PDFs into Myanmar with AI. Keep the original page structure and download a new PDF.</p>
      </section>

      <div className="grid gap-5 lg:grid-cols-[1.1fr_.9fr]">
        <section className="panel p-5 sm:p-7">
          <div className="mb-5 flex items-center justify-between"><div><p className="section-kicker">01 / SOURCE FILE</p><h3 className="mt-1 text-lg font-semibold">Upload your PDF</h3></div><FileText className="text-gold" size={22}/></div>
          <button className={`dropzone w-full ${file ? 'dropzone-selected' : ''}`} onClick={() => inputRef.current?.click()} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); const f = e.dataTransfer.files?.[0]; if (f) { setFile(f); setError('') } }}>
            <input ref={inputRef} className="hidden" type="file" accept="application/pdf,.pdf" onChange={e => { const f = e.target.files?.[0]; if (f) { setFile(f); setError('') } }}/>
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-gold/10 text-gold">{file ? <Check size={22}/> : <UploadCloud size={24}/>}</div>
            {file ? <><p className="break-all text-sm font-medium text-white">{file.name}</p><p className="mt-1 text-xs text-zinc-500">{fileSize} · Tap to change file</p></> : <><p className="text-sm font-medium">Choose a PDF or drop it here</p><p className="mt-2 text-xs text-zinc-500">Text-based PDF · Max {health?.max_upload_mb || 15} MB</p></>}
          </button>
          <div className="mt-6"><label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-zinc-400">Translation engine</label><div className="grid grid-cols-2 gap-3">
            <button onClick={() => setProvider('gemini')} className={`provider-option ${provider === 'gemini' ? 'provider-active' : ''}`}><Sparkles size={17}/><span>Gemini</span><span className="provider-state">{health ? (health.providers.gemini ? 'Ready' : 'Key missing') : 'Checking'}</span></button>
            <button onClick={() => setProvider('groq')} className={`provider-option ${provider === 'groq' ? 'provider-active' : ''}`}><Activity size={17}/><span>Groq</span><span className="provider-state">{health ? (health.providers.groq ? 'Ready' : 'Key missing') : 'Checking'}</span></button>
          </div><p className="mt-2 text-[11px] leading-5 text-zinc-500">API keys are configured on the backend, never embedded in this web app.</p></div>
          <button disabled={!file || busy || working} onClick={() => void upload()} className="gold-button mt-6 w-full">{busy ? <LoaderCircle className="animate-spin" size={17}/> : <Languages size={17}/>}<span>{working ? 'Translation in progress' : 'Translate PDF'}</span></button>
          {providerReady === false && <p className="mt-3 flex gap-2 text-xs text-amber-300"><CircleAlert size={15} className="shrink-0"/>This provider's API key is not configured on the backend yet.</p>}
        </section>

        <section className="panel flex min-h-[390px] flex-col p-5 sm:p-7">
          <div className="mb-6 flex items-center justify-between"><div><p className="section-kicker">02 / TRANSLATION</p><h3 className="mt-1 text-lg font-semibold">Your progress</h3></div><div className="live-pill"><span className={working ? 'pulse-dot' : 'static-dot'}></span>{working ? 'LIVE' : 'STATUS'}</div></div>
          {!job ? <div className="flex flex-1 flex-col items-center justify-center py-10 text-center"><div className="empty-orbit"><Languages size={27}/></div><p className="mt-5 text-sm font-medium text-zinc-300">Ready when you are</p><p className="mt-2 max-w-xs text-xs leading-5 text-zinc-500">Upload a PDF to see translation progress, page count and job status here.</p></div> : <>
            <div className="rounded-xl border border-white/8 bg-black/20 p-4"><div className="flex items-start gap-3"><div className="rounded-lg bg-gold/10 p-2 text-gold"><FileText size={20}/></div><div className="min-w-0 flex-1"><p className="break-all text-sm font-medium">{job.filename}</p><p className="mt-1 text-xs text-zinc-500">{job.provider.toUpperCase()} · {job.total_pages} pages</p></div><span className={`status-tag ${job.status}`}>{job.status}</span></div></div>
            <div className="mt-7 flex items-end justify-between"><div><p className="text-xs text-zinc-500">Overall completion</p><p className="mt-1 text-4xl font-semibold tracking-tight gold-text">{job.progress}%</p></div><p className="pb-1 text-xs text-zinc-400">{job.completed_pages} / {job.total_pages} pages</p></div>
            <div className="progress-track mt-4"><div className="progress-fill" style={{ width: `${job.progress}%` }}/></div>
            <p className="mt-4 min-h-10 text-xs leading-5 text-zinc-400">{job.message}</p>
            {job.error && <div className="mt-3 rounded-lg border border-red-500/20 bg-red-500/5 p-3 text-xs leading-5 text-red-200"><CircleAlert size={15} className="mr-2 inline"/>{job.error}</div>}
            <div className="mt-auto flex gap-3 pt-6">{job.status === 'completed' ? <button onClick={() => void download()} className="gold-button w-full"><ArrowDownToLine size={17}/>Download Myanmar PDF</button> : job.status === 'failed' ? <button disabled={busy} onClick={() => void retry()} className="gold-button w-full"><Activity size={17}/>Retry unfinished pages</button> : <div className="subtle-button w-full"><LoaderCircle size={16} className="animate-spin"/>Working on your document…</div>}</div>
            <button className="mt-3 flex items-center justify-center gap-1 self-center text-xs text-zinc-500 transition hover:text-gold" onClick={() => { setJob(null); setFile(null); setError('') }}><X size={13}/> Clear current job</button>
          </>}
        </section>
      </div>

      {error && <div className="mt-5 flex items-start gap-2 rounded-xl border border-red-500/20 bg-red-500/5 p-4 text-sm text-red-200"><CircleAlert size={18} className="mt-0.5 shrink-0"/><span className="flex-1">{error}</span><button onClick={() => setError('')}><X size={16}/></button></div>}

      <section className="mt-6 grid gap-3 sm:grid-cols-3"><div className="feature-card"><LockKeyhole size={18}/><div><p>API keys stay server-side</p><span>Not exposed in frontend code</span></div></div><div className="feature-card"><ShieldCheck size={18}/><div><p>Text-only PDF processing</p><span>OCR is intentionally disabled</span></div></div><div className="feature-card"><CheckCircle2 size={18}/><div><p>Retry-friendly workflow</p><span>Completed pages cached during job</span></div></div></section>
      <footer className="mt-10 border-t border-white/8 pt-5 text-center text-[11px] leading-5 text-zinc-600">G.I PDF TRANSLATOR <span className="mx-2 text-gold/60">◆</span> GOLDEN EDITION<br/>Translation quality and exact PDF layout may vary by document.</footer>
    </div>
  </main>
}
