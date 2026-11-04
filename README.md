# Content Tool

Automated faceless-video production: stock footage search (Pexels/Pixabay), AI narration (Fish Audio/ElevenLabs), timed subtitles, scene matching and FFmpeg export.

## Features
- Topic, script and keyword driven projects
- Pexels and Pixabay stock-video search with relevance scoring
- Fish Audio or ElevenLabs narration
- Sentence-level scene allocation and SRT subtitles
- 9:16, 16:9 and 1:1 exports at 720p or 1080p
- Background music upload, volume control, subtitle styling
- Render status polling and MP4 download
- API keys supplied per job and never written to disk

## Run locally
Requires Python 3.11+ and FFmpeg/ffprobe installed and available on PATH.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```
Open http://localhost:8000.

## Railway deployment
1. Create a Railway project and deploy this GitHub repository as a service.
2. Railway detects the Dockerfile. The app listens on Railway's PORT.
3. Add a persistent volume mounted at `/data` if you want completed renders to survive redeployments. Without a volume, generated files are temporary.
4. Open the generated Railway domain.

## API keys
- Pexels: https://www.pexels.com/api/
- Pixabay: https://pixabay.com/api/docs/
- ElevenLabs: https://elevenlabs.io/
- Fish Audio: https://fish.audio/

Keys are entered in the UI and sent only to the backend for the requested render. Use your own keys and follow each provider's terms, rate limits and media licensing rules.

## Notes on matching
Content Tool ranks candidate clips using topic, keyword and sentence text. It creates one scene per script sentence where enough stock footage is available. Automated semantic matching is best-effort, not guaranteed perfect; review the scene list and replace footage when needed. Stock media is subject to each provider's license and attribution requirements.

## Current scope
This is a functional first release. It does not include accounts, billing, cloud project history, AI script generation, or a persistent job database. Jobs are held in process memory; use a single Railway replica and a mounted volume for reliable temporary render storage.