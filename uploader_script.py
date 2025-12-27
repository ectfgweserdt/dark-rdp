import os
import sys
import argparse
import time
import asyncio
import subprocess
import json
import base64
import requests
from telethon import TelegramClient, errors
from telethon.sessions import StringSession 
from telethon.tl.types import MessageMediaDocument
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
GEMINI_MODEL = "gemini-2.5-flash-preview-09-2025"
IMAGEN_MODEL = "imagen-4.0-generate-001"
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

# --- UTILS ---
def run_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()
    return output.decode(), error.decode(), process.returncode

def download_progress_callback(current, total):
    print(f"⏳ Telegram Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)

# --- AI METADATA ---
async def get_ai_metadata(filename):
    if not GEMINI_API_KEY:
        return {"title": filename, "description": "Auto-upload", "image_prompt": filename}
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    prompt = f"Analyze filename: '{filename}'. 1. Extract formal title. 2. Write 3-paragraph plot summary/cast using Search. 3. Image prompt for cinematic thumbnail. Return JSON: 'title', 'description', 'image_prompt'."
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    try:
        res = requests.post(url, json=payload)
        res.raise_for_status()
        data = res.json()
        text = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ AI Metadata failed: {e}")
        return {"title": filename, "description": "Auto-upload", "image_prompt": filename}

async def generate_thumbnail(image_prompt):
    if not GEMINI_API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGEN_MODEL}:predict?key={GEMINI_API_KEY}"
    payload = {"instances": [{"prompt": f"Cinematic movie poster, no text: {image_prompt}"}], "parameters": {"sampleCount": 1}}
    
    try:
        res = requests.post(url, json=payload)
        data = res.json()
        img_b64 = data.get('predictions', [{}])[0].get('bytesBase64Encoded')
        if img_b64:
            with open("thumbnail.png", "wb") as f:
                f.write(base64.b64decode(img_b64))
            return "thumbnail.png"
    except Exception as e:
        print(f"⚠️ Thumbnail failed: {e}")
    return None

# --- VIDEO PROCESSING ---
def process_video(input_path):
    output_path = "processed_video.mp4"
    print(f"\n🔍 Analyzing audio for {input_path}...")
    
    # Try to find English track
    cmd_probe = f"ffprobe -v error -select_streams a -show_entries stream=index:tags=language -of csv=p=0 '{input_path}'"
    out, _, _ = run_command(cmd_probe)
    
    target_stream = "0:a:0" # Default
    for line in out.splitlines():
        if 'eng' in line.lower():
            target_stream = f"0:a:{line.split(',')[0]}"
            print(f"✅ Found English track: {target_stream}")
            break

    print("✂️ Stripping other tracks...")
    cmd_ffmpeg = f"ffmpeg -i '{input_path}' -map 0:v:0 -map {target_stream} -c copy -y '{output_path}'"
    _, err, code = run_command(cmd_ffmpeg)
    
    if code == 0 and os.path.exists(output_path):
        return output_path
    print(f"⚠️ FFmpeg failed, using original: {err}")
    return input_path

# --- YOUTUBE UPLOAD ---
def upload_to_youtube(video_path, metadata, thumb_path):
    creds = Credentials(
        token=None,
        refresh_token=os.environ.get('YOUTUBE_REFRESH_TOKEN'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ.get('YOUTUBE_CLIENT_ID'),
        client_secret=os.environ.get('YOUTUBE_CLIENT_SECRET'),
        scopes=YOUTUBE_SCOPES
    )
    creds.refresh(Request())
    youtube = build('youtube', 'v3', credentials=creds)
    
    body = {
        'snippet': {'title': metadata['title'][:100], 'description': metadata['description'], 'categoryId': '24'},
        'status': {'privacyStatus': 'private'}
    }
    
    print(f"🚀 Uploading: {metadata['title']}")
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status: print(f"Uploaded {int(status.progress() * 100)}%")

    video_id = response['id']
    if thumb_path:
        print("🖼️ Applying thumbnail...")
        youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
    print(f"✅ Success: https://youtu.be/{video_id}")

# --- MAIN ---
async def run_flow(link):
    # Parse Link
    parts = [p for p in link.strip('/').split('/') if p]
    msg_id = int(parts[-1])
    chat_id = int(f"-100{parts[parts.index('c')+1]}")

    client = TelegramClient(StringSession(os.environ['TG_SESSION_STRING']), os.environ['TG_API_ID'], os.environ['TG_API_HASH'])
    await client.start()
    
    print(f"📡 Fetching msg {msg_id}...")
    message = await client.get_messages(chat_id, ids=msg_id)
    if not message or not message.media:
        print("🔴 No media found.")
        return

    raw_file = f"downloaded_{msg_id}.mp4"
    print("⬇️ Downloading...")
    await client.download_media(message, raw_file, progress_callback=download_progress_callback)
    await client.disconnect()

    # AI & Process
    metadata = await get_ai_metadata(message.file.name or raw_file)
    thumb = await generate_thumbnail(metadata['image_prompt'])
    final_video = process_video(raw_file)

    # Upload
    try:
        upload_to_youtube(final_video, metadata, thumb)
    finally:
        for f in [raw_file, "processed_video.mp4", "thumbnail.png"]:
            if os.path.exists(f): os.remove(f)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
