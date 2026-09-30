#!/usr/bin/env python3
"""
Server Quartermaster — Kỹ Sư Trưởng Cập Nhật & Bảo Trì Hệ Thống (09:00 AM)
Hỗ trợ cả 2 chế độ:
  1. CLI Subcommands:
       - `python3 main.py run` (mặc định): Chạy live run cập nhật và báo cáo Telegram.
       - `python3 main.py info`: Scout trạng thái hiện tại của hệ thống (read-only) và in kết quả.
       - `python3 main.py update`: Quét và ép cập nhật ngay lập tức.
       - `python3 main.py listen`: Chạy daemon bot Telegram tiếp nhận lệnh /info và /update 24/7.
"""

import sys
import os
import json
import time
import fcntl
import signal
import html
import urllib.request
import urllib.parse
import subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

CONFIG_PATH = Path.home() / ".workspace" / "config" / "updater_registry.json"
LOCK_PATH = Path.home() / ".workspace" / "run" / "quartermaster.lock"
ENV_PATH = Path.home() / ".workspace" / "quartermaster.env"

def load_env():
    env_vars = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip('"').strip("'")
    return env_vars

def send_telegram_report(report_text: str, custom_chat_id: str = None) -> bool:
    env_vars = load_env()
    token = env_vars.get("TELEGRAM_BOT_TOKEN") or env_vars.get("QUARTERMASTER_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("QUARTERMASTER_TELEGRAM_BOT_TOKEN")
    chat_id = custom_chat_id or env_vars.get("TELEGRAM_ADMIN_CHAT_ID") or os.getenv("TELEGRAM_ADMIN_CHAT_ID")

    if not token or not chat_id:
        print("[Quartermaster] Notice: ~/.workspace/quartermaster.env chưa được cấu hình. Bỏ qua gửi Telegram.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": report_text,
        "parse_mode": "HTML"
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                print("[Quartermaster] Đã gửi báo cáo Telegram thành công (HTTP 200).")
                return True
            else:
                body = resp.read().decode("utf-8")
                print(f"[Quartermaster] Telegram trả về mã {resp.status}: {body}")
                return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else ""
        print(f"[Quartermaster] Lỗi gửi Telegram (HTTPError {e.code}): {err_body}")
        return False
    except Exception as e:
        print(f"[Quartermaster] Lỗi gửi Telegram: {e}")
        return False

def run_cmd(cmd: str, cwd: str = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout.strip(), stderr.strip()
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(2)
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            proc.communicate()
            return -1, "", f"Command timed out after {timeout}s (Process group killed)"
    except Exception as e:
        return -2, "", str(e)

def process_service(svc: dict, dry_run: bool = False) -> dict:
    name = svc.get("name", "unknown")
    policy = svc.get("policy", "auto")
    svc_type = svc.get("type", "generic")
    cwd = svc.get("cwd")
    timeout = svc.get("timeout_sec", 120)

    result = {
        "name": name,
        "policy": policy,
        "status": "UP_TO_DATE",
        "detail": "",
        "error": None
    }

    if dry_run:
        if svc.get("probe_cmd"):
            code, out, _ = run_cmd(svc["probe_cmd"], cwd=cwd, timeout=timeout)
            if "NEW_VERSION:" in out:
                result["status"] = "ALERT"
                result["detail"] = out.replace("NEW_VERSION:", "Có bản mới: ")
            elif "UP_TO_DATE:" in out:
                result["status"] = "UP_TO_DATE"
                result["detail"] = out.replace("UP_TO_DATE:", "Hiện tại: ")
            else:
                result["detail"] = f"Probe: {out}"
        elif svc.get("version_cmd"):
            _, out, _ = run_cmd(svc["version_cmd"], cwd=cwd, timeout=timeout)
            result["detail"] = f"Version: {out}"
        else:
            result["detail"] = "Ready"
        return result

    # Chạy Probe nếu có
    probe_cmd = svc.get("probe_cmd")
    has_new_version = False
    probe_up_to_date = False

    if probe_cmd:
        code, out, err = run_cmd(probe_cmd, cwd=cwd, timeout=timeout)
        if "NEW_VERSION:" in out:
            has_new_version = True
            result["detail"] = out.replace("NEW_VERSION:", "Bản mới: ")
        elif "UP_TO_DATE:" in out:
            probe_up_to_date = True
            result["detail"] = out.replace("UP_TO_DATE:", "Hiện tại: ")
        else:
            result["detail"] = out[:100]

    # Xử lý Policy alert
    if policy == "alert":
        if has_new_version:
            result["status"] = "ALERT"
        else:
            result["status"] = "UP_TO_DATE"
        return result

    # Xử lý Policy auto
    if svc_type == "cli_self_update":
        ver_cmd = svc.get("version_cmd")
        old_ver = ""
        if ver_cmd:
            _, old_ver, _ = run_cmd(ver_cmd, cwd=cwd, timeout=timeout)

        up_cmd = svc.get("update_cmd")
        code, out, err = run_cmd(up_cmd, cwd=cwd, timeout=timeout)
        if code != 0:
            result["status"] = "FAILED"
            result["error"] = err or out
            return result

        new_ver = ""
        if ver_cmd:
            _, new_ver, _ = run_cmd(ver_cmd, cwd=cwd, timeout=timeout)

        if old_ver and new_ver and old_ver != new_ver:
            result["status"] = "UPDATED"
            result["detail"] = f"{old_ver} ➜ {new_ver}"
        else:
            result["status"] = "UP_TO_DATE"
            result["detail"] = new_ver or "Đang ở bản mới nhất"

    elif svc_type in ("docker", "make", "generic"):
        if probe_up_to_date:
            hc_cmd = svc.get("healthcheck_cmd")
            if hc_cmd:
                hc_code, hc_out, hc_err = run_cmd(hc_cmd, cwd=cwd, timeout=30)
                if hc_code != 0:
                    result["status"] = "FAILED"
                    result["error"] = f"Healthcheck failed: {hc_err or hc_out}"
                    return result
            result["status"] = "UP_TO_DATE"
            return result

        up_cmd = svc.get("update_cmd")
        if not up_cmd:
            result["status"] = "FAILED"
            result["error"] = "Thiếu cấu hình update_cmd"
            return result

        code, out, err = run_cmd(up_cmd, cwd=cwd, timeout=timeout)
        if code != 0:
            result["status"] = "FAILED"
            result["error"] = (err or out)[:200]
            if name == "orca":
                run_cmd("sudo systemctl start orca-serve.service", timeout=30)
            return result

        hc_cmd = svc.get("healthcheck_cmd")
        if hc_cmd:
            time.sleep(3)
            hc_code, hc_out, hc_err = run_cmd(hc_cmd, cwd=cwd, timeout=30)
            if hc_code != 0:
                result["status"] = "FAILED"
                result["error"] = f"Healthcheck failed: {hc_err or hc_out}"
                if name == "orca":
                    run_cmd("sudo systemctl start orca-serve.service", timeout=30)
                return result

        combined_out = (out + " " + err).lower()
        no_change_markers = [
            "already up to date",
            "image is up to date",
            "không có bản mới",
            "trùng sha256",
            "running",
            "skipped"
        ]
        if any(marker in combined_out for marker in no_change_markers):
            result["status"] = "UP_TO_DATE"
            result["detail"] = "Đang ở bản mới nhất (Health: OK)"
        else:
            result["status"] = "UPDATED"
            result["detail"] = "Đã cập nhật (Health: OK)"

    return result

def execute_engine(dry_run: bool = False, title_prefix: str = "Báo Cáo Cập Nhật Hạ Tầng") -> str:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return "⚠️ Một tiến trình Quartermaster khác đang chạy. Bỏ qua."

    start_time = time.time()
    if not CONFIG_PATH.exists():
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        return f"❌ Lỗi: Không tìm thấy registry tại {CONFIG_PATH}"

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        return f"❌ Lỗi parse JSON registry: {e}"

    services = config.get("services", [])
    results = []

    for svc in services:
        res = process_service(svc, dry_run=dry_run)
        results.append(res)

    elapsed = round(time.time() - start_time, 2)
    vn_tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now_str = datetime.now(vn_tz).strftime("%d/%m/%Y %H:%M:%S")

    report_lines = [
        f"🛠️ <b>{title_prefix}</b>",
        f"📅 <i>Thời gian: {now_str}</i>\n"
    ]

    for r in results:
        status_icon = "✅"
        if r["status"] == "UPDATED":
            status_icon = "🚀"
        elif r["status"] == "FAILED":
            status_icon = "❌"
        elif r["status"] == "ALERT":
            status_icon = "⚠️"

        svc_name_esc = html.escape(str(r['name']))
        status_esc = html.escape(str(r['status']))
        line = f"{status_icon} <b>{svc_name_esc}</b>: {status_esc}"

        if r["detail"]:
            detail_esc = html.escape(str(r['detail']))
            line += f" ({detail_esc})"
        if r["error"]:
            error_esc = html.escape(str(r['error']))
            line += f" — <code>{error_esc}</code>"

        report_lines.append(line)

    report_lines.append(f"\n⏱️ <i>Thời gian thực thi: {elapsed}s</i>")
    final_report = "\n".join(report_lines)

    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()
    return final_report

def start_telegram_listener():
    env_vars = load_env()
    token = env_vars.get("TELEGRAM_BOT_TOKEN") or env_vars.get("QUARTERMASTER_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("QUARTERMASTER_TELEGRAM_BOT_TOKEN")
    admin_chat_id = env_vars.get("TELEGRAM_ADMIN_CHAT_ID") or os.getenv("TELEGRAM_ADMIN_CHAT_ID")

    if not token:
        print("[Quartermaster Listener] Lỗi: Chưa cấu hình TELEGRAM_BOT_TOKEN.")
        sys.exit(1)

    # Tự động đồng bộ danh sách lệnh lên Telegram API (hiển thị nút Menu trên app)
    try:
        cmd_url = f"https://api.telegram.org/bot{token}/setMyCommands"
        cmd_payload = {
            "commands": [
                {"command": "info", "description": "Scout thông tin phiên bản & healthcheck hạ tầng (Read-only)"},
                {"command": "update", "description": "Quét & ép cập nhật các dịch vụ ngay lập tức"},
                {"command": "help", "description": "Hướng dẫn sử dụng và danh sách lệnh"}
            ]
        }
        req_cmd = urllib.request.Request(
            cmd_url,
            data=json.dumps(cmd_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req_cmd, timeout=10) as r:
            if r.status == 200:
                print("[Quartermaster Bot] Đã tự động đồng bộ danh sách lệnh lên Telegram Menu thành công.")
    except Exception as e:
        print(f"[Quartermaster Bot] Cảnh báo: Không thể đồng bộ commands lên Telegram: {e}")

    print(f"==> [Quartermaster Bot] Bắt đầu Long Polling nhận lệnh /info và /update...")
    last_update_id = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?offset={last_update_id + 1}&timeout=30"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=35) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if not data.get("ok"):
                time.sleep(3)
                continue

            for update in data.get("result", []):
                update_id = update["update_id"]
                if update_id > last_update_id:
                    last_update_id = update_id

                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue

                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                from_user = msg.get("from", {}).get("username", "user")

                # Chỉ nhận lệnh từ Admin Chat ID đã cấu hình
                if admin_chat_id and chat_id != str(admin_chat_id):
                    print(f"[Quartermaster Bot] Từ chối lệnh từ Chat ID lạ: {chat_id} (@{from_user})")
                    continue

                if text in ("/info", "/status"):
                    print(f"[Quartermaster Bot] Nhận lệnh {text} từ @{from_user}")
                    report = execute_engine(dry_run=True, title_prefix="Thông Tin Hạ Tầng Hiện Tại (/info)")
                    send_telegram_report(report, custom_chat_id=chat_id)

                elif text in ("/update", "/upgrade"):
                    print(f"[Quartermaster Bot] Nhận lệnh {text} từ @{from_user}")
                    send_telegram_report("⏳ <i>Đang bắt đầu quét và cập nhật hạ tầng...</i>", custom_chat_id=chat_id)
                    report = execute_engine(dry_run=False, title_prefix="Báo Cáo Cập Nhật Hạ Tầng (/update)")
                    send_telegram_report(report, custom_chat_id=chat_id)

                elif text in ("/start", "/help"):
                    help_msg = (
                        "🛠️ <b>Quartermaster Bot Command Center</b>\n\n"
                        "• <code>/info</code> : Scout thông tin phiên bản & health hiện tại (an toàn, không chạm vào service).\n"
                        "• <code>/update</code> : Quét và ép cập nhật các dịch vụ ngay lập tức.\n"
                        "• <code>/help</code> : Hiển thị bảng trợ giúp này."
                    )
                    send_telegram_report(help_msg, custom_chat_id=chat_id)

        except Exception as e:
            time.sleep(5)

def main():
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        print("Sử dụng: python3 main.py [run|info|update|listen|--dry-run]")
        print("  info     : Scout trạng thái các dịch vụ (read-only, an toàn).")
        print("  update   : Thực thi cập nhật ngay lập tức và gửi báo cáo về Telegram.")
        print("  listen   : Chạy bot Telegram Long Polling nhận lệnh /info và /update.")
        print("  run      : Chạy live run định kỳ (mặc định của systemd).")
        print("  --dry-run: Chế độ kiểm thử mô phỏng.")
        sys.exit(0)

    mode = args[0] if args else "run"

    if mode == "info" or "--dry-run" in args:
        report = execute_engine(dry_run=True, title_prefix="Thông Tin Hạ Tầng Hiện Tại (Info)")
        print(report)
    elif mode == "update":
        report = execute_engine(dry_run=False, title_prefix="Báo Cáo Cập Nhật Hạ Tầng (Update)")
        print(report)
        send_telegram_report(report)
    elif mode == "listen":
        start_telegram_listener()
    elif mode == "run":
        report = execute_engine(dry_run=False, title_prefix="Báo Cáo Cập Nhật Hạ Tầng (09:00 AM)")
        print(report)
        send_telegram_report(report)
    else:
        print(f"Tham số không hợp lệ: {mode}. Dùng --help để xem hướng dẫn.")
        sys.exit(1)

if __name__ == "__main__":
    main()
