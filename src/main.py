#!/usr/bin/env python3
"""
Server Quartermaster — Kỹ Sư Trưởng Cập Nhật & Bảo Trì Hệ Thống (09:00 AM)
Đọc cấu hình từ ~/.workspace/config/updater_registry.json, thực thi tuần tự an toàn,
kiểm tra healthcheck và gửi bản tin tổng hợp HTML về Telegram.
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

def send_telegram_report(report_text: str) -> bool:
    env_vars = load_env()
    token = env_vars.get("QUARTERMASTER_TELEGRAM_BOT_TOKEN") or os.getenv("QUARTERMASTER_TELEGRAM_BOT_TOKEN")
    chat_id = env_vars.get("TELEGRAM_ADMIN_CHAT_ID") or os.getenv("TELEGRAM_ADMIN_CHAT_ID")

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
    """
    Thực thi lệnh với process group riêng biệt.
    Nếu timeout, gửi SIGTERM đến toàn bộ process group để tránh mồ côi cháu (grandchildren).
    """
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
        print(f"==> [DRY-RUN] Kiểm tra dịch vụ: {name} ({svc_type}, policy: {policy})")
        if svc.get("probe_cmd"):
            code, out, _ = run_cmd(svc["probe_cmd"], cwd=cwd, timeout=timeout)
            if "NEW_VERSION:" in out:
                result["status"] = "ALERT"
                result["detail"] = out.replace("NEW_VERSION:", "Có bản mới: ")
            elif "UP_TO_DATE:" in out:
                result["status"] = "UP_TO_DATE"
                result["detail"] = out.replace("UP_TO_DATE:", "Hiện tại: ")
            else:
                result["detail"] = f"Probe exit {code}: {out}"
        elif svc.get("version_cmd"):
            _, out, _ = run_cmd(svc["version_cmd"], cwd=cwd, timeout=timeout)
            result["detail"] = f"Current version: {out}"
        else:
            result["detail"] = "Ready (dry-run)"
        return result

    print(f"==> Đang kiểm tra & xử lý: {name} (policy: {policy})")

    # 1. Chạy Probe nếu có
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

    # 2. Xử lý Policy alert
    if policy == "alert":
        if has_new_version:
            result["status"] = "ALERT"
        else:
            result["status"] = "UP_TO_DATE"
        return result

    # 3. Xử lý Policy auto
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
        # Nếu probe đã xác nhận dịch vụ đang ở bản mới nhất, kiểm tra healthcheck rồi giữ UP_TO_DATE
        if probe_up_to_date:
            hc_cmd = svc.get("healthcheck_cmd")
            if hc_cmd:
                hc_code, hc_out, hc_err = run_cmd(hc_cmd, cwd=cwd, timeout=30)
                if hc_code != 0:
                    result["status"] = "FAILED"
                    result["error"] = f"Service không hoạt động (Healthcheck failed: {hc_err or hc_out})"
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

        # Chạy healthcheck
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

        # Kiểm tra output xem có thực sự update hay không
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

def main():
    dry_run = "--dry-run" in sys.argv
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[Quartermaster] Một tiến trình Quartermaster khác đang chạy. Bỏ qua.")
        sys.exit(0)

    start_time = time.time()
    print(f"=== BẮT ĐẦU QUARTERMASTER RUNNER ({'DRY-RUN' if dry_run else 'LIVE'}) ===")

    if not CONFIG_PATH.exists():
        print(f"[Quartermaster] Lỗi: Không tìm thấy registry tại {CONFIG_PATH}")
        sys.exit(1)

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[Quartermaster] Lỗi parse JSON registry: {e}")
        sys.exit(1)

    services = config.get("services", [])
    results = []

    for svc in services:
        res = process_service(svc, dry_run=dry_run)
        results.append(res)

    elapsed = round(time.time() - start_time, 2)
    print(f"=== HOÀN TẤT TRONG {elapsed}s ===")

    # Format thời gian theo múi giờ Asia/Ho_Chi_Minh
    vn_tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now_str = datetime.now(vn_tz).strftime("%d/%m/%Y %H:%M")

    report_lines = [
        f"🛠️ <b>Báo Cáo Cập Nhật Hạ Tầng (09:00 AM)</b>",
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

    print("\n--- BẢN TIN TELEGRAM DỰ KIẾN ---")
    print(final_report)
    print("--------------------------------\n")

    if not dry_run:
        send_telegram_report(final_report)

    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()

if __name__ == "__main__":
    main()
