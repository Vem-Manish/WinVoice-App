import json
import os
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()


def get_session():
    """Loads the user session. Ensures login has happened."""
    if not os.path.exists("session.json"):
        raise FileNotFoundError("Please login via the WinVoice GUI first.")
    with open("session.json", "r") as f:
        return json.load(f)


def get_google_service(api_name, version):
    """
    Returns an authorized Google API service.
    Automatically refreshes the token if it has expired.
    """
    session = get_session()
    tokens = session.get("tokens", {})

    # 1. Reconstruct Credentials with refresh capability
    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),  # Ensure this is in your .env
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        scopes=session.get("scope", "").split(" ")
    )

    # 2. Auto-Refresh if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Optional: Save the new token back to session.json here if desired
        except Exception as e:
            print(f"Error refreshing token: {e}")
            raise

    return build(api_name, version, credentials=creds)


def get_ai_client():
    """Returns Ollama raw caller instead of Gemini."""
    from ai_backend import call_ollama_raw
    return call_ollama_raw