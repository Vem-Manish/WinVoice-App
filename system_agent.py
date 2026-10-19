# system_agent.py
# Requires: pip install psutil
import json
import os
from ai_backend import _call_ollama_chat

SYSTEM_PROMPT = """You are an intelligent System Info assistant. Parse ANY system/hardware request and output ONLY valid JSON.

ACTIONS:
battery()              - battery level, status, time remaining
cpu()                  - CPU usage and info
ram()                  - RAM/memory usage
disk()                 - disk/storage space
network()              - network speed and connectivity
full_report()          - complete system overview
temperature()          - CPU temperature (if available)
processes()            - top running processes by CPU/RAM
wifi_info()            - current WiFi network info

OUTPUT FORMAT:
{"action":"action_name","data":{}}

EXAMPLES:
User: what is my battery level
{"action":"battery","data":{}}

User: how much battery is left
{"action":"battery","data":{}}

User: is my laptop charging
{"action":"battery","data":{}}

User: how much ram is being used
{"action":"ram","data":{}}

User: what is my cpu usage
{"action":"cpu","data":{}}

User: how much disk space do I have
{"action":"disk","data":{}}

User: check my storage
{"action":"disk","data":{}}

User: how is my network
{"action":"network","data":{}}

User: full system report
{"action":"full_report","data":{}}

User: system info
{"action":"full_report","data":{}}

User: what processes are running
{"action":"processes","data":{}}

User: what is my wifi network
{"action":"wifi_info","data":{}}

User: cpu temperature
{"action":"temperature","data":{}}"""


def _fix_wrapper(plan: dict) -> dict:
    if not plan:
        return plan
    action = plan.get("action")
    data = plan.get("data")
    if action and not data:
        top = {k: v for k, v in plan.items() if k != "action"}
        if top:
            return {"action": action, "data": top}
    return plan


def _get_psutil():
    try:
        import psutil
        return psutil, None
    except ImportError:
        return None, "psutil not installed. Run: pip install psutil"


class SystemAgent:
    def __init__(self):
        pass

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        q = (query or "").lower().strip()

        # Count how many system topics are mentioned — if multiple, skip AI
        topics = ["battery","charging","cpu","ram","memory","disk","storage",
                  "network","internet","wifi","temperature","process"]
        topic_count = sum(1 for t in topics if t in q)

        # 1) If multiple topics or broad query, go straight to full report
        if topic_count >= 2 or "full" in q or "system info" in q or "report" in q:
            action = "full_report"
        else:
            # 2) Try AI parse for single-topic queries
            action = None
            raw = _call_ollama_chat(system=SYSTEM_PROMPT, user=q + "\nJSON:")
            if raw and isinstance(raw, dict):
                plan = _fix_wrapper(raw)
                action = (plan.get("action") or "").strip().lower()

            # 3) Deterministic fallback
            if not action:
                if "battery" in q or "charging" in q:
                    action = "battery"
                elif "cpu" in q:
                    action = "cpu"
                elif "ram" in q or "memory" in q:
                    action = "ram"
                elif "disk" in q or "storage" in q:
                    action = "disk"
                elif "network" in q or "internet" in q or "wifi" in q:
                    action = "network"
                elif "temperature" in q or "temp" in q:
                    action = "temperature"
                elif "process" in q:
                    action = "processes"
                else:
                    action = "full_report"

        psutil, err = _get_psutil()
        if err:
            return f"❌ {err}"

        try:
            if action == "battery":
                return self._battery(psutil)
            elif action == "cpu":
                return self._cpu(psutil)
            elif action == "ram":
                return self._ram(psutil)
            elif action == "disk":
                return self._disk(psutil)
            elif action == "network":
                return self._network(psutil)
            elif action == "temperature":
                return self._temp(psutil)
            elif action == "processes":
                return self._processes(psutil)
            elif action == "wifi_info":
                return self._wifi()
            else:
                return self._full(psutil)
        except Exception as e:
            return f"❌ System info error: {e}"

    def _battery(self, psutil) -> str:
        bat = psutil.sensors_battery()
        if not bat:
            return "🔌 No battery detected (desktop PC or battery info unavailable)."
        pct = round(bat.percent)
        plugged = bat.power_plugged
        secs = bat.secsleft

        status = "🔌 Charging" if plugged else "🔋 On Battery"
        emoji = "🔋" if pct > 50 else ("⚠️" if pct > 20 else "🚨")

        if secs == psutil.POWER_TIME_UNLIMITED:
            time_str = "Fully charged" if pct >= 99 else "Calculating..."
        elif secs == psutil.POWER_TIME_UNKNOWN or secs < 0:
            time_str = "Calculating..."
        else:
            h, m = divmod(secs // 60, 60)
            time_str = f"{h}h {m}m remaining" if h else f"{m}m remaining"

        # Battery health hint
        if not plugged and pct <= 10:
            hint = "\n   🚨 CRITICAL BATTERY! Plug in now!"
        elif not plugged and pct <= 20:
            hint = "\n   ⚠️ Low battery! Please plug in."
        else:
            hint = ""

        return (
            f"{emoji} Battery: {pct}%\n"
            f"   {status}\n"
            f"   ⏱️ {time_str}{hint}"
        )

    def _cpu(self, psutil) -> str:
        cpu_pct = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()
        cpu_freq = psutil.cpu_freq()
        freq_str = f"{cpu_freq.current:.0f} MHz" if cpu_freq else "N/A"

        load = psutil.getloadavg() if hasattr(psutil, 'getloadavg') else None

        status = "🔴 High" if cpu_pct > 80 else ("🟡 Medium" if cpu_pct > 50 else "🟢 Normal")
        load_str = f"   Load avg: {load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}\n" if load else ""
        return (
            f"⚙️ CPU Usage: {cpu_pct}% ({status})\n"
            f"   Cores: {cpu_count} | Frequency: {freq_str}\n"
            f"{load_str}".rstrip()
        )

    def _ram(self, psutil) -> str:
        vm = psutil.virtual_memory()
        total_gb = vm.total / 1024**3
        used_gb = vm.used / 1024**3
        avail_gb = vm.available / 1024**3
        pct = vm.percent

        swap = psutil.swap_memory()
        swap_gb = swap.used / 1024**3

        status = "🔴 High" if pct > 80 else ("🟡 Medium" if pct > 60 else "🟢 Normal")
        return (
            f"💾 RAM Usage: {pct}% ({status})\n"
            f"   Used: {used_gb:.1f} GB / {total_gb:.1f} GB\n"
            f"   Available: {avail_gb:.1f} GB\n"
            f"   Swap: {swap_gb:.1f} GB used"
        )

    def _disk(self, psutil) -> str:
        lines = ["💿 Disk Storage:"]
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                total = usage.total / 1024**3
                used = usage.used / 1024**3
                free = usage.free / 1024**3
                pct = usage.percent
                status = "🔴" if pct > 90 else ("🟡" if pct > 75 else "🟢")
                lines.append(
                    f"   {status} {part.device}: {used:.1f}/{total:.1f} GB "
                    f"({pct}% used | {free:.1f} GB free)"
                )
            except Exception:
                pass
        return "\n".join(lines) if len(lines) > 1 else "❌ Could not read disk info."

    def _network(self, psutil) -> str:
        # Check connectivity
        import socket
        connected = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(3)
                s.connect(("8.8.8.8", 53))
            connected = True
        except Exception:
            pass

        # Network I/O
        net = psutil.net_io_counters()
        sent_mb = net.bytes_sent / 1024**2
        recv_mb = net.bytes_recv / 1024**2

        status = "🟢 Connected" if connected else "🔴 No Internet"
        return (
            f"🌐 Network: {status}\n"
            f"   📤 Sent: {sent_mb:.1f} MB\n"
            f"   📥 Received: {recv_mb:.1f} MB"
        )

    def _temp(self, psutil) -> str:
        if not hasattr(psutil, 'sensors_temperatures'):
            return "🌡️ Temperature sensors not available on this system."
        temps = psutil.sensors_temperatures()
        if not temps:
            return "🌡️ No temperature sensors found."
        lines = ["🌡️ Temperature readings:"]
        for name, entries in temps.items():
            for entry in entries[:2]:
                t = entry.current
                label = entry.label or name
                status = "🔴 Hot!" if t > 80 else ("🟡 Warm" if t > 65 else "🟢 Normal")
                lines.append(f"   {status} {label}: {t:.0f}°C")
        return "\n".join(lines[:8])

    def _processes(self, psutil) -> str:
        procs = []
        for p in psutil.process_iter(['name', 'cpu_percent', 'memory_percent']):
            try:
                procs.append(p.info)
            except Exception:
                pass

        # Sort by CPU
        top_cpu = sorted(procs, key=lambda x: x.get('cpu_percent', 0) or 0, reverse=True)[:5]
        top_mem = sorted(procs, key=lambda x: x.get('memory_percent', 0) or 0, reverse=True)[:5]

        lines = ["⚙️ Top processes by CPU:"]
        for p in top_cpu:
            lines.append(f"   {p['name']}: {p.get('cpu_percent',0):.1f}% CPU")
        lines.append("\n💾 Top processes by RAM:")
        for p in top_mem:
            lines.append(f"   {p['name']}: {p.get('memory_percent',0):.1f}% RAM")
        return "\n".join(lines)

    def _wifi(self) -> str:
        try:
            import subprocess
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            output = result.stdout
            if not output.strip():
                return "📶 No WiFi adapter found or not connected."

            lines = ["📶 WiFi Info:"]
            for line in output.splitlines():
                line = line.strip()
                for key in ("SSID", "Signal", "Radio type", "Authentication", "Channel"):
                    if line.startswith(key):
                        lines.append(f"   {line}")
            return "\n".join(lines) if len(lines) > 1 else "📶 WiFi info unavailable."
        except Exception as e:
            return f"❌ Could not get WiFi info: {e}"

    def _full(self, psutil) -> str:
        parts = []
        try:
            parts.append(self._battery(psutil))
        except Exception:
            pass
        try:
            parts.append(self._cpu(psutil))
        except Exception:
            pass
        try:
            parts.append(self._ram(psutil))
        except Exception:
            pass
        try:
            parts.append(self._disk(psutil))
        except Exception:
            pass
        try:
            parts.append(self._network(psutil))
        except Exception:
            pass
        return "\n\n".join(parts) if parts else "❌ Could not gather system info."