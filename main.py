import os
import re
import urllib.parse
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="Video Downloader Pro")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, "templates", "index.html")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

class ExtractRequest(BaseModel):
    url: str

def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if "tiktok.com" in url_lower:
        return "tiktok"
    elif "pinterest.com" in url_lower or "pin.it" in url_lower:
        return "pinterest"
    elif "instagram.com" in url_lower:
        return "instagram"
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    return "general"

def extract_tiktok_tikwm(url: str):
    """Extrai vídeo do TikTok sem marca d'água via TikWM."""
    try:
        api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
        r = requests.get(api_url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                res_data = data.get("data", {})
                video_url = res_data.get("hdplay") or res_data.get("play")
                return {
                    "title": res_data.get("title", "TikTok Video"),
                    "thumbnail": res_data.get("cover", ""),
                    "video_url": video_url,
                    "platform": "TikTok (Sem Marca D'água)",
                }
    except Exception:
        pass
    return None

def extract_with_ytdlp(url: str, platform_name: str):
    """Extrai vídeo usando yt-dlp na melhor qualidade disponível."""
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        video_url = info.get("url")
        if not video_url and "formats" in info:
            formats = [f for f in info["formats"] if f.get("url")]
            if formats:
                video_url = formats[-1]["url"]

        return {
            "title": info.get("title", f"{platform_name} Video"),
            "thumbnail": info.get("thumbnail", ""),
            "video_url": video_url,
            "platform": platform_name,
        }

@app.get("/", response_class=FileResponse)
async def home():
    if not os.path.exists(INDEX_FILE):
        return HTMLResponse("<h1>Página em carregamento...</h1>")
    return FileResponse(INDEX_FILE)

@app.post("/api/extract")
async def extract_video(req: ExtractRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Por favor, envie um link válido começando com http.")

    platform = detect_platform(url)

    # 1. TikTok (Tenta via TikWM primeiro para garantir sem marca d'água)
    if platform == "tiktok":
        res = extract_tiktok_tikwm(url)
        if res and res.get("video_url"):
            return {"success": True, **res}

    # 2. Pinterest, Instagram, Twitter, YouTube e fallback do TikTok
    platform_names = {
        "tiktok": "TikTok",
        "pinterest": "Pinterest",
        "instagram": "Instagram",
        "twitter": "Twitter / X",
        "youtube": "YouTube",
        "general": "Vídeo",
    }

    try:
        res = extract_with_ytdlp(url, platform_names.get(platform, "Vídeo"))
        if res and res.get("video_url"):
            return {"success": True, **res}
    except Exception as e:
        # Se for TikTok e yt-dlp falhar, tentar TikWM novamente
        if platform == "tiktok":
            res = extract_tiktok_tikwm(url)
            if res and res.get("video_url"):
                return {"success": True, **res}
        raise HTTPException(status_code=400, detail=f"Não foi possível obter o vídeo: {str(e)}")

    raise HTTPException(status_code=400, detail="Não foi possível encontrar o arquivo de vídeo deste link.")

@app.get("/api/download")
async def stream_download(url: str, title: str = "video"):
    """Faz streaming do vídeo diretamente para o navegador forçando o download."""
    safe_title = re.sub(r'[^a-zA-Z0-9_\- ]', '', title).strip() or "video"
    filename = f"{safe_title}.mp4"

    def iter_content():
        with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    yield chunk

    encoded_filename = urllib.parse.quote(filename)
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"; filename*=UTF-8\'\'{encoded_filename}',
        "Content-Type": "video/mp4",
    }
    return StreamingResponse(iter_content(), headers=headers)
