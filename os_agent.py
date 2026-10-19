import os
import subprocess
from pathlib import Path

COMMON_APPS = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "explorer": "explorer",
    "files": "explorer",
    "cmd": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "paint": "mspaint",
    "word": "winword",
    "microsoft word": "winword",
    "excel": "excel",
    "microsoft excel": "excel",
    "powerpoint": "powerpnt",
    "microsoft powerpoint": "powerpnt",
    "photoshop": "photoshop",
    "adobe photoshop": "photoshop",
    "spotify": "spotify",
    "vlc": "vlc",
    "vscode": "code",
    "visual studio code": "code",
    "code": "code",
    "settings": "ms-settings:"
}

def launch_app(app_name: str) -> str:
    """Attempts to launch a Windows application."""
    app_lower = app_name.lower().strip()
    
    # 1. Try known direct executables or URIs
    exe = COMMON_APPS.get(app_lower)
    if exe:
        try:
            os.startfile(exe)
            return f"Opening {app_name.title()}."
        except Exception as e:
            return f"Found {app_name.title()} but couldn't open it: {e}"

    # 2. Fallback: Search Start Menu shortcuts via PowerShell
    safe_name = app_name.replace('"', '').replace("'", '').replace(';', '').replace('&', '').replace('|', '').replace('`', '')
    ps_script = f"""
    $apps = Get-ChildItem -Path "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs", "$env:ALLUSERSPROFILE\\Microsoft\\Windows\\Start Menu\\Programs" -Recurse -Filter "*.lnk" -ErrorAction SilentlyContinue
    $match = $apps | Where-Object {{ $_.Name -match "{safe_name}" }} | Select-Object -First 1
    if ($match) {{
        Start-Process $match.FullName
        Write-Output "FOUND"
    }} else {{
        Write-Output "NOT_FOUND"
    }}
    """
    try:
        result = subprocess.run(["powershell", "-Command", ps_script], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if "FOUND" in result.stdout:
            return f"Opening {app_name.title()}."
    except Exception as e:
        print(f"[App Launcher Error] {e}")

    return f"I couldn't find an application named {app_name} on your computer."

def fuzzy_match(query: str, filename: str) -> bool:
    """Checks if all words in the query are present in the filename."""
    words = query.lower().replace(".", " ").split()
    fn = filename.lower()
    return all(w in fn for w in words)

def find_and_open_file(file_query: str) -> str:
    """Searches common directories for a file matching the query and opens it."""
    search_dirs = [
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "Downloads"
    ]
    
    max_depth = 4
    
    for sdir in search_dirs:
        if not sdir.exists():
            continue
            
        for root, dirs, files in os.walk(str(sdir)):
            # Enforce max depth to keep search lightning fast
            depth = len(Path(root).relative_to(sdir).parts)
            if depth >= max_depth:
                dirs.clear() # Stop descending
                
            for f in files:
                if fuzzy_match(file_query, f):
                    match_path = os.path.join(root, f)
                    try:
                        os.startfile(match_path)
                        return f"I found and opened {f}."
                    except Exception as e:
                        return f"I found {f}, but couldn't open it. {e}"
                        
    return f"I searched your Desktop, Documents, and Downloads, but couldn't find a file matching '{file_query}'."
