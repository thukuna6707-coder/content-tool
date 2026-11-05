from __future__ import annotations
import os, re, uuid, time, subprocess, threading, base64, json, difflib, textwrap
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
# A single Railway replica has a limited memory budget. Queue renders so two
# simultaneous x264 processes cannot exhaust the container's RAM.
render_semaphore = threading.Semaphore(1)

class RenderRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=180)
    script: str = Field(min_length=20, max_length=12000)
    keywords: str = Field(default="", max_length=1000)
    stock_provider: str = "both"
    video_style: str = "stock"
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

SCENE_STOPWORDS = {"the", "and", "for", "with", "from", "that", "this", "then", "they", "them", "their", "into", "over", "under", "what", "how", "why", "when", "where", "who", "are", "was", "were", "has", "have", "had", "you", "your", "its", "but", "not", "can", "will", "would", "could", "about", "after", "before", "while", "next", "finally", "actually", "called", "carefully", "several", "days", "begins", "begin"}

def tokens(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z0-9]{3,}", text or "") if w.lower() not in SCENE_STOPWORDS}

def score_clip(clip: dict, sentence: str, topic: str = "") -> float:
    """Score evidence in the clip's own metadata, never its search query.

    Search queries are generated from the script, so counting query-word overlap
    as proof would make almost any search result look relevant. Metadata must
    independently mention the scene; topic overlap is only supporting evidence.
    """
    wanted = tokens(sentence)
    found = tokens(" ".join(str(clip.get(k, "")) for k in ("tags", "description", "alt")))
    if not wanted or not found:
        return 0.0
    scene_hits = wanted & found
    scene_coverage = len(scene_hits) / max(1, len(wanted))
    topic_terms = tokens(topic)
    topic_coverage = len(topic_terms & found) / max(1, len(topic_terms)) if topic_terms else 0.0
    # A clip must describe the actual narrated beat. Topic-only matches cannot pass.
    if len(scene_hits) == 0 or scene_coverage < 0.20:
        return 0.0
    return min(1.0, scene_coverage * 0.82 + topic_coverage * 0.18)

def scene_search_queries(sentence: str, topic: str, keywords: str) -> list[str]:
    """Build short scene-first searches with topic context and useful fallbacks."""
    scene_terms = stock_search_query(sentence, max_chars=58)
    topic_terms = stock_search_query(topic, max_chars=28)
    keyword_terms = stock_search_query(keywords, max_chars=42)
    candidates = [
        stock_search_query(scene_terms, topic_terms, max_chars=78),
        scene_terms,
        stock_search_query(topic_terms, scene_terms, max_chars=72),
        stock_search_query(keyword_terms, scene_terms, max_chars=72),
    ]
    return list(dict.fromkeys(q for q in candidates if q))

def pexels_search(key: str, query: str, per_page=12) -> list[dict]:
    if not key: return []
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization":key}, params={"query":query,"per_page":per_page,"orientation":"portrait"}, timeout=25)
    r.raise_for_status()
    out=[]
    for v in r.json().get("videos",[]):
        files=[x for x in v.get("video_files",[]) if x.get("file_type")=="video/mp4" and x.get("link")]
        # Avoid downloading 2K/4K stock masters into a small Railway container.
        # Prefer the best source at or below 720p; if none exists, choose the smallest.
        safe=[x for x in files if int(x.get("width",0) or 0)*int(x.get("height",0) or 0) <= 1280*720]
        f=max(safe or files, key=lambda x:int(x.get("width",0) or 0)*int(x.get("height",0) or 0), default=None)
        if f:
            user=v.get("user") or {}
            out.append({"url":f["link"],"description":str(v.get("alt") or ""),"alt":str(v.get("alt") or ""),"source":"Pexels","video_page":v.get("url","")})
    return out

def pixabay_search(key: str, query: str, per_page=12) -> list[dict]:
    if not key: return []
    r=requests.get("https://pixabay.com/api/videos/",params={"key":key,"q":query,"per_page":per_page,"safesearch":"true"},timeout=25)
    r.raise_for_status()
    out=[]
    for v in r.json().get("hits",[]):
        videos=v.get("videos",{}); f=videos.get("medium") or videos.get("small") or videos.get("large")
        if f and f.get("url"):
            out.append({"url":f["url"],"description":v.get("tags", ""),"tags":v.get("tags", ""),"source":"Pixabay"})
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
    # Query each narration beat independently and combine several focused
    # searches so the best available footage is not limited to the first query.
    queries = scene_search_queries(sentence, req.topic, req.keywords)
    clips = []
    providers = [req.stock_provider] if req.stock_provider in ("pexels", "pixabay") else ["pexels", "pixabay"]
    for provider in providers:
        key = (req.pexels_key or os.getenv("PEXELS_API_KEY", "")) if provider == "pexels" else (req.pixabay_key or os.getenv("PIXABAY_API_KEY", ""))
        if not key: continue
        try:
            for query_rank, query in enumerate(queries[:3]):
                found = pexels_search(key, query) if provider == "pexels" else pixabay_search(key, query)
                for clip in found:
                    clip["search_query"] = query
                    clip["query_rank"] = query_rank
                    clip["match_score"] = score_clip(clip, sentence, req.topic)
                clips.extend(found)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            print(f"{provider} search warning: HTTP {status}; scene query attempts={len(queries[:3])}")
        except Exception as e:
            print(f"{provider} search warning: {type(e).__name__}")
    unique = {}
    for clip in clips:
        if clip.get("url"):
            current = unique.get(clip["url"])
            if current is None or clip.get("match_score", 0) > current.get("match_score", 0):
                unique[clip["url"]] = clip
    return sorted(unique.values(), key=lambda c: (c.get("match_score", 0), -c.get("query_rank", 0)), reverse=True)

def generate_voice(req: RenderRequest, script: str, out: Path) -> list[dict] | None:
    if not req.voice_key: raise RuntimeError("Add the selected voice provider API key in Settings.")
    if req.voice_provider == "fish":
        voice_id = req.voice_id.strip() or FISH_DEFAULT_VOICE_ID
        response = requests.post(
            "https://api.fish.audio/v1/tts/stream/with-timestamp",
            headers={"Authorization": f"Bearer {req.voice_key}", "Content-Type": "application/json", "model": "s2.1-pro-free"},
            json={"text": script, "reference_id": voice_id, "format": "opus", "latency": "balanced"},
            stream=True, timeout=(25, 300)
        )
        if not response.ok:
            detail = response.text[:600]
            raise RuntimeError(f"Fish Audio timestamped speech failed ({response.status_code}): {detail}")
        audio_parts = []
        alignment_by_chunk = {}
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", "replace")
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            encoded = event.get("audio_base64")
            if encoded:
                audio_parts.append(base64.b64decode(encoded))
            alignment = event.get("alignment")
            if alignment and isinstance(alignment.get("segments"), list):
                seq = int(event.get("chunk_seq", 0))
                alignment_by_chunk[seq] = {"offset": float(event.get("chunk_audio_offset_sec") or 0), "segments": alignment["segments"]}
        if not audio_parts:
            raise RuntimeError("Fish Audio returned no audio data. Please check the voice ID and API key.")
        out.write_bytes(b"".join(audio_parts))
        aligned = []
        for seq in sorted(alignment_by_chunk):
            item = alignment_by_chunk[seq]
            for segment in item["segments"]:
                text = str(segment.get("text") or "").strip()
                if not text:
                    continue
                start = max(0.0, item["offset"] + float(segment.get("start", 0)))
                end = max(start + 0.02, item["offset"] + float(segment.get("end", start + 0.02)))
                aligned.append({"text": text, "start": start, "end": end})
        if not aligned:
            raise RuntimeError("Fish Audio returned speech but no timestamp alignment. No unsynchronised subtitles were rendered; please retry.")
        return aligned
    voice=req.voice_id or "21m00Tcm4TlvDq8ikWAM"
    r=requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",headers={"xi-api-key":req.voice_key,"Content-Type":"application/json","Accept":"audio/mpeg"},json={"text":script,"model_id":"eleven_multilingual_v2","voice_settings":{"stability":0.48,"similarity_boost":0.78,"style":0.25,"use_speaker_boost":True}},timeout=240)
    if not r.ok: raise RuntimeError(f"Voice API error ({r.status_code}): {r.text[:700]}")
    out.write_bytes(r.content)
    return None

def srt_time(seconds: float) -> str:
    ms=int(seconds*1000); h=ms//3600000; ms%=3600000; m=ms//60000; ms%=60000; s=ms//1000; ms%=1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def ass_time(seconds: float) -> str:
    cs=max(0,int(seconds*100)); h=cs//360000; cs%=360000; m=cs//6000; cs%=6000; sec=cs//100; cs%=100
    return f"{h}:{m:02}:{sec:02}.{cs:02}"

def ass_color(value: str) -> str:
    value=value if re.fullmatch(r"#[0-9a-fA-F]{6}",value or "") else "#ffffff"
    return "&H00"+value[5:7]+value[3:5]+value[1:3]

def aligned_script_word_times(script: str, raw_segments: list[dict], duration: float) -> list[dict]:
    script_words = re.findall(r"\S+", script)
    aligned_words = []
    for segment in raw_segments:
        parts = re.findall(r"\S+", str(segment.get("text") or ""))
        if not parts: continue
        start = float(segment.get("start", 0)); end = max(start + 0.02, float(segment.get("end", start + 0.02)))
        weights = [max(1, len(re.sub(r"[^\w]", "", word))) for word in parts]
        total = sum(weights) or 1; cursor = start
        for word, weight in zip(parts, weights):
            next_cursor = cursor + (end - start) * weight / total
            aligned_words.append({"text": word, "start": cursor, "end": max(cursor + 0.02, next_cursor)})
            cursor = next_cursor
    normalize = lambda value: re.sub(r"[^a-z0-9']", "", value.lower())
    wanted = [normalize(w) for w in script_words]
    heard = [normalize(w["text"]) for w in aligned_words]
    times = [None] * len(script_words)
    matcher = difflib.SequenceMatcher(None, wanted, heard, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                times[i] = (aligned_words[j]["start"], aligned_words[j]["end"])
        elif tag == "replace" and i2 > i1 and j2 > j1:
            a = aligned_words[j1]["start"]; b = aligned_words[j2-1]["end"]
            weights = [max(1, len(re.sub(r"[^\w]", "", script_words[i]))) for i in range(i1, i2)]
            total = sum(weights) or 1; cursor = a
            for i, weight in zip(range(i1, i2), weights):
                nxt = cursor + (b-a)*weight/total; times[i] = (cursor, max(cursor+0.02,nxt)); cursor = nxt
    # Interpolate any script words that ASR-style text alignment could not pair.
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1; continue
        j = i
        while j < len(times) and times[j] is None: j += 1
        prev_end = times[i-1][1] if i > 0 and times[i-1] else 0.0
        next_start = times[j][0] if j < len(times) and times[j] else duration
        weights = [max(1, len(re.sub(r"[^\w]", "", script_words[k]))) for k in range(i,j)]
        total = sum(weights) or 1; cursor = prev_end
        for k, weight in zip(range(i,j), weights):
            nxt = cursor + max(0.02, next_start-prev_end) * weight / total
            times[k] = (cursor, nxt); cursor = nxt
        i = j
    return [{"text": word, "start": max(0.0, min(duration, times[i][0])), "end": max(0.0, min(duration, max(times[i][0]+0.02, times[i][1])))} for i, word in enumerate(script_words)]

def build_sentence_windows(script: str, voice_alignment: list[dict] | None, duration: float) -> list[tuple[float, float]]:
    sentences = split_sentences(script)
    if not sentences:
        return []
    if voice_alignment:
        word_times = aligned_script_word_times(script, voice_alignment, duration)
        counts = [len(re.findall(r"\S+", sentence)) for sentence in sentences]
        starts = []; cursor = 0
        for count in counts:
            group = word_times[cursor:cursor + count]
            starts.append(group[0]["start"] if group else (starts[-1] if starts else 0.0))
            cursor += count
        windows = []
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else duration
            start = 0.0 if i == 0 else min(duration, start)
            end = max(start + 0.08, min(duration, end))
            windows.append((start, end))
        return windows
    weights = [max(1, len(re.findall(r"\w+", sentence))) for sentence in sentences]
    total = sum(weights) or 1; cursor = 0.0; windows = []
    for weight in weights:
        end = cursor + duration * weight / total
        windows.append((cursor, end)); cursor = end
    return windows


def write_ass(script: str, duration: float, path: Path, req: RenderRequest, voice_alignment: list[dict] | None = None):
    fonts={"DejaVu Sans","DejaVu Sans Condensed","DejaVu Serif","DejaVu Sans Mono"}
    font=req.subtitle_font if req.subtitle_font in fonts else "DejaVu Sans"
    alignment={"bottom":2,"middle":5,"top":8}.get(req.subtitle_position,2)
    margin={"bottom":82,"middle":0,"top":70}.get(req.subtitle_position,82)
    bold=1 if req.subtitle_weight in ("bold","heavy") else 0
    outline=max(0,min(8,req.subtitle_outline_width)); border=3 if req.subtitle_background else 1
    back=ass_color(req.subtitle_background_color).replace("&H00","&H55",1)
    outline_color=ass_color(req.subtitle_outline_color); primary=ass_color(req.subtitle_color); highlight=ass_color(req.subtitle_highlight_color)
    target=1080 if req.quality=="1080p" else 720
    if req.aspect=="16:9": play_w,play_h=target,int(target*9/16)
    elif req.aspect=="1:1": play_w=play_h=target
    else: play_w,play_h=int(target*9/16),target
    header=(f"[Script Info]\nScriptType: v4.00+\nPlayResX: {play_w}\nPlayResY: {play_h}\nScaledBorderAndShadow: yes\nWrapStyle: 2\n\n"
      "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
    style=f"Style: Default,{font},{req.subtitle_size},{primary},{highlight},{outline_color},{back},{bold},0,0,0,100,100,0,0,{border},{outline},1,{alignment},64,64,{margin},1\n\n"
    if voice_alignment:
        words=aligned_script_word_times(script, voice_alignment, duration)
    else:
        raw_words=re.findall(r"\S+",script); weights=[max(1,len(re.sub(r"[^\w]","",w))) for w in raw_words]; total=sum(weights) or 1; cursor=0.0; words=[]
        for word,weight in zip(raw_words,weights):
            end=cursor+duration*weight/total; words.append({"text":word,"start":cursor,"end":end}); cursor=end
    groups=[]
    if req.subtitle_mode=="word":
        groups=[[w] for w in words]
    elif req.subtitle_mode=="phrase":
        step=max(2,min(6,req.subtitle_phrase_words)); groups=[words[i:i+step] for i in range(0,len(words),step)]
    else:
        cursor=0
        for sentence in split_sentences(script):
            count=len(re.findall(r"\S+",sentence)); groups.append(words[cursor:cursor+count]); cursor+=count
        if cursor < len(words): groups.append(words[cursor:])
    events="[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    rows=[]
    for group in groups:
        if not group: continue
        text=" ".join(w["text"] for w in group)
        if req.subtitle_uppercase: text=text.upper()
        text=text.replace("{","\\{").replace("}","\\}").replace("\n"," ")
        tags="{\\c"+highlight+"}" if req.subtitle_mode=="word" else ""
        if req.subtitle_animation=="fade": tags+="{\\fad(70,70)}"
        elif req.subtitle_animation=="pop": tags+="{\\fscx108\\fscy108\\t(0,120,\\fscx100\\fscy100)}"
        start=min(duration,max(0.0,group[0]["start"])); end=min(duration,max(start+0.04,group[-1]["end"]))
        rows.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{tags}{text}")
    path.write_text(header+style+events+"\n".join(rows)+"\n",encoding="utf-8")

def download_clip(url: str, path: Path):
    with requests.get(url,stream=True,timeout=90) as r:
        r.raise_for_status()
        with path.open("wb") as f:
            for chunk in r.iter_content(1024*1024):
                if chunk: f.write(chunk)
    if path.stat().st_size < 12000: raise RuntimeError("Downloaded stock clip is unexpectedly small.")

def render_motion_graphics_scene(sentence: str, topic: str, seg: Path, seg_duration: float, width: int, height: int, folder: Path, index: int):
    """Render a clean 2D motion-graphics explainer scene; narration stays in subtitles only."""
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", sentence) if w.lower() not in SCENE_STOPWORDS]
    labels = list(dict.fromkeys(words))[:3] or ["Key idea", "Explained"]
    label_files = []
    for n, label in enumerate(labels):
        f = folder / f"motion_{index}_label_{n}.txt"
        f.write_text(label[:22], encoding="utf-8")
        label_files.append(f)
    topic_file = folder / f"motion_{index}_topic.txt"
    topic_file.write_text((topic or "THE BIG IDEA")[:44].upper(), encoding="utf-8")
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    small = max(18, int(height * 0.025)); label_size = max(24, int(height * 0.043))
    # Each beat gets a moving infographic composition: orbiting blocks, animated
    # connector bars and concise concept labels. The narration is never drawn here.
    x1 = f"(w*0.18)+(sin(t*1.7+{index})*w*0.045)"
    x2 = f"(w*0.62)+(cos(t*1.3+{index})*w*0.045)"
    y1 = f"(h*0.36)+(sin(t*1.1+{index})*h*0.035)"
    y2 = f"(h*0.61)+(cos(t*1.5+{index})*h*0.035)"
    filters = [
        f"drawbox=x=0:y=0:w=iw:h={max(6,height//150)}:color=0x8b7cff:t=fill",
        f"drawbox=x={x1}:y={y1}:w={int(width*.20)}:h={max(12,int(height*.012))}:color=0x8b7cff@0.95:t=fill",
        f"drawbox=x={x2}:y={y2}:w={int(width*.20)}:h={max(12,int(height*.012))}:color=0x42d9c8@0.95:t=fill",
        f"drawbox=x={x1}:y={y1}:w={int(width*.20)}:h={int(height*.15)}:color=0x222b49@0.95:t=fill",
        f"drawbox=x={x2}:y={y2}:w={int(width*.20)}:h={int(height*.15)}:color=0x173c48@0.95:t=fill",
        f"drawtext=fontfile={font}:textfile={topic_file.as_posix()}:fontcolor=0xAEB8FF:fontsize={small}:x=(w-text_w)/2:y=h*0.13",
        f"drawtext=fontfile={font}:textfile={label_files[0].as_posix()}:fontcolor=white:fontsize={label_size}:x=(w-text_w)/2:y=h*0.405",
    ]
    if len(label_files) > 1:
        filters.append(f"drawtext=fontfile={font}:textfile={label_files[1].as_posix()}:fontcolor=0x9CF5E8:fontsize={label_size}:x=(w-text_w)/2:y=h*0.655")
    if len(label_files) > 2:
        filters.append(f"drawtext=fontfile={font}:textfile={label_files[2].as_posix()}:fontcolor=0xD7D4FF:fontsize={max(18,int(label_size*.72))}:x=(w-text_w)/2:y=h*0.82")
    filters.append("format=yuv420p")
    vf=",".join(filters)
    run(["ffmpeg","-y","-f","lavfi","-i",f"color=c=0x0B1020:s={width}x{height}:r=30","-t",f"{seg_duration:.3f}","-vf",vf,"-an","-c:v","libx264","-threads","1","-preset","ultrafast","-tune","zerolatency","-x264-params","threads=1:lookahead-threads=1:sync-lookahead=0:rc-lookahead=0:ref=1:bframes=0","-crf","26","-pix_fmt","yuv420p",str(seg)],timeout=180)

def render_job(job_id: str, req: RenderRequest, music_path: Optional[Path]):
    folder=JOBS_DIR/job_id; folder.mkdir(parents=True,exist_ok=True)
    def update(**kw):
        with lock: jobs[job_id].update(kw)
    try:
        update(status="processing",stage="Generating narration",progress=5)
        voice=folder/("voice.opus" if req.voice_provider == "fish" else "voice.mp3"); voice_alignment=generate_voice(req,req.script,voice)
        duration=ffprobe_duration(voice); sentences=split_sentences(req.script)
        if not sentences: raise RuntimeError("The script has no readable sentences.")
        selected=[]; seen=set(); generated_scene_count=0
        if req.video_style == "motion_graphics":
            update(stage="Designing motion-graphics explainer scenes",progress=14)
            selected=[(sentence,{"url":None,"source":"FAST FLOW Motion Graphics","match_score":1.0,"generated_motion_scene":True}) for sentence in sentences]
            generated_scene_count=len(selected)
        else:
            update(stage="Searching stock footage",progress=14)
            for i,sentence in enumerate(sentences):
                clips=search_clips(req,sentence)
                best=next((c for c in clips if c.get("url") not in seen and c.get("match_score",0) >= 0.24),None)
                if best:
                    seen.add(best["url"]); selected.append((sentence,best))
                else:
                    # Use abstract motion graphics, never a full-script text card.
                    selected.append((sentence,{"url":None,"source":"FAST FLOW Motion Graphics fallback","match_score":1.0,"generated_motion_scene":True}))
                    generated_scene_count += 1
                update(progress=14+int(34*(i+1)/len(sentences)),stage=f"Matching scene {i+1}/{len(sentences)}")
        if not selected: raise RuntimeError("No scenes could be prepared from this script.")
        if generated_scene_count:
            update(stage=f"Creating {generated_scene_count} script-matched visual scene(s)",progress=47)
        scene_windows=build_sentence_windows(req.script,voice_alignment,duration)
        scene_durations=[max(0.08,end-start) for start,end in scene_windows]
        if len(scene_durations) != len(selected):
            raise RuntimeError("Scene timing could not be aligned with the narration script. Please retry.")
        target=1080 if req.quality=="1080p" else 720
        portrait_width=max(2, int(round((target*9/16)/2))*2)
        if req.aspect=="16:9": width,height=target,portrait_width
        elif req.aspect=="1:1": width=height=target
        else: width,height=portrait_width,target
        segments=[]; update(stage="Downloading and assembling scenes",progress=50)
        for i,(sentence,clip) in enumerate(selected):
            seg=folder/f"scene_{i}.mp4"
            seg_duration=scene_durations[i]
            if clip.get("generated_motion_scene"):
                render_motion_graphics_scene(sentence, req.topic, seg, seg_duration, width, height, folder, i)
            else:
                raw=folder/f"raw_{i}.mp4"
                download_clip(clip["url"],raw)
                run(["ffmpeg","-y","-threads","1","-filter_threads","1","-filter_complex_threads","1","-stream_loop","-1","-i",str(raw),"-t",f"{seg_duration:.3f}","-vf",f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=fast_bilinear,crop={width}:{height},setsar=1,fps=30,format=yuv420p","-an","-c:v","libx264","-threads","1","-preset","ultrafast","-tune","zerolatency","-x264-params","threads=1:lookahead-threads=1:sync-lookahead=0:rc-lookahead=0:ref=1:bframes=0","-crf","23","-pix_fmt","yuv420p",str(seg)],timeout=300)
                raw.unlink(missing_ok=True)
            if not seg.exists() or seg.stat().st_size < 1024:
                raise RuntimeError(f"FFmpeg did not produce a valid video file for scene {i+1}.")
            segments.append(seg)
            update(progress=50+int(25*(i+1)/len(selected)),stage=f"Preparing scene {i+1}/{len(selected)}")
        concat=folder/"concat.txt"; concat.write_text("\n".join("file '"+p.name+"'" for p in segments),encoding="utf-8")
        silent=folder/"silent.mp4"; run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),"-c","copy",str(silent)],timeout=300)
        final=folder/"content-tool.mp4"; ass=folder/"subtitles.ass"; write_ass(req.script,duration,ass,req,voice_alignment)
        update(stage="Burning your subtitle template and mixing audio",progress=80)
        subtitle_filter=f"ass={ass.as_posix()}"
        cmd=["ffmpeg","-y","-threads","1","-filter_threads","1","-filter_complex_threads","1","-i",str(silent),"-i",str(voice)]
        has_music=bool(music_path and music_path.exists())
        if has_music: cmd += ["-i",str(music_path)]
        cmd += ["-vf",subtitle_filter,"-map","0:v:0","-map","1:a:0"]
        if has_music: cmd += ["-filter_complex",f"[2:a]volume={req.music_volume}[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]","-map","[a]"]
        cmd += ["-c:v","libx264","-threads","1","-preset","ultrafast","-tune","zerolatency","-x264-params","threads=1:lookahead-threads=1:sync-lookahead=0:rc-lookahead=0:ref=1:bframes=0","-crf","23","-pix_fmt","yuv420p","-c:a","aac","-threads","1","-b:a","128k","-max_muxing_queue_size","512","-shortest","-movflags","+faststart",str(final)]
        try:
            run(cmd,timeout=900)
        except RuntimeError as render_error:
            # Railway may still kill the first pass under a tight memory quota.
            # Retry once with a bounded 720p frame, 24fps, and a lower-memory
            # scale path. Keep the selected aspect ratio and subtitle timing.
            if "signal 9" not in str(render_error).lower():
                raise
            safe_target=720
            safe_width=max(2, int(round((safe_target*9/16)/2))*2)
            if req.aspect=="16:9": safe_w,safe_h=safe_target,safe_width
            elif req.aspect=="1:1": safe_w=safe_h=safe_target
            else: safe_w,safe_h=safe_width,safe_target
            low_memory_filter=(f"scale={safe_w}:{safe_h}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
                               f"pad={safe_w}:{safe_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,{subtitle_filter}")
            cmd[cmd.index("-vf")+1]=low_memory_filter
            cmd[cmd.index("-crf")+1]="28"
            update(stage="Retrying in memory-safe 720p mode",progress=86)
            run(cmd,timeout=1200)
        # Release intermediate media as soon as the final MP4 is complete.
        for temporary in [*segments, silent, concat, voice, ass]:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
        update(status="completed",stage="Ready to download",progress=100,download_url=f"/api/jobs/{job_id}/download",scenes=[{"sentence":s,"source":c["source"],"duration":round(scene_durations[i],1),"start":round(scene_windows[i][0],2),"end":round(scene_windows[i][1],2),"visual_query":c.get("search_query", ""),"match_score":round(c.get("match_score",0),2)} for i,(s,c) in enumerate(selected)],subtitle_sync=("Fish Audio word timestamps" if voice_alignment else "estimated timing"))
    except Exception as e: update(status="failed",stage="Render failed",error=str(e)[:1600],progress=100)

def queued_render_job(job_id: str, req: RenderRequest, music_path: Optional[Path]):
    with render_semaphore:
        render_job(job_id, req, music_path)

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
    if req.video_style != "motion_graphics" and not req.pexels_key and not req.pixabay_key and not os.getenv("PEXELS_API_KEY") and not os.getenv("PIXABAY_API_KEY"):
        raise HTTPException(400,"Add at least one stock footage API key, or choose Motion Graphics Explainer mode.")
    if music and isinstance(music,UploadFile):
        if music.content_type not in ("audio/mpeg","audio/mp3","audio/wav","audio/x-wav","audio/mp4","audio/aac","audio/ogg"):
            raise HTTPException(400,"Unsupported music file type.")
        ext=Path(music.filename or "music.mp3").suffix or ".mp3"
        folder=JOBS_DIR/uuid.uuid4().hex[:12]; folder.mkdir(parents=True,exist_ok=True)
        music_path=folder/f"background{ext}"; music_path.write_bytes(await music.read())
    job_id=uuid.uuid4().hex[:12]
    with lock: jobs[job_id]={"id":job_id,"status":"queued","stage":"Waiting to start","progress":0,"created":time.time()}
    threading.Thread(target=queued_render_job,args=(job_id,req,music_path),daemon=True).start()
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
