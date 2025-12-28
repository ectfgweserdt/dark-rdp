import os
import sys
import argparse
import time
import asyncio
import subprocess
import json
import base64
import re
import requests
from telethon import TelegramClient, errors
from telethon.sessions import StringSession 
from telethon.tl.types import MessageMediaDocument
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import googleapiclient.errors

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
GEMINI_MODEL = "gemini-2.5-flash-preview-09-2025"
# Changed to the preview model that usually works on free tiers
IMAGE_GEN_MODEL = "gemini-2.5-flash-image-preview"

# Fetching the API Key
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()

def run_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()
    return output.decode(), error.decode(), process.returncode

def download_progress_callback(current, total):
    print(f"⏳ Telegram Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)

def clean_fallback_title(filename):
    name = os.path.splitext(filename)[0]
    name = re.sub(r'(_|\.|\-)', ' ', name)
    tags = [
        r'\d{3,4}p', 'HD', 'NF', 'WEB-DL', 'Dual Audio', 'ES', 'x264', 'x265', 
        'HEVC', 'BluRay', 'HDRip', 'AAC', '5.1', '10bit'
    ]
    for tag in tags:
        name = re.sub(tag, '', name, flags=re.IGNORECASE)
    return ' '.join(name.split()).strip()

# --- AI METADATA ---
async def get_ai_metadata(filename):
    print(f"🤖 Calling Gemini AI for metadata: {filename}")
    
    if not GEMINI_API_KEY:
        print(f"⚠️ GEMINI_API_KEY is EMPTY!")
        title = clean_fallback_title(filename)
        return {"title": title, "description": f"Series: {title}", "image_prompt": title}
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    
    prompt = (
        f"You are a professional YouTube SEO manager. Analyze this file: '{filename}'\n"
        "1. CLEAN TITLE: Return ONLY the formal title (e.g., 'Love, Death & Robots - S04E09'). "
        "Remove ALL technical tags like WEB-DL, 720p, Dual Audio, etc.\n"
        "2. DESCRIPTION: Write 3 paragraphs of cinematic description. Use search tools for plot/cast. No links.\n"
        "3. IMAGE PROMPT: A descriptive artistic prompt for a cinematic poster (no text).\n"
        "Return ONLY JSON."
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    try:
        res = requests.post(url, json=payload, timeout=45)
        if res.status_code == 200:
            data = res.json()
            raw_text = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
            meta = json.loads(raw_text)
            if 'title' in meta:
                print(f"✅ AI metadata generated: {meta['title']}")
                return meta
    except Exception as e:
        print(f"⚠️ AI Metadata failed: {e}")
    
    title = clean_fallback_title(filename)
    return {"title": title, "description": f"Quality upload of {title}.", "image_prompt": title}

async def generate_thumbnail(image_prompt):
    if not GEMINI_API_KEY: return None
    print(f"🎨 Generating AI Thumbnail (Flash Image Mode)...")
    
    # Using the Flash Image Preview which is generally accessible to free tier users
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_GEN_MODEL}:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [{
            "parts": [{
                "text": f"Generate a high-quality cinematic movie poster, digital art, high detail, masterpiece, NO TEXT: {image_prompt}"
            }]
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE"]
        }
    }
    
    try:
        res = requests.post(url, json=payload, timeout=90)
        if res.status_code == 200:
            data = res.json()
            # Find the part that contains inlineData (the image)
            parts = data.get('candidates', [{}])[0].get('content', {}).get('parts', [])
            img_data = None
            for p in parts:
                if 'inlineData' in p:
                    img_data = p['inlineData']['data']
                    break
            
            if img_data:
                with open("thumbnail.png", "wb") as f:
                    f.write(base64.b64decode(img_data))
                print("✅ Thumbnail generated successfully via Flash Image.")
                return "thumbnail.png"
            else:
                print(f"⚠️ No image data found in response: {res.text[:200]}")
        else:
            print(f"⚠️ Image API Error ({res.status_code}): {res.text[:200]}")
                    
    except Exception as e:
        print(f"⚠️ Thumbnail generation failed: {e}")
    return None

# --- VIDEO PROCESSING ---
def process_video(input_path):
    output_path = "processed_video.mp4"
    print(f"\n🔍 Extracting English audio track...")
    cmd_ffmpeg = (
        f"ffmpeg -i '{input_path}' "
        f"-map 0:v:0 -map 0:a:m:language:eng? -map 0:a:0? "
        f"-c:v copy -c:a aac -b:a 192k -y '{output_path}'"
    )
    _, _, code = run_command(cmd_ffmpeg)
    return output_path if code == 0 and os.path.exists(output_path) else input_path

# --- YOUTUBE UPLOAD ---
def upload_to_youtube(video_path, metadata, thumb_path):
    try:
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
            'snippet': {
                'title': metadata.get('title', 'Video Upload')[:95],
                'description': metadata.get('description', 'High quality content.'),
                'categoryId': '24'
            },
            'status': {'privacyStatus': 'private'}
        }
        
        print(f"🚀 Uploading to YouTube: {body['snippet']['title']}")
        media = MediaFileUpload(video_path, chunksize=1024*1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status: print(f"Uploaded {int(status.progress() * 100)}%")

        video_id = response['id']
        
        if thumb_path and os.path.exists(thumb_path):
            print(f"🖼️ Applying thumbnail to video {video_id}...")
            # Wait for video to be indexed
            time.sleep(12)
            try:
                youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
                print("✅ Thumbnail applied!")
            except Exception as te:
                print(f"⚠️ Thumbnail application failed: {te}")
            
        print(f"🎉 SUCCESS! https://youtu.be/{video_id}")
        
    except Exception as e:
        print(f"🔴 YouTube Error: {e}")

# --- MAIN ---
async def run_flow(link):
    try:
        parts = [p for p in link.strip('/').split('/') if p]
        msg_id = int(parts[-1])
        c_idx = parts.index('c')
        chat_id = int(f"-100{parts[c_idx+1]}")
    except: return

    client = TelegramClient(StringSession(os.environ['TG_SESSION_STRING']), os.environ['TG_API_ID'], os.environ['TG_API_HASH'])
    await client.start()
    message = await client.get_messages(chat_id, ids=msg_id)
    if not message or not message.media: return

    raw_file = f"download_{msg_id}" + (message.file.ext if hasattr(message, 'file') else ".mp4")
    print(f"⬇️ Downloading...")
    await client.download_media(message, raw_file, progress_callback=download_progress_callback)
    await client.disconnect()

    # Get Metadata first to get the image prompt
    metadata = await get_ai_metadata(message.file.name or raw_file)
    
    # Run image generation and video processing
    thumb_task = generate_thumbnail(metadata.get('image_prompt', ''))
    final_video = process_video(raw_file)
    
    thumb = await thumb_task
    upload_to_youtube(final_video, metadata, thumb)

    # Final cleanup
    for f in [raw_file, "processed_video.mp4", "thumbnail.png"]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
