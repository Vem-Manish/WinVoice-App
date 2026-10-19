import pyautogui
import time
import webbrowser
import pyperclip
import subprocess
import os
import ctypes



def _confirm_dangerous(description: str) -> bool:
    """Show a thread-safe Windows MessageBox for dangerous actions."""
    result = ctypes.windll.user32.MessageBoxW(
        0,
        f"WinVoice wants to: {description}\n\nDo you want to proceed?",
        "WinVoice \u2014 Confirm Action",
        0x00000004 | 0x00000030  # MB_YESNO | MB_ICONWARNING
    )
    return result == 6  # IDYES


def execute_step(step):
    try:
        action = step.get("action")

        # ── WAIT ──────────────────────────────────────────────────────
        if action == "WAIT":
            print(f"   ⏳ Waiting {step.get('seconds', 1)}s...")
            time.sleep(step.get("seconds", 1))

        # ── OPEN_APP ──────────────────────────────────────────────────
        elif action == "OPEN_APP":
            app_name = step.get("app")
            if not app_name:
                print("   ❌ Error: AI forgot the app name.")
                return
            print(f"   📱 Opening {app_name}...")
            import os_agent
            res = os_agent.launch_app(app_name)
            if "couldn't find" in res:
                # Smart web fallback
                domains = {
                    "youtube": "youtube.com", 
                    "gmail": "mail.google.com", 
                    "spotify": "open.spotify.com",
                    "netflix": "netflix.com",
                    "twitter": "twitter.com",
                    "x": "x.com",
                    "facebook": "facebook.com",
                    "instagram": "instagram.com",
                    "whatsapp": "web.whatsapp.com",
                    "discord": "discord.com/app"
                }
                app_clean = app_name.lower().strip()
                domain = domains.get(app_clean)
                if not domain:
                    domain = f"www.{app_clean.replace(' ', '')}.com"
                url = f"https://{domain}"
                print(f"   🌐 App not found. Falling back to web: {url}")
                webbrowser.open(url)
                res = f"I opened {app_name} in your browser instead."
            print(f"   ✓ {res}")

        # ── FIND_FILE ─────────────────────────────────────────────────
        elif action == "FIND_FILE":
            file_name = step.get("file")
            if not file_name:
                print("   ❌ Error: AI forgot the file name.")
                return
            print(f"   🔍 Searching for {file_name}...")
            import os_agent
            res = os_agent.find_and_open_file(file_name)
            print(f"   ✓ {res}")

        # ── OPEN_URL ──────────────────────────────────────────────────
        elif action == "OPEN_URL":
            url = step.get("url")
            if url:
                print(f"   🌐 Opening: {url}")
                webbrowser.open(url)
                time.sleep(0.35)

        # ── YOUTUBE ───────────────────────────────────────────────────
        elif action in ("SEARCH_YOUTUBE", "YOUTUBE_TASK", "YOUTUBE"):
            query = step.get("query") or step.get("search") or ""
            import urllib.parse
            import re as _re
            if query:
                cleaned = _re.sub(r'\b(?:open|play|search|watch|find|show|listen to)\b', '', query, flags=_re.I)
                cleaned = _re.sub(r'\b(?:on|in|from|at)\s+youtube\b', '', cleaned, flags=_re.I).strip()
                cleaned = _re.sub(r'\byoutube\b', '', cleaned, flags=_re.I).strip()
                url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(cleaned or query)}"
            else:
                url = "https://www.youtube.com"
            print(f"   🌐 Opening YouTube: {url}")
            webbrowser.open(url)

        # ── TYPE ──────────────────────────────────────────────────────
        elif action == "TYPE":
            text = step.get("text", "")
            print(f"   📋 Pasting...")
            pyperclip.copy(text)
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "v")

        # ── PRESS ─────────────────────────────────────────────────────
        elif action == "PRESS":
            keys = step.get("keys") or [step.get("key")]
            keys = [k for k in keys if k is not None]
            if not keys:
                print("   ❌ PRESS step missing key names")
                return
            print(f"   🎹 Pressing: {keys}")
            if "win" in keys and "printscreen" in keys:
                pyautogui.keyDown("win")
                pyautogui.press("printscreen")
                time.sleep(0.1)
                pyautogui.keyUp("win")
            else:
                pyautogui.hotkey(*keys)

        # ── POWERSHELL ────────────────────────────────────────────────
        elif action == "POWERSHELL":
            command = step.get("command", "")
            print(f"   ⚡ PowerShell: {command}")

            # Dangerous commands require confirmation
            dangerous = {
                "shutdown /s": "Shut down the computer",
                "shutdown /r": "Restart the computer",
                "SetSuspendState": "Put the computer to sleep",
                "Stop-Process": None,  # Only confirm if it looks wrong
            }

            needs_confirm = None
            for key, description in dangerous.items():
                if key in command.lower() and description:
                    needs_confirm = description
                    break

            if needs_confirm:
                if not _confirm_dangerous(needs_confirm):
                    print(f"   ❌ User cancelled: {needs_confirm}")
                    return

            subprocess.Popen(
                ["powershell", "-Command", command],
                creationflags=subprocess.CREATE_NO_WINDOW
            )

        # ── NATIVE ────────────────────────────────────────────────────
        elif action == "NATIVE":
            command = (step.get("command") or "").upper()

            if command == "SHUTDOWN":
                print("   🔌 Shutting down PC...")
                subprocess.Popen(["shutdown", "/s", "/t", "3"], creationflags=subprocess.CREATE_NO_WINDOW)

            elif command == "RESTART":
                print("   🔄 Restarting PC...")
                subprocess.Popen(["shutdown", "/r", "/t", "3"], creationflags=subprocess.CREATE_NO_WINDOW)

            elif command == "SLEEP":
                print("   💤 Putting PC to sleep...")
                import threading
                import ctypes
                def _do_sleep():
                    time.sleep(2.0)
                    ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
                threading.Thread(target=_do_sleep, daemon=True).start()

            elif command == "SET_VOLUME":
                level = int(step.get("level", 50))
                level = max(0, min(100, level))
                print(f"   🔊 Setting volume to {level}%...")
                try:
                    from pycaw.pycaw import AudioUtilities
                    device = AudioUtilities.GetSpeakers()
                    volume = device.EndpointVolume
                    volume.SetMasterVolumeLevelScalar(level / 100.0, None)
                    print(f"   ✅ Volume set to {level}%")
                except Exception as e:
                    print(f"   ⚠️ Volume failed: {e}")

            elif command == "LOCK_PC":
                print("   🔒 Locking PC...")
                import ctypes
                ctypes.windll.user32.LockWorkStation()

            elif command in ("MONITOR_OFF", "SCREEN_OFF"):
                print("   🖥️ Turning off monitor...")
                import ctypes
                ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)

            elif command == "BRIGHTNESS":
                level = int(step.get("level", 50))
                level = max(0, min(100, level))
                print(f"   💡 Setting brightness to {level}%...")
                ps = f"""
            $monitor = Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods -ErrorAction SilentlyContinue
            if ($monitor -ne $null) {{
                $monitor.WmiSetBrightness(1, {level})
                Write-Host "Brightness set to {level}%"
            }} else {{
                Write-Host "Brightness not supported"
            }}
            """
                # Use run (blocking) not Popen — ensures WMI call completes
                result = subprocess.run(
                    ["powershell", "-Command", ps],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                print(f"   {result.stdout.strip()}")
                if result.stderr.strip():
                    print(f"   ⚠️ {result.stderr.strip()}")

            else:
                print(f"   ⚠️ Unknown NATIVE command: {command}")

        # ── DIRECT POWER ACTIONS ──────────────────────────────────────
        elif action in ("SHUTDOWN", "SLEEP", "RESTART"):
            cmd = action.upper()
            if cmd == "SHUTDOWN":
                print("   🔌 Shutting down PC...")
                subprocess.Popen(["shutdown", "/s", "/t", "3"], creationflags=subprocess.CREATE_NO_WINDOW)
            elif cmd == "RESTART":
                print("   🔄 Restarting PC...")
                subprocess.Popen(["shutdown", "/r", "/t", "3"], creationflags=subprocess.CREATE_NO_WINDOW)
            elif cmd == "SLEEP":
                print("   💤 Putting PC to sleep...")
                import threading
                import ctypes
                def _do_sleep():
                    time.sleep(2.0)
                    ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
                threading.Thread(target=_do_sleep, daemon=True).start()

    except Exception as e:
        print(f"   ⚠️ Step Failed: {e}")