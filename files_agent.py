# files_agent.py
# High-speed local file finding and cross-agent sharing agent for WinVoice.

import os
import glob
import re
import time
import subprocess
from pathlib import Path

# Supported search roots on Windows
DEFAULT_SEARCH_DIRS = [
    Path.home() / "Downloads",
    Path.home() / "Documents",
    Path.home() / "Desktop",
    Path.home() / "Pictures",
    Path.home() / "Videos",
    Path(os.path.dirname(os.path.abspath(__file__)))  # current workspace
]

# Add OneDrive if available
_onedrive = os.environ.get("OneDrive")
if _onedrive and os.path.exists(_onedrive):
    DEFAULT_SEARCH_DIRS.append(Path(_onedrive))

COMMON_EXTENSIONS = [
    "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "csv", "txt",
    "png", "jpg", "jpeg", "webp", "gif", "mp4", "mp3", "zip", "json", "py"
]

# File extensions that should NEVER be matched unless explicitly requested
EXCLUDE_EXTENSIONS = {
    "pak", "dll", "so", "exe", "sys", "bin", "dat", "tmp", "mo", "qm",
    "o", "a", "class", "pyc", "pyo", "pyd", "node", "pdb", "msi", "idx",
    "manifest", "loc"
}


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


class FilesAgent:
    """
    Intelligent local file finder and management agent.
    Searches recent files and standard user directories with fuzzy scoring,
    and formats output for downstream consumption by Gmail and WhatsApp agents.
    """

    def __init__(self):
        pass

    def _clean_query(self, query: str) -> tuple[str, str | None]:
        """
        Cleans the user query and extracts target filename and optional target extension.
        Example: 'find the file report.pdf' -> ('report.pdf', 'pdf')
                 'find the test1.txt file' -> ('test1.txt', 'txt')
                 'search for my quarterly budget spreadsheet' -> ('quarterly budget spreadsheet', 'xlsx')
        """
        q = (query or "").strip()
        # 0. Deduplicate repeated consecutive phrases (e.g. STT echo)
        q = re.sub(r'\b(.{4,60}?)\s+\1\b', r'\1', q, flags=re.IGNORECASE).strip()

        # 1. Remove common command prefixes
        prefixes = [
            r'^(find|search for|locate|get|where is|open|look for|reveal)\s+(the\s+file\s+|the\s+|a\s+|my\s+file\s+|my\s+)?',
            r'^(file\s+named\s+|file\s+called\s+|file\s+)',
        ]
        for p in prefixes:
            q = re.sub(p, '', q, flags=re.IGNORECASE).strip()

        # 2. Remove trailing command words (e.g. 'test1.txt file' -> 'test1.txt')
        q = re.sub(r'\s+(the\s+file|file|document|doc|folder)\b\.?$', '', q, flags=re.IGNORECASE).strip()
        q = q.rstrip(".?!").strip()

        # 3. Check for explicit extension
        ext = None
        ext_match = re.search(r'\.([a-zA-Z0-9]{1,5})$', q)
        if ext_match:
            ext = ext_match.group(1).lower()
        else:
            # Check for keyword hints (e.g. 'spreadsheet', 'document', 'image', 'photo')
            if any(w in q.lower() for w in ("spreadsheet", "excel", "sheet")):
                ext = "xlsx"
            elif any(w in q.lower() for w in ("document", "word", "doc")):
                ext = "docx"
            elif any(w in q.lower() for w in ("pdf",)):
                ext = "pdf"
            elif any(w in q.lower() for w in ("image", "picture", "photo")):
                ext = "png"
            elif any(w in q.lower() for w in ("text", "txt")):
                ext = "txt"

        return q, ext

    def _scan_recent_files(self, target_tokens: list[str], target_ext: str | None, raw_target_no_ext: str = "") -> list[tuple[float, str]]:
        """
        Scans Windows Recent files (%APPDATA%\\Microsoft\\Windows\\Recent).
        Fastest path for files the user recently opened, edited, or downloaded.
        """
        results = []
        recent_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Recent")
        if not os.path.exists(recent_dir):
            return results

        try:
            import winshell
        except ImportError:
            winshell = None

        lnk_files = glob.glob(os.path.join(recent_dir, "*.lnk"))
        lnk_files.sort(key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0, reverse=True)

        for lnk in lnk_files[:80]:
            try:
                target_path = None
                if winshell:
                    target_path = winshell.shortcut(lnk).path
                if not target_path or not os.path.isfile(target_path):
                    continue

                score = self._score_candidate(target_path, target_tokens, target_ext, raw_target_no_ext)
                if score >= 40:
                    results.append((score + 10, target_path))  # Recent file bonus
            except Exception:
                continue

        return results

    def _score_candidate(self, file_path: str, target_tokens: list[str], target_ext: str | None, raw_target_no_ext: str = "") -> float:
        """Computes a match score from 0 to 100 for a candidate file path."""
        fn = os.path.basename(file_path).lower()
        fn_no_ext, ext = os.path.splitext(fn)
        ext = ext.lstrip(".").lower()

        # Skip temporary, hidden, or system files
        if fn.startswith("~$") or fn.startswith("."):
            return 0.0

        # CRITICAL GUARD 1: Strict extension enforcement
        if target_ext:
            if ext != target_ext.lower():
                return 0.0
        else:
            # If target extension wasn't specified, reject internal application/system binary assets
            if ext in EXCLUDE_EXTENSIONS:
                return 0.0

        target_str = " ".join(target_tokens).lower()
        raw_clean = raw_target_no_ext.lower().strip() if raw_target_no_ext else target_str

        # Squashed versions (alphanumeric only, no spaces or punctuation)
        fn_squashed = re.sub(r'[^a-zA-Z0-9]', '', fn_no_ext)
        target_squashed = re.sub(r'[^a-zA-Z0-9]', '', raw_clean)

        # Phonetic / STT normalization: 'text' <-> 'test'
        # Google STT very frequently transcribes 'test1.txt' as 'text 1.txt' or 'text1.txt'
        target_phonetics = {target_squashed}
        if "text" in target_squashed:
            target_phonetics.add(target_squashed.replace("text", "test"))
        if "test" in target_squashed:
            target_phonetics.add(target_squashed.replace("test", "text"))

        score = 0.0

        # 1. Exact match or phonetic exact match
        if fn == target_str or fn_no_ext == raw_clean or fn_squashed == target_squashed:
            score += 90.0
        elif fn_squashed in target_phonetics:
            # STT heard 'text1' but file is 'test1' -> 88.0 points
            score += 88.0
        # 2. Strong containment with strict length validation
        elif fn_squashed and target_squashed:
            # Candidate contains target (e.g. 'project_report_final' contains 'report')
            if target_squashed in fn_squashed and len(target_squashed) >= 3:
                score += 75.0
            # Target contains candidate ONLY IF candidate is substantial (>= 4 chars and >= 75% length)
            # This completely prevents 2-letter files like 'te.pak' from matching 'text1'!
            elif fn_squashed in target_squashed and len(fn_squashed) >= 4 and (len(fn_squashed) / len(target_squashed)) >= 0.75:
                score += 65.0
            else:
                # 3. Token-based matching
                matches = sum(1 for tok in target_tokens if tok in fn)
                if target_tokens:
                    ratio = matches / len(target_tokens)
                    score += ratio * 50.0
        else:
            matches = sum(1 for tok in target_tokens if tok in fn)
            if target_tokens:
                ratio = matches / len(target_tokens)
                score += ratio * 50.0

        # Extension match bonus
        if target_ext and ext == target_ext.lower():
            score += 10.0

        # Recency bonus (within last 7 days)
        try:
            mtime = os.path.getmtime(file_path)
            age_days = (time.time() - mtime) / 86400.0
            if age_days < 1:
                score += 10.0
            elif age_days < 7:
                score += 5.0
        except Exception:
            pass

        return min(score, 100.0)

    def search_file(self, query: str, max_depth: int = 3) -> dict | None:
        """
        Searches recent items and core user directories for the best matching file.
        Returns a structured dictionary or None.
        """
        clean_name, target_ext = self._clean_query(query)
        raw_no_ext = os.path.splitext(clean_name)[0] if target_ext else clean_name
        target_tokens = [w.lower() for w in re.split(r'[\s_\-\.]+', clean_name) if len(w) > 1]
        if not target_tokens and clean_name:
            target_tokens = [clean_name.lower()]

        if not target_tokens:
            return None

        candidates: list[tuple[float, str]] = []
        seen_paths = set()

        # 1. Direct path check (instant 0.001s lookup across roots with variations)
        clean_squashed = re.sub(r'[\s_\-]+', '', clean_name)
        variations = [clean_name]
        if clean_squashed != clean_name:
            variations.append(clean_squashed)

        # Phonetic variants for direct lookup ('text' <-> 'test')
        if "text" in clean_name.lower():
            variations.append(re.sub(r'\btext\b|text', 'test', clean_name, flags=re.I))
            variations.append(re.sub(r'\btext\b|text', 'test', clean_squashed, flags=re.I))
        elif "test" in clean_name.lower():
            variations.append(re.sub(r'\btest\b|test', 'text', clean_name, flags=re.I))
            variations.append(re.sub(r'\btest\b|test', 'text', clean_squashed, flags=re.I))

        # Direct check of root directories
        for sdir in DEFAULT_SEARCH_DIRS:
            if not sdir.exists():
                continue
            for var in variations:
                direct = sdir / var
                if direct.is_file():
                    return self._build_result(str(direct))
                
                # If query didn't specify an extension, try standard extensions directly
                if not target_ext:
                    for ce in COMMON_EXTENSIONS:
                        cand_ce = sdir / f"{var}.{ce}"
                        if cand_ce.is_file():
                            return self._build_result(str(cand_ce))

        # 2. Check Recent Files (fast path ~5-15ms)
        recent_hits = self._scan_recent_files(target_tokens, target_ext, raw_no_ext)
        for score, p in recent_hits:
            if p not in seen_paths:
                seen_paths.add(p)
                candidates.append((score, p))

        # If high-confidence hit in recent items (score >= 85), return early
        if candidates and candidates[0][0] >= 85:
            best_score, best_path = candidates[0]
            return self._build_result(best_path)

        # 3. Check Top-Level Files in Standard User Directories FIRST before subfolders
        for sdir in DEFAULT_SEARCH_DIRS:
            if not sdir.exists():
                continue
            try:
                for entry in os.scandir(str(sdir)):
                    if entry.is_file():
                        if entry.path in seen_paths:
                            continue
                        score = self._score_candidate(entry.path, target_tokens, target_ext, raw_no_ext)
                        if score >= 85:
                            return self._build_result(entry.path)
                        elif score >= 40:
                            seen_paths.add(entry.path)
                            candidates.append((score, entry.path))
            except Exception:
                continue

        # 4. Search Subdirectories with robust pruning & depth limit
        for sdir in DEFAULT_SEARCH_DIRS:
            if not sdir.exists():
                continue

            base_parts_len = len(sdir.parts)
            for root, dirs, files in os.walk(str(sdir)):
                # Prune common heavy/irrelevant/system/IDE folders
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith((".", "node_modules", "AppData", "__pycache__", "venv", ".git", "$", "Program"))
                    and d.lower() not in {"locales", "resources", "site-packages", "dist", "build", "bin", "lib", "obj", "packages", "cache"}
                ]

                # Enforce max depth
                if len(Path(root).parts) - base_parts_len >= max_depth:
                    dirs.clear()

                for f in files:
                    full_p = os.path.join(root, f)
                    if full_p in seen_paths:
                        continue

                    score = self._score_candidate(full_p, target_tokens, target_ext, raw_no_ext)
                    if score >= 85:
                        return self._build_result(full_p)
                    elif score >= 40:
                        seen_paths.add(full_p)
                        candidates.append((score, full_p))

        if candidates:
            # Sort candidates by score descending
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_score, best_path = candidates[0]
            return self._build_result(best_path)

        # 5. Ultra-fast Windows PowerShell Search Fallback across user directories
        try:
            pattern = f"*{re.sub(r'[^a-zA-Z0-9]', '*', raw_no_ext)}*"
            cmd = f'powershell -NoProfile -Command "Get-ChildItem -Path \'$HOME\\Downloads\', \'$HOME\\Documents\', \'$HOME\\Desktop\' -Filter \'{pattern}\' -File -Recurse -Depth 3 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName -First 1"'
            out = subprocess.check_output(cmd, shell=True, timeout=3).decode("utf-8", errors="ignore").strip()
            if out and os.path.isfile(out):
                return self._build_result(out)
        except Exception:
            pass

        return None

    def _build_result(self, file_path: str) -> dict:
        """Builds standardized file metadata dictionary."""
        abs_path = os.path.abspath(file_path)
        stat = os.stat(abs_path)
        return {
            "status": "success",
            "file_path": abs_path,
            "file_name": os.path.basename(abs_path),
            "size_bytes": stat.st_size,
            "size_str": format_size(stat.st_size),
            "extension": os.path.splitext(abs_path)[1].lstrip(".").lower(),
            "modified": stat.st_mtime
        }

    def run(self, query: str, status_callback=None) -> str:
        """
        Agent execution entry point for WinVoice.
        """
        q_lower = query.lower().strip()

        if status_callback:
            status_callback("Scanning local files...")

        # Determine requested action (find vs open vs reveal)
        should_open = any(w in q_lower for w in ("open", "launch", "start"))
        should_reveal = any(w in q_lower for w in ("reveal", "show in folder", "show in explorer"))

        file_info = self.search_file(query)
        if not file_info:
            clean_name, _ = self._clean_query(query)
            return f"❌ Could not find any local file matching '{clean_name}'."

        path = file_info["file_path"]
        name = file_info["file_name"]
        size_str = file_info["size_str"]

        if should_open:
            try:
                os.startfile(path)
                return (
                    f"✅ Found and opened **{name}** ({size_str})\n"
                    f"[ATTACHMENT_FILE]: {path}\n"
                    f"[FILE_NAME]: {name}\n"
                    f"[FILE_SIZE]: {size_str}"
                )
            except Exception as e:
                return (
                    f"⚠️ Found **{name}**, but could not open it: {e}\n"
                    f"[ATTACHMENT_FILE]: {path}\n"
                    f"[FILE_NAME]: {name}"
                )

        if should_reveal:
            try:
                subprocess.Popen(f'explorer /select,"{path}"')
                return (
                    f"✅ Located **{name}** in File Explorer.\n"
                    f"[ATTACHMENT_FILE]: {path}\n"
                    f"[FILE_NAME]: {name}\n"
                    f"[FILE_SIZE]: {size_str}"
                )
            except Exception:
                pass

        # Standard file find response formatted for downstream agent ingestion
        return (
            f"📁 Located file: **{name}** ({size_str})\n"
            f"Path: `{path}`\n\n"
            f"[ATTACHMENT_FILE]: {path}\n"
            f"[FILE_NAME]: {name}\n"
            f"[FILE_SIZE]: {size_str}"
        )
