import os
import sys
import argparse
import time
import asyncio
from telethon import TelegramClient, errors
from telethon.sessions import StringSession 
from telethon.tl.types import MessageMediaDocument
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
SESSION_NAME = 'tg_session_output'

# --- TELEGRAM LINK UTILITY ---
def parse_telegram_link(link):
    try:
        parts = [p for p in link.strip('/').split('/') if p]
        try:
            c_index = parts.index('c')
        except ValueError:
            print(f"⚠️ Skipping invalid link format: {link}")
            return None, None

        message_id = int(parts[-1])
        base_channel_id = int(parts[c_index + 1])
        channel_id = int(f'-100{base_channel_id}')
        return channel_id, message_id
    except Exception as e:
        print(f"🔴 Error parsing link {link}: {e}")
        return None, None

# --- YOUTUBE AUTHENTICATION ---
def get_youtube_service(client_id, client_secret, refresh_token):
    print("Authenticating with YouTube...")
    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
            scopes=YOUTUBE_SCOPES
        )
        creds.refresh(Request())
        return build('youtube', 'v3', credentials=creds)
    except Exception as e:
        print(f"🔴 YouTube Authentication Error: {e}")
        sys.exit(1)

# --- YOUTUBE UPLOAD ---
def upload_video(youtube, filepath, title, description):
    print(f"🚀 Starting YouTube upload: {title}")
    body = dict(
        snippet=dict(title=title, description=description, categoryId="27"),
        status=dict(privacyStatus='private')
    )
    media = MediaFileUpload(filepath, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"   > YouTube Upload: {int(status.progress() * 100)}%")
            if response:
                print(f"✅ Uploaded: https://www.youtube.com/watch?v={response['id']}")
                return response['id']
        except Exception as e:
            retry += 1
            if retry > 5: break
            time.sleep(2 ** retry)
    return None

# --- PROGRESS CALLBACK ---
def download_progress_callback(current, total):
    print(f"⏳ TG Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)

# --- BATCH PROCESSING ---
async def process_batch(links_string):
    links = [l.strip() for l in links_string.split(',') if l.strip()]
    
    TG_API_ID = os.environ.get('TG_API_ID')
    TG_API_HASH = os.environ.get('TG_API_HASH')
    TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')
    YT_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
    YT_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
    YT_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

    if not all([TG_API_ID, TG_API_HASH, TG_SESSION_STRING, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]):
        print("🔴 Missing secrets. Check your GitHub Secrets settings.")
        return

    youtube_service = get_youtube_service(YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN)
    session = StringSession(TG_SESSION_STRING)
    
    async with TelegramClient(session, TG_API_ID, TG_API_HASH) as client:
        print(f"✅ Connected to Telegram. Total links: {len(links)}")

        for index, link in enumerate(links):
            print(f"\n--- Item {index + 1}/{len(links)} ---")
            channel_id, message_id = parse_telegram_link(link)
            
            if not channel_id: continue

            downloaded_filepath = None
            try:
                message = await client.get_messages(channel_id, ids=message_id)
                if not message or not (message.media and isinstance(message.media, MessageMediaDocument)):
                    print(f"❌ No video found at {link}")
                    continue

                file_name = f"video_{channel_id}_{message_id}.mp4"
                downloaded_filepath = await client.download_media(
                    message, file_name, progress_callback=download_progress_callback
                )
                print(f"\n✅ TG Download Finished.")

                title = f"Video Part {index+1} - {message_id}"
                description = message.message if message.message else f"Exported from {link}"
                upload_video(youtube_service, downloaded_filepath, title, description)

            except Exception as e:
                print(f"🔴 Error on {link}: {e}")
            finally:
                if downloaded_filepath and os.path.exists(downloaded_filepath):
                    os.remove(downloaded_filepath)
            
            if index < len(links) - 1:
                await asyncio.sleep(5) # Cooldown to protect account

# --- SESSION GENERATOR (LOCAL ONLY) ---
async def generate_telegram_session(api_id, api_hash):
    client = TelegramClient(SESSION_NAME, api_id, api_hash)
    await client.start()
    print(f"\nYour Session String:\n{client.session.save()}")
    await client.disconnect()

# --- MAIN ---
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('telegram_link', nargs='?')
    args = parser.parse_args()

    if not args.telegram_link:
        # Use existing local variables if set
        local_api_id = os.environ.get('TG_API_ID')
        local_api_hash = os.environ.get('TG_API_HASH')
        if local_api_id and local_api_hash:
            asyncio.run(generate_telegram_session(local_api_id, local_api_hash))
    else:
        asyncio.run(process_batch(args.telegram_link))
