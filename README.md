# Server Quartermaster (Kỹ Sư Trưởng Cập Nhật & Bảo Trì 09:00 AM)

Micro-service đảm nhận công việc kiểm tra và tự động cập nhật phần mềm, container, CLI tools lúc 09:00 AM mỗi ngày cho hệ sinh thái Virtual Office.

## 1. Cơ Chế Hoạt Động
- Được kích hoạt tự động qua `server-quartermaster.timer` (09:00:00 hàng ngày).
- Đọc danh sách dịch vụ đăng ký tại `~/.workspace/config/updater_registry.json`.
- Chạy tuần tự an toàn từng dịch vụ, khóa file bằng `flock` chống đè tiến trình.
- Tự động gửi báo cáo tổng hợp tình trạng cập nhật về Telegram mỗi ngày.

## 2. Cấu Hình
- Registry cấu hình mẫu: `updater_registry.example.json`
- Môi trường Telegram: `.env.example` -> copy sang `~/.workspace/quartermaster.env` và `chmod 600`.
