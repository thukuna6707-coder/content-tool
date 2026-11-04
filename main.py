from __future__ import annotations
import os, re, uuid, time, subprocess, threading
from pathlib import Path
from typing import Optional
import requests
from fastapi import FastAPI, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).parent
FISH_DEFAULT_VOICE_ID = "bf322df2096a46f18c579d0baa36f41d"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
app = FastAPI(title="Content Tool", version="1.0.0")
jobs: dict[str, dict] = {}
lock = threading.Lock()

class RenderRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=180)
    script: str = Field(min_length=20, max_length=12000)
    keywords: str = Field(default="", max_length=1000)
    stock_provider: str = "both"
    pexels_key: str = ""
    pixabay_key: str = ""
    voice_provider: str = "elevenlabs"
    voice_key: str = ""
    voice_id: str = FISH_DEFAULT_VOICE_ID
    aspect: str = "9:16"
    quality: str = "1080p"
    subtitle_size: int = Field(default=22, ge=12, le=48)
    subtitle_color: str = "#ffffff"
    subtitle_background: bool = True
    music_volume: float = Field(default=0.12, ge=0, le=1)

def run(cmd, timeout=600):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout.strip()

def ffprobe_duration(path: Path) -> float:
    out = run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)])
    return max(0.1, float(out))

def split_sentences(script: str) -> list[str]:
    parts = re.split(r'(?<=[.!?])\s+|\n+', script.strip())
    return [p.strip() for p in parts if p.strip()][:80]

def tokens(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z0-9]{3,}", text)}

def score_clip(clip: dict, query: str) -> float:
    words = tokens(query)
    tags = tokens(" ".join([clip.get("description",""), clip.get("url","")]))
    return len(words & tags) / max(1, len(words))

def pexels_search(key: str, query: str, per_page=12) -> list[dict]:
    if not key: return []
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization":key}, params={"query":query,"per_page":per_page,"orientation":"portrait"}, timeout=25)
    r.raise_for_status()
    out=[]
    for v in r.json().get("videos",[]):
        files=sorted(v.get("video_files",[]), key=lambda x:(x.get("width",0)*x.get("height",0), x.get("quality")=="hd"), reverse=True)
        f=next((x for x in files if x.get("file_type")=="video/mp4" and x.get("link")), None)
        if f: out.append({"url":f["link"],"description":v.get("url",""),"source":"Pexels"})
    return out

def pixabay_search(key: str, query: str, per_page=12) -> list[dict]:
    if not key: return []
    r=requests.get("https://pixabay.com/api/videos/",params={"key":key,"q":query,"per_page":per_page,"safesearch":"true"},timeout=25)
    r.raise_for_status()
    out=[]
    for v in r.json().get("hits",[]):
        videos=v.get("videos",{}); f=videos.get("large") or videos.get("medium") or videos.get("small")
        if f and f.get("url"): out.append({"url":f["url"],"description":v.get("tags",""),"source":"Pixabay"})
    return out

def search_clips(req: RenderRequest, sentence: str) -> list[dict]:
    q = " ".join([req.topic, sentence, req.keywords]).strip()[:180]
    clips=[]
    providers = [req.stock_provider] if req.stock_provider in ("pexels","pixabay") else ["pexels","pixabay"]
    for provider in providers:
        try:
            if provider=="pexels": clips += pexels_search(req.pexels_key or os.getenv("PEXELS_API_KEY",""), q)
            else: clips += pixabay_search(req.pixabay_key or os.getenv("PIXABAY_API_KEY",""), q)
        except Exception as e: print(f"{provider} search warning: {e}")
    return sorted(clips,key=lambda c:score_clip(c, sentence+" "+req.topic),reverse=True)

def generate_voice(req: RenderRequest, script: str, out: Path):
    if not req.voice_key: raise RuntimeError("Add the selected voice provider API key in Settings.")
    if req.voice_provider=="fish":
        r=requests.post("https://api.fish.audio/v1/tts",headers={"Authorization":f"Bearer {req.voice_key}","Content-Type":"application/json","model":"s2.1-pro-free"},json={"text":script,"reference_id":req.voice_id or FISH_DEFAULT_VOICE_ID,"format":"mp3","mp3_bitrate":128},timeout=240)
    else:
        voice=req.voice_id or "21m00Tcm4TlvDq8ikWAM"
        r=requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",headers={"xi-api-key":req.voice_key,"Content-Type":"application/json","Accept":"audio/mpeg"},json={"text":script,"model_id":"eleven_multilingual_v2","voice_settings":{"stability":0.48,"similarity_boost":0.78,"style":0.25,"use_speaker_boost":True}},timeout=240)
    if not r.ok: raise RuntimeError(f"Voice API error ({r.status_code}): {r.text[:700]}")
    out.write_bytes(r.content)

def srt_time(seconds: float) -> str:
    ms=int(seconds*1000); h=ms//3600000; ms%=3600000; m=ms//60000; ms%=60000; s=ms//1000; ms%=1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def write_srt(sentences: list[str], duration: float, path: Path):
    weights=[max(1,len(re.findall(r"\w+",s))) for s in sentences]
    total=sum(weights) or 1; cursor=0; blocks=[]
    for i,(sentence,w) in enumerate(zip(sentences,weights),1):
        start=cursor; cursor += duration*w/total
        blocks.append(f"{i}\n{srt_time(start)} --> {srt_time(cursor)}\n{sentence}\n")
    path.write_text("\n".join(blocks),encoding="utf-8")

def download_clip(url: str, path: Path):
    with requests.get(url,stream=True,timeout=90) as r:
        r.raise_for_status()
        with path.open("wb") as f:
            for chunk in r.iter_content(1024*1024):
                if chunk: f.write(chunk)
    if path.stat().st_size < 12000: raise RuntimeError("Downloaded stock clip is unexpectedly small.")

def render_job(job_id: str, req: RenderRequest, music_path: Optional[Path]):
    folder=JOBS_DIR/job_id; folder.mkdir(parents=True,exist_ok=True)
    def update(**kw):
        with lock: jobs[job_id].update(kw)
    try:
        update(status="processing",stage="Generating narration",progress=5)
        voice=folder/"voice.mp3"; generate_voice(req,req.script,voice)
        duration=ffprobe_duration(voice); sentences=split_sentences(req.script)
        if not sentences: raise RuntimeError("The script has no readable sentences.")
        update(stage="Searching stock footage",progress=14)
        selected=[]; seen=set()
        for i,sentence in enumerate(sentences):
            clips=search_clips(req,sentence)
            best=next((c for c in clips if c["url"] not in seen),None)
            if best: seen.add(best["url"]); selected.append((sentence,best))
            update(progress=14+int(34*(i+1)/len(sentences)),stage=f"Matching scene {i+1}/{len(sentences)}")
        if not selected: raise RuntimeError("No stock footage found. Check your API keys and try broader keywords.")
        weights=[max(1,len(re.findall(r"\w+",s))) for s,_ in selected]; total=sum(weights)
        target=1080 if req.quality=="1080p" else 720
        if req.aspect=="16:9": width,height=target,int(target*9/16)
        elif req.aspect=="1:1": width=height=target
        else: width,height=int(target*9/16),target
        segments=[]; update(stage="Downloading and assembling scenes",progress=50)
        for i,(sentence,clip) in enumerate(selected):
            raw=folder/f"raw_{i}.mp4"; seg=folder/f"scene_{i}.mp4"; download_clip(clip["url"],raw)
            seg_duration=max(1.0,duration*weights[i]/total)
            run(["ffmpeg","-y","-stream_loop","-1","-i",str(raw),"-t",f"{seg_duration:.3f}","-vf",f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30","-an","-c:v","libx264","-preset","veryfast","-crf","23","-pix_fmt","yuv420p",str(seg)],timeout=300)
            segments.append(seg); raw.unlink(missing_ok=True)
            update(progress=50+int(25*(i+1)/len(selected)),stage=f"Preparing scene {i+1}/{len(selected)}")
        concat=folder/"concat.txt"; concat.write_text("\n".join("file '"+p.name+"'" for p in segments),encoding="utf-8")
        silent=folder/"silent.mp4"; run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),"-c","copy",str(silent)],timeout=300)
        final=folder/"content-tool.mp4"; srt=folder/"subtitles.srt"; write_srt([s for s,_ in selected],duration,srt)
        update(stage="Burning subtitles and mixing audio",progress=80)
        color=req.subtitle_color.lstrip("#")
        back="BorderStyle=3,BackColour=&H90000000,Outline=0" if req.subtitle_background else "BorderStyle=1,Outline=2"
        subtitle_filter=f"subtitles={srt.as_posix()}:force_style='FontName=DejaVu Sans,FontSize={req.subtitle_size},Bold=1,PrimaryColour=&H00{color[4:6]}{color[2:4]}{color[0:2]},{back},Shadow=1,Alignment=2,MarginV=70'"
        cmd=["ffmpeg","-y","-i",str(silent),"-i",str(voice)]
        has_music=bool(music_path and music_path.exists())
        if has_music: cmd += ["-i",str(music_path)]
        cmd += ["-vf",subtitle_filter,"-map","0:v:0","-map","1:a:0"]
        if has_music: cmd += ["-filter_complex",f"[2:a]volume={req.music_volume}[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]","-map","[a]"]
        cmd += ["-c:v","libx264","-preset","veryfast","-crf","22","-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart",str(final)]
        run(cmd,timeout=900)
        update(status="completed",stage="Ready to download",progress=100,download_url=f"/api/jobs/{job_id}/download",scenes=[{"sentence":s,"source":c["source"],"duration":round(duration*w/total,1)} for (s,c),w in zip(selected,weights)])
    except Exception as e: update(status="failed",stage="Render failed",error=str(e)[:1600],progress=100)

@app.get("/api/health")
def health(): return {"ok":True,"service":"Content Tool"}

@app.post("/api/fish/voices")
async def list_fish_voices(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Send a valid JSON body with api_key.")
    api_key = str(payload.get("api_key", "")).strip()
    if not api_key:
        raise HTTPException(400, "Add your Fish Audio API key in API settings first.")
    headers = {"Authorization": f"Bearer {api_key}"}
    voices = {}
    for owned in (False, True):
        try:
            response = requests.get("https://api.fish.audio/model", headers=headers,
                params={"page_size": 100, "page_number": 1, "sort_by": "score", "self": str(owned).lower()}, timeout=30)
            response.raise_for_status()
            data = response.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            for item in items:
                if not isinstance(item, dict) or item.get("type", "tts") != "tts":
                    continue
                if item.get("state") not in (None, "trained"):
                    continue
                model_id = item.get("_id") or item.get("id")
                if not model_id:
                    continue
                author = item.get("author") or {}
                if not isinstance(author, dict):
                    author = {}
                voices[str(model_id)] = {"id": str(model_id), "title": str(item.get("title") or "Untitled voice"),
                    "author": str(author.get("nickname") or author.get("username") or ""), "owned": owned}
        except requests.RequestException as exc:
            if not voices:
                status = getattr(getattr(exc, "response", None), "status_code", 502)
                raise HTTPException(status, "Could not load Fish Audio voices. Check your API key and try again.")
    return {"voices": list(voices.values()), "default_voice_id": FISH_DEFAULT_VOICE_ID}

@app.post("/api/jobs")
async def create_job(request: Request):
    music_path=None
    if "multipart/form-data" in request.headers.get("content-type",""):
        form=await request.form()
        try: req=RenderRequest.model_validate_json(form.get("payload","{}"))
        except Exception as e: raise HTTPException(422,str(e))
        music=form.get("music")
    else:
        try: req=RenderRequest.model_validate(await request.json())
        except Exception as e: raise HTTPException(422,str(e))
        music=None
    if not req.pexels_key and not req.pixabay_key and not os.getenv("PEXELS_API_KEY") and not os.getenv("PIXABAY_API_KEY"):
        raise HTTPException(400,"Add at least one stock footage API key.")
    if music and isinstance(music,UploadFile):
        if music.content_type not in ("audio/mpeg","audio/mp3","audio/wav","audio/x-wav","audio/mp4","audio/aac","audio/ogg"):
            raise HTTPException(400,"Unsupported music file type.")
        ext=Path(music.filename or "music.mp3").suffix or ".mp3"
        folder=JOBS_DIR/uuid.uuid4().hex[:12]; folder.mkdir(parents=True,exist_ok=True)
        music_path=folder/f"background{ext}"; music_path.write_bytes(await music.read())
    job_id=uuid.uuid4().hex[:12]
    with lock: jobs[job_id]={"id":job_id,"status":"queued","stage":"Waiting to start","progress":0,"created":time.time()}
    threading.Thread(target=render_job,args=(job_id,req,music_path),daemon=True).start()
    return {"id":job_id,"status":"queued"}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with lock: job=jobs.get(job_id)
    if not job: raise HTTPException(404,"Job not found. Jobs are kept in memory and may reset after redeploy.")
    return job

@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    with lock: job=jobs.get(job_id)
    if not job or job.get("status")!="completed": raise HTTPException(404,"Video is not ready.")
    path=JOBS_DIR/job_id/"content-tool.mp4"
    if not path.exists(): raise HTTPException(404,"Render file is no longer available.")
    return FileResponse(path,media_type="video/mp4",filename=f"content-tool-{job_id}.mp4")

app.mount("/",StaticFiles(directory=APP_DIR/"static",html=True),name="static")
