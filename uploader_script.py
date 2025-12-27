import os
import sys
import argparse
import time
import asyncio
import subprocess
import json
import re
from telethon import TelegramClient, errors
from telethon.sessions import StringSession 
from telethon.tl.types import MessageMediaDocument
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import requests

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
GEMINI_MODEL = "gemini-2.5-flash-preview-09-2025"
IMAGEN_MODEL = "imagen-4.0-generate-001"

# --- HELPER: RUN SHELL COMMANDS (FFMPEG) ---
def run_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()
    return output.decode(), error.decode(), process.returncode

# --- AI: GENERATE METADATA & THUMBNAIL ---
async def get_ai_metadata(filename):
    """Uses Gemini to clean titles and scrape descriptions."""
    api_key = "" # System provides this at runtime
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    
    prompt = f"""
    Analyze the filename: "{filename}"
    1. Extract a formal title (e.g., 'Alice in Borderland - S01E01').
    2. Write a professional 3-paragraph description including plot summary and cast (use Google Search to verify details).
    3. Provide a high-quality prompt for an image generator to create a cinematic thumbnail for this content.
    Return ONLY JSON with keys: 'title', 'description', 'image_prompt'.
    """
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    try:
        res = requests.post(url, json=payload)
        data = res.json()
        text = data['candidates'][0]['content']['parts'][0]['text']
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ AI Metadata failed: {e}")
        return {"title": filename, "description": "Uploaded via Auto-Bot", "image_prompt": filename}

async def generate_thumbnail(image_prompt):
    """Generates a cinematic thumbnail using Imagen 4."""
    api_key = ""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGEN_MODEL}:predict?key={api_key}"
    
    payload = {
        "instances": {"prompt": f"Cinematic movie poster style, high quality, no text: {image_prompt}"},
        "parameters": {"sampleCount": 1}
    }
    
    try:
        res = requests.post(url, json=payload)
        img_data = res.json()['predictions'][0]['bytesBase64Encoded']
        path = "thumbnail.png"
        with open(path, "wb") as f:
            import base64
            f.write(base64.b64decode(img_data))
        return path
    except Exception as e:
        print(f"⚠️ Thumbnail generation failed: {e}")
        return None

# --- VIDEO: AUDIO TRACK FILTERING ---
def process_audio_tracks(input_path):
    """Keeps only English audio and the video track."""
    output_path = "processed_video.mp4"
    print("🔍 Analyzing audio tracks...")
    
    # Check for English audio stream
    cmd_probe = f"ffprobe -v error -select_streams a -show_entries stream=index:tags=language -of csv=p=0 '{input_path}'"
    out, _, _ = run_command(cmd_probe)
    
    target_stream = "0:a" # Default to first audio
    for line in out.splitlines():
        if 'eng' in line.lower():
            target_stream = f"0:a:{line.split(',')[0]}"
            print(f"✅ Found English track: {target_stream}")
            break

    print("✂️ Removing non-English audio tracks...")
    # Map video (0:v), selected audio (target_stream), and copy codecs to save time/CPU
    cmd_ffmpeg = f"ffmpeg -i '{input_path}' -map 0:v:0 -map {target_stream} -c copy -y '{output_path}'"
    _, err, code = run_command(cmd_ffmpeg)
    
    if code == 0:
        return output_path
    else:
        print(f"⚠️ FFmpeg error: {err}")
        return input_path

# --- YOUTUBE UPLOAD ---
def upload_to_youtube(creds_data, video_path, metadata, thumb_path):
    creds = Credentials(**creds_data)
    if creds.expired: creds.refresh(Request())
    
    youtube = build('youtube', 'v3', credentials=creds)
    
    body = {
        'snippet': {
            'title': metadata['title'][:100],
            'description': metadata['description'],
            'categoryId': '24' # Entertainment
        },
        'status': {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False}
    }
    
    print(f"🚀 Uploading to YouTube: {metadata['title']}")
    insert_request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True)
    )
    
    response = insert_request.execute()
    video_id = response['id']
    
    if thumb_path:
        print("🖼️ Setting AI-generated thumbnail...")
        youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
    
    print(f"✅ Success! https://youtu.be/{video_id}")

# --- MAIN LOGIC ---
async def main(link):
    # (Telegram setup logic same as previous versions...)
    TG_API_ID = os.environ.get('TG_API_ID')
    TG_API_HASH = os.environ.get('TG_API_HASH')
    TG_SESSION = os.environ.get('TG_SESSION_STRING')
    
    client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
    await client.start()
    
    # Parse link, Download, etc.
    # ... (Assuming download finishes as 'temp_video.mp4')
    
    # 1. Process Video
    final_video = process_audio_tracks("temp_video.mp4")
    
    # 2. AI Enhancement
    metadata = await get_ai_metadata("Alice.In.Borderland.S01E01.1080p.NF.WEB-DL.DDP5.1.x264.mp4")
    thumb = await generate_thumbnail(metadata['image_prompt'])
    
    # 3. YouTube Upload
    yt_creds = {
        "token": None,
        "refresh_token": os.environ.get('YOUTUBE_REFRESH_TOKEN'),
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": os.environ.get('YOUTUBE_CLIENT_ID'),
        "client_secret": os.environ.get('YOUTUBE_CLIENT_SECRET')
    }
    
    upload_to_youtube(yt_creds, final_video, metadata, thumb)

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
