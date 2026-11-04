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
    voice_id: str = Field(default=FISH_DEFAULT_VOICE_ID, max_length=128)
    aspect: str = "9:16"
    quality: str = "1080p"
    subtitle_size: int = Field(default=48, ge=12, le=72)
    subtitle_color: str = "#ffffff"
    subtitle_highlight_color: str = "#8cf4ff"
    subtitle_outline_color: str = "#080808"
    subtitle_outline_width: int = Field(default=3, ge=0, le=8)
    subtitle_background: bool = True
    subtitle_background_color: str = "#101522"
    subtitle_position: str = "bottom"
    subtitle_animation: str = "fade"
    subtitle_mode: str = "sentence"
    subtitle_phrase_words: int = Field(default=3, ge=2, le=6)
    subtitle_font: str = "DejaVu Sans"
    subtitle_weight: str = "bold"
    subtitle_uppercase: bool = False
    subtitle_template_id: str = "clean-white-0"
    music_volume: float = Field(default=0.12, ge=0, le=1)

def run(cmd, timeout=600):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode:
        details = (p.stderr or p.stdout)[-2200:]
        if p.returncode < 0:
            signal_number = -p.returncode
            raise RuntimeError(f"FFmpeg was terminated by signal {signal_number}. This can happen when the renderer exceeds its memory limit.\n{details}")
        raise RuntimeError(details)
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
    metadata = " ".join(str(clip.get(k, "")) for k in ("title", "tags", "description", "url"))
    tags = tokens(metadata)
    return len(words & tags) / max(1, len(words))


def scene_search_queries(sentence: str, topic: str, keywords: str) -> list[str]:
    """Build short scene-first searches with topic context and useful fallbacks."""
    candidates = [
        stock_search_query(sentence, topic, keywords, max_chars=88),
        stock_search_query(sentence, max_chars=72),
        stock_search_query(topic, sentence, max_chars=72),
        stock_search_query(keywords, topic, max_chars=72),
    ]
    return list(dict.fromkeys(q for q in candidates if q))

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

def stock_search_query(*parts: str, max_chars: int = 88) -> str:
    """Build a short, useful stock-video query (Pixabay q must stay under 100 chars)."""
    stop = {"the", "and", "for", "with", "from", "that", "this", "then", "they", "them", "their", "into", "over", "under", "what", "how", "why", "when", "where", "who", "are", "was", "were", "has", "have", "had", "you", "your", "its", "are", "begins", "begin", "next", "finally", "actually", "called", "carefully", "several", "days"}
    words=[]; seen=set()
    for part in parts:
        for word in re.findall(r"[a-zA-Z0-9]+", part.lower()):
            if len(word) < 3 or word in stop or word in seen:
                continue
            candidate=" ".join(words+[word])
            if len(candidate) > max_chars:
                continue
            words.append(word); seen.add(word)
            if len(words) >= 10:
                return " ".join(words)
    return " ".join(words)


def search_clips(req: RenderRequest, sentence: str) -> list[dict]:
    # Search each narration beat first, then try focused/contextual fallbacks.
    queries = scene_search_queries(sentence, req.topic, req.keywords)
    clips = []
    providers = [req.stock_provider] if req.stock_provider in ("pexels", "pixabay") else ["pexels", "pixabay"]
    for provider in providers:
        key = (req.pexels_key or os.getenv("PEXELS_API_KEY", "")) if provider == "pexels" else (req.pixabay_key or os.getenv("PIXABAY_API_KEY", ""))
        if not key:
            continue
        try:
            for query in queries:
                found = pexels_search(key, query) if provider == "pexels" else pixabay_search(key, query)
                for clip in found:
                    clip["search_query"] = query
                clips.extend(found)
                if len(clips) >= 8:
                    break
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            print(f"{provider} search warning: HTTP {status}; scene query attempts={len(queries)}")
        except Exception as e:
            print(f"{provider} search warning: {type(e).__name__}")
    unique = {}
    for clip in clips:
        if clip.get("url"):
            unique.setdefault(clip["url"], clip)
    return sorted(unique.values(), key=lambda c: score_clip(c, sentence), reverse=True)

def generate_voice(req: RenderRequest, script: str, out: Path):
    if not req.voice_key: raise RuntimeError("Add the selected voice provider API key in Settings.")
    if req.voice_provider=="fish":
        voice_id = req.voice_id.strip() or FISH_DEFAULT_VOICE_ID
        r=requests.post("https://api.fish.audio/v1/tts",headers={"Authorization":f"Bearer {req.voice_key}","Content-Type":"application/json","model":"s2.1-pro-free"},json={"text":script,"reference_id":voice_id,"format":"mp3","mp3_bitrate":128},timeout=240)
    else:
        voice=req.voice_id or "21m00Tcm4TlvDq8ikWAM"
        r=requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",headers={"xi-api-key":req.voice_key,"Content-Type":"application/json","Accept":"audio/mpeg"},json={"text":script,"model_id":"eleven_multilingual_v2","voice_settings":{"stability":0.48,"similarity_boost":0.78,"style":0.25,"use_speaker_boost":True}},timeout=240)
    if not r.ok: raise RuntimeError(f"Voice API error ({r.status_code}): {r.text[:700]}")
    out.write_bytes(r.content)

def srt_time(seconds: float) -> str:
    ms=int(seconds*1000); h=ms//3600000; ms%=3600000; m=ms//60000; ms%=60000; s=ms//1000; ms%=1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def ass_time(seconds: float) -> str:
    cs=max(0,int(seconds*100)); h=cs//360000; cs%=360000; m=cs//6000; cs%=6000; sec=cs//100; cs%=100
    return f"{h}:{m:02}:{sec:02}.{cs:02}"

def ass_color(value: str) -> str:
    value=value if re.fullmatch(r"#[0-9a-fA-F]{6}",value or "") else "#ffffff"
    return "&H00"+value[5:7]+value[3:5]+value[1:3]

def write_ass(sentences: list[str], duration: float, path: Path, req: RenderRequest):
    weights=[max(1,len(re.findall(r"\w+",s))) for s in sentences]
    total=sum(weights) or 1
    fonts={"DejaVu Sans","DejaVu Sans Condensed","DejaVu Serif","DejaVu Sans Mono"}
    font=req.subtitle_font if req.subtitle_font in fonts else "DejaVu Sans"
    alignment={"bottom":2,"middle":5,"top":8}.get(req.subtitle_position,2)
    margin={"bottom":82,"middle":0,"top":70}.get(req.subtitle_position,82)
    bold=1 if req.subtitle_weight in ("bold","heavy") else 0
    outline=max(0,min(8,req.subtitle_outline_width))
    border=3 if req.subtitle_background else 1
    back=ass_color(req.subtitle_background_color).replace("&H00","&H55",1)
    outline_color=ass_color(req.subtitle_outline_color)
    primary=ass_color(req.subtitle_color)
    highlight=ass_color(req.subtitle_highlight_color)
    target=1080 if req.quality=="1080p" else 720
    if req.aspect=="16:9": play_w,play_h=target,int(target*9/16)
    elif req.aspect=="1:1": play_w=play_h=target
    else: play_w,play_h=int(target*9/16),target
    header=(f"[Script Info]\nScriptType: v4.00+\nPlayResX: {play_w}\nPlayResY: {play_h}\nScaledBorderAndShadow: yes\nWrapStyle: 2\n\n"
      "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
    style=f"Style: Default,{font},{req.subtitle_size},{primary},{highlight},{outline_color},{back},{bold},0,0,0,100,100,0,0,{border},{outline},1,{alignment},64,64,{margin},1\n\n"
    events="[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    rows=[]; cursor=0.0
    for sentence,w in zip(sentences,weights):
        scene_duration=duration*w/total
        if req.subtitle_uppercase: sentence=sentence.upper()
        if req.subtitle_mode=="word":
            units=[[word] for word in re.findall(r"\S+",sentence)]
        elif req.subtitle_mode=="phrase":
            words=re.findall(r"\S+",sentence); step=max(2,min(6,req.subtitle_phrase_words))
            units=[words[i:i+step] for i in range(0,len(words),step)]
        else:
            units=[sentence.split()]
        unit_weights=[max(1,sum(len(x) for x in unit)) for unit in units]
        unit_total=sum(unit_weights) or 1; local=cursor
        for unit,uw in zip(units,unit_weights):
            end=local+scene_duration*uw/unit_total
            text=" ".join(unit).replace("{","\\{").replace("}","\\}").replace("\\n"," ")
            tags="{\\c"+highlight+"}" if req.subtitle_mode=="word" else ""
            if req.subtitle_animation=="fade": tags+="{\\fad(100,100)}"
            elif req.subtitle_animation=="pop": tags+="{\\fscx108\\fscy108\\t(0,120,\\fscx100\\fscy100)}"
            rows.append(f"Dialogue: 0,{ass_time(local)},{ass_time(max(local+0.08,end))},Default,,0,0,0,,{tags}{text}")
            local=end
        cursor+=scene_duration
    path.write_text(header+style+events+"\n".join(rows)+"\n",encoding="utf-8")

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
        selected=[]; seen=set(); missing=[]
        for i,sentence in enumerate(sentences):
            clips=search_clips(req,sentence)
            # Prefer a new clip, but reuse the best scene match rather than
            # silently removing a narrated sentence when the library is sparse.
            best=next((c for c in clips if c["url"] not in seen),None)
            if best is None and clips:
                best=clips[0]
            if best:
                seen.add(best["url"]); selected.append((sentence,best))
            else:
                missing.append(i+1)
            update(progress=14+int(34*(i+1)/len(sentences)),stage=f"Matching scene {i+1}/{len(sentences)}")
        if missing:
            raise RuntimeError("No matching stock footage could be found for scene(s) "+", ".join(map(str,missing))+". Try adding more specific visual keywords or connecting both Pexels and Pixabay.")
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
            run(["ffmpeg","-y","-threads","2","-filter_threads","1","-stream_loop","-1","-i",str(raw),"-t",f"{seg_duration:.3f}","-vf",f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30","-an","-c:v","libx264","-threads","2","-preset","ultrafast","-crf","23","-pix_fmt","yuv420p",str(seg)],timeout=300)
            if not seg.exists() or seg.stat().st_size < 1024:
                raise RuntimeError(f"FFmpeg did not produce a valid video file for scene {i+1}. The downloaded source may contain no decodable video frames.")
            segments.append(seg); raw.unlink(missing_ok=True)
            update(progress=50+int(25*(i+1)/len(selected)),stage=f"Preparing scene {i+1}/{len(selected)}")
        concat=folder/"concat.txt"; concat.write_text("\n".join("file '"+p.name+"'" for p in segments),encoding="utf-8")
        silent=folder/"silent.mp4"; run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),"-c","copy",str(silent)],timeout=300)
        final=folder/"content-tool.mp4"; ass=folder/"subtitles.ass"; write_ass([s for s,_ in selected],duration,ass,req)
        update(stage="Burning your subtitle template and mixing audio",progress=80)
        subtitle_filter=f"ass={ass.as_posix()}"
        cmd=["ffmpeg","-y","-threads","2","-filter_threads","1","-filter_complex_threads","1","-i",str(silent),"-i",str(voice)]
        has_music=bool(music_path and music_path.exists())
        if has_music: cmd += ["-i",str(music_path)]
        cmd += ["-vf",subtitle_filter,"-map","0:v:0","-map","1:a:0"]
        if has_music: cmd += ["-filter_complex",f"[2:a]volume={req.music_volume}[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]","-map","[a]"]
        cmd += ["-c:v","libx264","-threads","2","-preset","ultrafast","-crf","22","-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart",str(final)]
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
