import os
import re
import uuid
import urllib.parse
from html import unescape
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from pydantic import BaseModel
import yt_dlp

try:
    from curl_cffi import requests as cffi_requests
except Exception:
    cffi_requests = None

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None

app = FastAPI(title="Video Downloader Pro")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, "templates", "index.html")
TEMP_DIR = os.path.join(BASE_DIR, "temp_downloads")
os.makedirs(TEMP_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
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

def extract_tiktok(url: str):
    """Extrai vídeo do TikTok sem marca d'água em HD via TikWM usando bypass de Cloudflare/TLS."""
    clean_url = url.strip()
    last_err = "Nenhum erro registrado"
    
    # 1. Tentativa com curl_cffi (bypass de Cloudflare WAF no Render)
    if cffi_requests:
        try:
            r = cffi_requests.post(
                "https://www.tikwm.com/api/",
                data={"url": clean_url, "hd": 1},
                impersonate="chrome120",
                timeout=15
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == 0:
                    res_data = data.get("data", {})
                    video_url = res_data.get("hdplay") or res_data.get("play")
                    if video_url:
                        if video_url.startswith("/"):
                            video_url = "https://www.tikwm.com" + video_url
                        return {
                            "title": res_data.get("title") or "TikTok Video",
                            "thumbnail": res_data.get("cover", ""),
                            "download_type": "direct",
                            "source_url": video_url,
                            "platform": "TikTok (Sem Marca D'água)",
                        }, None
                else:
                    last_err = f"TikWM code={data.get('code')}, msg={data.get('msg')}"
            else:
                last_err = f"TikWM status={r.status_code}, text={r.text[:100]}"
        except Exception as e:
            last_err = f"Exception curl_cffi: {str(e)}"
    else:
        last_err = "curl_cffi não está disponível"

    # 2. Fallback com requests padrão
    try:
        r = requests.post(
            "https://www.tikwm.com/api/",
            data={"url": clean_url, "hd": 1},
            headers=HEADERS,
            timeout=12
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                res_data = data.get("data", {})
                video_url = res_data.get("hdplay") or res_data.get("play")
                if video_url:
                    if video_url.startswith("/"):
                        video_url = "https://www.tikwm.com" + video_url
                    return {
                        "title": res_data.get("title") or "TikTok Video",
                        "thumbnail": res_data.get("cover", ""),
                        "download_type": "direct",
                        "source_url": video_url,
                        "platform": "TikTok (Sem Marca D'água)",
                    }, None
    except Exception as e:
        last_err += f" | requests: {str(e)}"

    return None, last_err

def extract_instagram(url: str):
    """Extrai mídia de Reel ou Post público do Instagram."""
    req_module = cffi_requests if cffi_requests else requests
    try:
        if cffi_requests:
            r = cffi_requests.get(url.strip(), impersonate="chrome120", timeout=12)
        else:
            r = requests.get(url.strip(), headers=HEADERS, timeout=12)
            
        if r.status_code == 200:
            html = r.text
            og_match = re.search(r'<meta\s+property=["\']og:video["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
            if og_match:
                video_url = unescape(og_match.group(1))
                thumb_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
                thumb = unescape(thumb_match.group(1)) if thumb_match else ""
                return {
                    "title": "Instagram Video",
                    "thumbnail": thumb,
                    "download_type": "direct",
                    "source_url": video_url,
                    "platform": "Instagram",
                }
    except Exception as e:
        print("Erro Instagram scraper:", e)
    return None

def extract_metadata_ytdlp(url: str, platform_name: str):
    """Extrai metadados do vídeo usando yt-dlp sem baixar o arquivo ainda."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title", f"{platform_name} Video"),
            "thumbnail": info.get("thumbnail", ""),
            "download_type": "ytdlp",
            "source_url": url,
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
        raise HTTPException(status_code=400, detail="Envie um link válido começando com http.")

    platform = detect_platform(url)

    # 1. TikTok (sem marca d'água direto via TikWM com TLS impersonation)
    if platform == "tiktok":
        res, err_detail = extract_tiktok(url)
        if res:
            return {"success": True, **res}
        raise HTTPException(status_code=400, detail=f"Erro TikTok: {err_detail}")

    # 2. Instagram
    if platform == "instagram":
        res = extract_instagram(url)
        if res:
            return {"success": True, **res}

    # 3. Pinterest, YouTube, Twitter, Instagram fallback
    platform_names = {
        "tiktok": "TikTok",
        "pinterest": "Pinterest",
        "instagram": "Instagram",
        "twitter": "Twitter / X",
        "youtube": "YouTube",
        "general": "Vídeo",
    }
    p_name = platform_names.get(platform, "Vídeo")

    try:
        res = extract_metadata_ytdlp(url, p_name)
        return {"success": True, **res}
    except Exception as e:
        err_msg = str(e)
        if "Private video" in err_msg or "Sign in to confirm you’re not a bot" in err_msg:
            raise HTTPException(status_code=400, detail="Esta plataforma bloqueia downloads anônimos de servidores em nuvem (exige login do usuário).")
        raise HTTPException(status_code=400, detail=f"Não foi possível processar este link: {err_msg}")

def cleanup_file(filepath: str):
    """Remove arquivo temporário do servidor após envio."""
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass

@app.get("/api/download")
async def handle_download(url: str, dtype: str = "ytdlp", title: str = "video", background_tasks: BackgroundTasks = None):
    """Baixa e monta o arquivo MP4 real de alta resolução para o usuário."""
    safe_title = re.sub(r'[^a-zA-Z0-9_\- ]', '', title).strip() or "video"
    filename = f"{safe_title}.mp4"
    encoded_filename = urllib.parse.quote(filename)

    # Caso 1: Download direto (TikTok sem marca d'água, etc.)
    if dtype == "direct":
        def iter_content():
            req_func = cffi_requests.get if cffi_requests else requests.get
            headers = HEADERS if not cffi_requests else {}
            with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"; filename*=UTF-8\'\'{encoded_filename}',
            "Content-Type": "video/mp4",
        }
        return StreamingResponse(iter_content(), headers=headers)

    # Caso 2: Processamento via yt-dlp + ffmpeg (Pinterest, Twitter, etc.)
    temp_id = uuid.uuid4().hex[:8]
    temp_out = os.path.join(TEMP_DIR, f"{temp_id}.%(ext)s")
    final_file = os.path.join(TEMP_DIR, f"{temp_id}.mp4")

    ydl_opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": temp_out,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(final_file):
            for f in os.listdir(TEMP_DIR):
                if f.startswith(temp_id):
                    final_file = os.path.join(TEMP_DIR, f)
                    break

        if not os.path.exists(final_file) or os.path.getsize(final_file) < 1000:
            raise HTTPException(status_code=500, detail="Falha ao gerar o arquivo de vídeo completo.")

        # Agenda a remoção do arquivo assim que o download terminar
        background_tasks.add_task(cleanup_file, final_file)

        return FileResponse(
            path=final_file,
            filename=filename,
            media_type="video/mp4"
        )
    except Exception as e:
        cleanup_file(final_file)
        raise HTTPException(status_code=500, detail=f"Erro ao processar o download do vídeo: {str(e)}")
