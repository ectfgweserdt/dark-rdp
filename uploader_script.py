import os
import sys
import argparse
import time
import asyncio
from telethon import TelegramClient, errors
from telethon.sessions import StringSession 
from telethon.tl.types import MessageMediaDocument
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# --- CONFIGURATION ---
# YouTube Scopes required for video upload
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

# Use a distinct session name to avoid conflicts if needed
SESSION_NAME = 'tg_session_output'

# =================================================================
# 🛑 FOR LOCAL SESSION GENERATION ONLY 🛑
# These are only used if you run the script locally WITHOUT a link 
# to generate the session string. They are IGNORED in the GitHub Action flow.
LOCAL_TG_API_ID = 0
LOCAL_TG_API_HASH = ""
# =================================================================


# --- TELEGRAM LINK UTILITY ---
def parse_telegram_link(link):
    """
    Parses a t.me/c/CHAT_ID[/THREAD_ID]/MSG_ID link into parts, 
    correctly handling the optional thread ID.
    """
    try:
        # Split the URL path by '/' and clean up all empty strings
        # parts will contain: [..., 'c', 'CHAT_ID', 'MSG_ID'] or [..., 'c', 'CHAT_ID', 'THREAD_ID', 'MSG_ID']
        parts = [p for p in link.strip('/').split('/') if p]

        # Find the index of 'c' (should be the indicator for canonical supergroup link)
        try:
            # We look for 'c' to determine the start of the ID sequence
            c_index = parts.index('c')
        except ValueError:
            raise ValueError("Link must contain '/c/' indicating a canonical channel link (e.g., https://t.me/c/ID/MSG).")

        # The message ID is always the last element
        message_id = int(parts[-1])

        # The base channel ID is always the part immediately after 'c'
        # The thread ID (if present) is ignored for message retrieval by Telethon
        if len(parts) <= c_index + 1:
            raise ValueError("Link format is incomplete. Missing CHAT_ID.")
            
        base_channel_id = int(parts[c_index + 1])
        
        # Apply the Telethon fix for supergroup channel IDs found in canonical links (t.me/c/...)
        # Supergroup IDs need to be formatted as -100xxxxxxxxxx
        channel_id = int(f'-100{base_channel_id}')

        return channel_id, message_id
    except Exception as e:
        # Catch and print the specific error, then exit
        print(f"🔴 Error parsing link: {e}")
        sys.exit(1)

# --- YOUTUBE AUTHENTICATION (FOR GITHUB WORKFLOW) ---
def get_youtube_service(client_id, client_secret, refresh_token):
    """Authenticates using a stored refresh token for non-interactive use."""
    print("Authenticating with YouTube using Refresh Token...")
    try:
        # Create a mock credentials object using the refresh token
        creds = Credentials(
            token=None,  # No immediate access token needed, it will be refreshed
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
            scopes=YOUTUBE_SCOPES
        )
        # Attempt to refresh the token to get a valid service
        creds.refresh(Request())
        
        # Build the YouTube service client
        youtube = build('youtube', 'v3', credentials=creds)
        print("YouTube Authentication successful.")
        return youtube
    except Exception as e:
        print(f"🔴 YouTube Authentication Error. Check CLIENT_ID, CLIENT_SECRET, and REFRESH_TOKEN: {e}")
        sys.exit(1)

# --- YOUTUBE UPLOAD ---
def upload_video(youtube, filepath, title, description):
    """Uploads the video file and sets its privacy status to private."""
    print(f"Starting upload for: {title}")
    
    body = dict(
        snippet=dict(
            title=title,
            description=description,
            tags=["educational", "telegram_export"],
            categoryId="27" # Category 27 is "Education"
        ),
        status=dict(
            privacyStatus='private' # This is the crucial step to make it private
        )
    )

    media = MediaFileUpload(filepath, chunksize=-1, resumable=True)
    
    # Insert request (resumable upload handled by the client library)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    error = None
    retry = 0
    MAX_RETRIES = 5
    
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                # YouTube upload percentage report
                print(f"Uploaded {int(status.progress() * 100)}%")
            
            if response is not None:
                if 'id' in response:
                    print(f"✅ Video Upload Complete! YouTube ID: {response['id']}")
                    print(f"Link: https://www.youtube.com/watch?v={response['id']}")
                    return response['id']
                else:
                    raise Exception(f"Upload failed with unexpected response: {response}")

        except Exception as e:
            error = e
            retry += 1
            if retry > MAX_RETRIES:
                print(f"🔴 Fatal Error: Maximum retries reached. Upload failed. {error}")
                break
            
            # Simple exponential backoff
            sleep_time = 2 ** retry
            print(f"Retriable error occurred: {error}. Retrying in {sleep_time} seconds...")
            time.sleep(sleep_time)
            
    return None

# --- DOWNLOAD PROGRESS REPORTER ---
def download_progress_callback(current, total):
    """Prints the Telegram download progress in MB and percentage."""
    # Print the progress on the same line using \r
    print(f"⏳ Telegram Download Progress: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)


# --- TELEGRAM DOWNLOAD ---
async def download_video_and_upload(link):
    """Main asynchronous function to handle the Telegram download and YouTube upload."""
    
    # 1. Get secrets from environment variables (set by GitHub Actions)
    TG_API_ID = os.environ.get('TG_API_ID')
    TG_API_HASH = os.environ.get('TG_API_HASH')
    TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')
    
    YT_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
    YT_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
    YT_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

    # 2. Check for ALL necessary secrets.
    required_secrets = [TG_API_ID, TG_API_HASH, TG_SESSION_STRING, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]
    if not all(required_secrets):
        print("🔴 Missing one or more required secrets. Cannot proceed with upload.")
        print("Please ensure all six required secrets are set in GitHub Actions.")
        sys.exit(1)

    # 3. Parse the input link
    channel_id, message_id = parse_telegram_link(link)
    print(f"Targeting channel ID: {channel_id}, Message ID: {message_id}")
    
    client = None
    downloaded_filepath = None
    try:
        # 4. Connect to Telegram
        print("Connecting to Telegram...")
        
        # Use StringSession to prevent local file system errors
        session = StringSession(TG_SESSION_STRING)
        client = TelegramClient(session, TG_API_ID, TG_API_HASH)
        
        await client.start()
        print("Connection successful.")

        # 5. Get the message
        print(f"Fetching message {message_id} from chat {channel_id}...")
        message = await client.get_messages(channel_id, ids=message_id)

        # Check for message existence and media type
        if not message or not (message.media and isinstance(message.media, MessageMediaDocument)):
            print("🔴 Error: Message is missing or does not contain a supported media file (video/document).")
            # CRITICAL CHECK: If the message is retrieved but has no media, it might mean the session lacks access.
            if message and message.media is None:
                print("Note: The message was found, but it contains no media. Ensure the session has access to the video.")
            return

        # 6. Download the file
        file_name = f"video_{channel_id}_{message_id}.mp4"
        print(f"Starting download of media to {file_name}...")
        
        # Pass the progress callback to show live transfer status
        downloaded_filepath = await client.download_media(
            message, 
            file_name, 
            progress_callback=download_progress_callback
        )
        
        # Print a clean line after the download is finished (overwriting the last progress update)
        print("                                                                                            ", end='\r')
        print(f"✅ Download complete: {downloaded_filepath}")
        
        # Determine Title and Description
        title = os.path.basename(downloaded_filepath)
        description = message.message if message.message else f"Exported video from Telegram message {link}"
        
        # 7. YouTube Authentication and Upload
        youtube_service = get_youtube_service(YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN)
        upload_video(youtube_service, downloaded_filepath, title, description)

    except Exception as e:
        print(f"\n🔴 An unexpected error occurred during the process: {e}")
    
    finally:
        # 8. Cleanup
        if client:
            await client.disconnect()
        if downloaded_filepath and os.path.exists(downloaded_filepath):
            print(f"Cleaning up local file: {downloaded_filepath}")
            os.remove(downloaded_filepath)

# --- LOCAL SESSION GENERATION (Run this once locally) ---
async def generate_telegram_session(api_id, api_hash):
    """
    Runs locally to generate the TG_SESSION_STRING for use in GitHub secrets.
    """
    if not api_id or not api_hash:
        print("TG_API_ID and TG_API_HASH must be provided to generate a session string.")
        return

    # Use the specific session name defined globally
    client = TelegramClient(SESSION_NAME, api_id, api_hash)
    
    print("\n--- ATTENTION ---")
    print("You must run this command in a LOCAL, INTERACTIVE terminal (or clean cloud environment).")
    print("The script is about to prompt you for your phone number or bot token.")
    print("-----------------\n")

    session_string = None
    
    try:
        # This is where the interactive prompts for phone, code, and 2FA password happen
        await client.start()
        session_string = client.session.save()
        
    except errors.SessionPasswordNeededError:
        print("🔴 Login Failed: Two-factor authentication (2FA) is required. The script should have prompted you for a password.")
        print("Please ensure you enter your password when prompted or disable 2FA for this generation step.")
        return
    except Exception as e:
        print(f"🔴 Login Failed! Telethon Error: {e}")
        print("Please check your phone number, login code (and password, if applicable) were entered correctly.")
        return
    finally:
        await client.disconnect()
        session_filepath = f'{SESSION_NAME}.session'
        if os.path.exists(session_filepath):
            try:
                os.remove(session_filepath)
                print(f"(Cleaned up local file: {session_filepath})")
            except Exception:
                print(f"⚠️ Warning: Could not delete session file '{session_filepath}'.")

    if session_string:
        print("\n-------------------------------------------------------------")
        print("      🔑 TELEGRAM SESSION STRING GENERATED 🔑")
        print("-------------------------------------------------------------")
        print("\n✅ SUCCESS! COPY THIS ENTIRE STRING AND SAVE IT AS 'TG_SESSION_STRING' IN GITHUB SECRETS:")
        print(session_string)
        print("\n-------------------------------------------------------------")
    else:
        print("🔴 FINAL ERROR: Session string is missing after successful login.")
    

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Automated Telegram video downloader and YouTube uploader.')
    parser.add_argument('telegram_link', nargs='?', help='The full URL of the Telegram message/video (e.g., https://t.me/c/ID/MSG_ID).')
    args = parser.parse_args()

    # If the link is missing, we assume the user is trying to generate the session string locally
    if not args.telegram_link:
        print("No Telegram link provided. Checking for secrets to initiate session generation...")
        
        # Use hardcoded fallbacks only for local session generation if env vars are missing
        local_api_id = os.environ.get('TG_API_ID') or str(LOCAL_TG_API_ID)
        local_api_hash = os.environ.get('TG_API_HASH') or LOCAL_TG_API_HASH

        if local_api_id and local_api_hash and local_api_id != '0' and local_api_hash != '':
            print("Found Telegram API credentials. Starting interactive login...")
            asyncio.run(generate_telegram_session(local_api_id, local_api_hash))
            print("Session generation finished. You must copy the string above and set it as a GitHub Secret.")
            print("\nNext, run the script again with the telegram link argument from GitHub Actions.")
        else:
            print("🔴 ERROR: To generate the session string locally, you must provide TG_API_ID and TG_API_HASH in the script variables or environment.")
        
    else:
        # If the link is provided, run the full process asynchronously (Upload Mode)
        asyncio.run(download_video_and_upload(args.telegram_link))
