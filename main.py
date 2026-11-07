import os
import re
import uuid
import base64
import urllib.parse
from html import unescape
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
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
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    elif "reddit.com" in url_lower or "redd.it" in url_lower:
        return "reddit"
    elif "facebook.com" in url_lower or "fb.watch" in url_lower:
        return "facebook"
    elif "instagram.com" in url_lower:
        return "instagram"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    return "general"

def extract_twitter(url: str):
    """Extrai vídeo do Twitter / X via TwitSave em todas as qualidades disponíveis ou fallback yt-dlp."""
    clean_url = url.strip()
    
    # 1. Tentativa via TwitSave
    try:
        ts_url = f"https://twitsave.com/info?url={urllib.parse.quote(clean_url)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        r = requests.get(ts_url, headers=headers, timeout=12)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            candidates = []
            seen_urls = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "download?file=" in href:
                    encoded_file = href.split("download?file=")[1].split("&")[0]
                    try:
                        decoded_file = base64.b64decode(urllib.parse.unquote(encoded_file)).decode("utf-8")
                        if decoded_file in seen_urls:
                            continue
                        seen_urls.add(decoded_file)
                        text = a.get_text(strip=True)
                        res_match = re.search(r'(\d{3,4})[pP]|\b(\d{3,4})x(\d{3,4})\b', text)
                        h = 0
                        if res_match:
                            if res_match.group(1):
                                h = int(res_match.group(1))
                            elif res_match.group(3):
                                h = int(res_match.group(3))
                        candidates.append((h, decoded_file, text))
                    except Exception:
                        pass
            
            if candidates:
                # Ordena da maior para a menor resolução
                candidates.sort(key=lambda x: x[0], reverse=True)
                title_elem = soup.find("p", class_=re.compile(r"m-2|text|content"))
                title = title_elem.get_text(strip=True)[:100] if title_elem else "Twitter Video"
                
                formats = []
                for h, direct_url, raw_text in candidates:
                    if h >= 1080:
                        label = "1080p Full HD"
                        badge = "Melhor Qualidade"
                    elif h >= 720:
                        label = "720p HD"
                        badge = "Alta Resolução"
                    elif h > 0:
                        label = f"{h}p SD"
                        badge = "Econômico"
                    else:
                        label = "MP4 Alta Resolução"
                        badge = "Padrão"
                    formats.append({
                        "label": label,
                        "quality": f"{h}p" if h > 0 else "mp4",
                        "badge": badge,
                        "url": direct_url,
                        "dtype": "direct",
                        "format_id": str(h) if h > 0 else "best",
                        "is_audio": False
                    })
                
                return {
                    "title": title,
                    "thumbnail": "",
                    "platform": "Twitter / X",
                    "formats": formats
                }
    except Exception as e:
        print("Erro TwitSave:", e)

    # 2. Fallback via yt-dlp
    try:
        return extract_metadata_ytdlp(clean_url, "Twitter / X")
    except Exception as e:
        print("Erro yt-dlp twitter:", e)

    return None

def extract_tiktok(url: str):
    """Extrai vídeo do TikTok sem marca d'água com opções de qualidade em HD e áudio."""
    clean_url = url.strip()
    
    # 1. Tentativa via TikWM (rápido e com campos hdplay, play e music)
    req_func = cffi_requests.post if cffi_requests else requests.post
    kw = {"impersonate": "chrome120"} if cffi_requests else {"headers": HEADERS}
    try:
        r = req_func(
            "https://www.tikwm.com/api/",
            data={"url": clean_url, "hd": 1},
            timeout=12,
            **kw
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                res_data = data.get("data", {})
                title = res_data.get("title") or "TikTok Video"
                thumbnail = res_data.get("cover", "")
                
                formats = []
                if res_data.get("hdplay"):
                    hd = res_data["hdplay"]
                    if hd.startswith("/"): hd = "https://www.tikwm.com" + hd
                    formats.append({
                        "label": "1080p Full HD (Sem Marca)",
                        "quality": "1080p",
                        "badge": "Melhor Qualidade",
                        "url": hd,
                        "dtype": "direct",
                        "format_id": "1080",
                        "is_audio": False
                    })
                if res_data.get("play"):
                    sd = res_data["play"]
                    if sd.startswith("/"): sd = "https://www.tikwm.com" + sd
                    formats.append({
                        "label": "720p HD (Padrão)",
                        "quality": "720p",
                        "badge": "Mais Rápido",
                        "url": sd,
                        "dtype": "direct",
                        "format_id": "720",
                        "is_audio": False
                    })
                if res_data.get("music"):
                    music = res_data["music"]
                    if music.startswith("/"): music = "https://www.tikwm.com" + music
                    formats.append({
                        "label": "Áudio Original (MP3)",
                        "quality": "audio",
                        "badge": "Áudio",
                        "url": music,
                        "dtype": "direct",
                        "format_id": "audio",
                        "is_audio": True
                    })
                
                if formats:
                    return {
                        "title": title,
                        "thumbnail": thumbnail,
                        "platform": "TikTok",
                        "formats": formats
                    }, None
    except Exception as e:
        print("Erro TikWM backend:", e)

    # 2. Fallback via SaveTik
    try:
        r = req_func(
            "https://savetik.co/api/ajaxSearch",
            data={"q": clean_url, "lang": "en"},
            timeout=15,
            **kw
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "ok":
                html = data.get("data", "")
                soup = BeautifulSoup(html, "html.parser")
                
                formats = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    text = a.get_text(strip=True).lower()
                    if "snapcdn.app" in href:
                        if "hd" in text:
                            formats.append({
                                "label": "1080p Full HD (Sem Marca)",
                                "quality": "1080p",
                                "badge": "Melhor Qualidade",
                                "url": href,
                                "dtype": "direct",
                                "format_id": "1080",
                                "is_audio": False
                            })
                        elif "mp3" in text or "audio" in text:
                            formats.append({
                                "label": "Áudio do Vídeo (MP3)",
                                "quality": "audio",
                                "badge": "Áudio",
                                "url": href,
                                "dtype": "direct",
                                "format_id": "audio",
                                "is_audio": True
                            })
                        else:
                            formats.append({
                                "label": "720p HD (Padrão)",
                                "quality": "720p",
                                "badge": "Normal",
                                "url": href,
                                "dtype": "direct",
                                "format_id": "720",
                                "is_audio": False
                            })
                
                thumb_img = soup.find("img", src=True)
                thumbnail = thumb_img["src"] if thumb_img else ""
                title_tag = soup.find("h3") or soup.find("h4")
                title = title_tag.get_text(strip=True) if title_tag else "TikTok Video"
                
                if formats:
                    return {
                        "title": title,
                        "thumbnail": thumbnail,
                        "platform": "TikTok",
                        "formats": formats
                    }, None
    except Exception as e:
        print("Erro SaveTik backend:", e)

    return None, "Não foi possível extrair as mídias do TikTok."

def extract_instagram(url: str):
    """Extrai Reel ou Post público do Instagram com opções de qualidade."""
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
                    "platform": "Instagram",
                    "formats": [
                        {
                            "label": "Qualidade Máxima Original (HD)",
                            "quality": "1080p",
                            "badge": "Melhor Qualidade",
                            "url": video_url,
                            "dtype": "direct",
                            "format_id": "best",
                            "is_audio": False
                        },
                        {
                            "label": "Apenas Áudio (MP3)",
                            "quality": "audio",
                            "badge": "Áudio",
                            "url": url.strip(),
                            "dtype": "ytdlp",
                            "format_id": "audio",
                            "is_audio": True
                        }
                    ]
                }
    except Exception as e:
        print("Erro Instagram scraper:", e)
    
    # Fallback yt-dlp para Instagram
    try:
        return extract_metadata_ytdlp(url, "Instagram")
    except Exception:
        return None

def extract_metadata_ytdlp(url: str, platform_name: str):
    """Extrai metadados e opções reais de qualidade usando yt-dlp."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title", f"{platform_name} Video")
        thumbnail = info.get("thumbnail", "")

        formats_list = info.get("formats", [])
        heights = set()
        for f in formats_list:
            h = f.get("height")
            if h and isinstance(h, int) and h > 0:
                heights.add(h)
        
        sorted_heights = sorted(list(heights), reverse=True)
        available_formats = []

        # 1. 1080p ou superior (4K, 1440p)
        top_h = next((h for h in sorted_heights if h >= 1080), None)
        if top_h:
            lbl = f"{top_h}p Ultra HD" if top_h > 1080 else "1080p Full HD"
            available_formats.append({
                "label": f"{lbl} (Qualidade Máxima)",
                "quality": str(top_h),
                "badge": "Melhor Qualidade",
                "url": url,
                "dtype": "ytdlp",
                "format_id": str(top_h),
                "is_audio": False
            })

        # 2. 720p HD
        has_720 = next((h for h in sorted_heights if 700 <= h < 1080), None)
        if has_720:
            available_formats.append({
                "label": "720p HD",
                "quality": "720",
                "badge": "Alta Resolução",
                "url": url,
                "dtype": "ytdlp",
                "format_id": "720",
                "is_audio": False
            })

        # 3. 480p SD ou menor
        has_sd = next((h for h in sorted_heights if h < 700), None)
        if has_sd:
            available_formats.append({
                "label": f"{has_sd}p SD",
                "quality": str(has_sd),
                "badge": "Econômico",
                "url": url,
                "dtype": "ytdlp",
                "format_id": str(has_sd),
                "is_audio": False
            })

        # Se não foram detectados formatos específicos de altura, fornece a melhor qualidade
        if not available_formats:
            available_formats.append({
                "label": "Qualidade Máxima Original (HD)",
                "quality": "best",
                "badge": "Melhor Qualidade",
                "url": url,
                "dtype": "ytdlp",
                "format_id": "best",
                "is_audio": False
            })

        # Opção de Áudio MP3
        available_formats.append({
            "label": "Apenas Áudio (MP3)",
            "quality": "audio",
            "badge": "Áudio",
            "url": url,
            "dtype": "ytdlp",
            "format_id": "audio",
            "is_audio": True
        })

        return {
            "title": title,
            "thumbnail": thumbnail,
            "platform": platform_name,
            "formats": available_formats
        }

@app.get("/manifest.json")
async def get_manifest():
    manifest_path = os.path.join(STATIC_DIR, "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="Manifest não encontrado")

@app.get("/sw.js")
async def get_sw():
    sw_path = os.path.join(STATIC_DIR, "sw.js")
    if os.path.exists(sw_path):
        headers = {"Service-Worker-Allowed": "/"}
        return FileResponse(sw_path, media_type="application/javascript", headers=headers)
    raise HTTPException(status_code=404, detail="Service Worker não encontrado")

@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "has_curl_cffi": cffi_requests is not None,
        "ffmpeg": FFMPEG_PATH is not None
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

    # 1. TikTok
    if platform == "tiktok":
        res, err_detail = extract_tiktok(url)
        if res:
            return {"success": True, **res}
        raise HTTPException(status_code=400, detail=f"Erro TikTok: {err_detail}")

    # 2. Twitter / X
    if platform == "twitter":
        res = extract_twitter(url)
        if res:
            return {"success": True, **res}
        raise HTTPException(status_code=400, detail="Não foi possível extrair o vídeo do Twitter/X. Verifique se o post contém um vídeo público.")

    # 3. Instagram
    if platform == "instagram":
        res = extract_instagram(url)
        if res:
            return {"success": True, **res}

    # 4. Pinterest, Reddit, Facebook, YouTube e outros links
    platform_names = {
        "tiktok": "TikTok",
        "pinterest": "Pinterest",
        "instagram": "Instagram",
        "twitter": "Twitter / X",
        "reddit": "Reddit",
        "facebook": "Facebook",
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
async def handle_download(
    url: str,
    dtype: str = "ytdlp",
    title: str = "video",
    format_id: str = "best",
    is_audio: bool = False,
    background_tasks: BackgroundTasks = None
):
    """Baixa o arquivo na qualidade escolhida com cabeçalhos de anexo para não abrir nova aba."""
    safe_title = re.sub(r'[^a-zA-Z0-9_\- ]', '', title).strip() or "video"
    is_mp3 = is_audio or (format_id == "audio")
    ext = "mp3" if is_mp3 else "mp4"
    filename = f"{safe_title}.{ext}"
    encoded_filename = urllib.parse.quote(filename)
    media_type = "audio/mpeg" if is_mp3 else "video/mp4"

    # Caso 1: Download direto (TikTok sem marca d'água, Twitter CDN, etc.)
    if dtype == "direct":
        def iter_content():
            req_func = cffi_requests.get if cffi_requests else requests.get
            with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"; filename*=UTF-8\'\'{encoded_filename}',
            "Content-Type": media_type,
        }
        return StreamingResponse(iter_content(), headers=headers)

    # Caso 2: Processamento via yt-dlp + ffmpeg com a resolução solicitada
    temp_id = uuid.uuid4().hex[:8]
    temp_out = os.path.join(TEMP_DIR, f"{temp_id}.%(ext)s")
    final_file = os.path.join(TEMP_DIR, f"{temp_id}.{ext}")

    ydl_opts = {
        "outtmpl": temp_out,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    if is_mp3:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"
        if format_id.isdigit():
            h = int(format_id)
            ydl_opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        elif format_id == "best":
            ydl_opts["format"] = "bestvideo*+bestaudio/best"
        else:
            ydl_opts["format"] = "bestvideo+bestaudio/best"

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

        background_tasks.add_task(cleanup_file, final_file)

        return FileResponse(
            path=final_file,
            filename=filename,
            media_type=media_type
        )
    except Exception as e:
        cleanup_file(final_file)
        raise HTTPException(status_code=500, detail=f"Erro ao processar o download do vídeo: {str(e)}")
