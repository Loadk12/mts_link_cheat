# NTS Auto-Join (Windows) — МТС Линк

Собран под твою ссылку и имя.

- Сервисный режим через NSSM (или разовый запуск).
- Не даёт Windows уснуть.
- Автовход в лобби МТС Линк (фокус → ввод имени → клик «Присоединиться»).
- Healthcheck: DOM (кнопки/aria-label), WebSocket, WebRTC. Авторелоад при обрыве.
- Уведомления в Telegram (заполни токен/чат при желании).

## Запуск (быстрый тест)

```powershell
cd <папка_проекта>
py -3.10 -m venv venv
.env\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python -m nts_autojoin.service_main
```

Логи смотри в `./logs/ntsbot.log`.

## Установка как сервис (опционально)
```powershell
# от имени администратора; положи nssm.exe в .\scripts\ или добавь в PATH
.\scripts\install_service.ps1
```

## Где править
- `config/schedule.yaml` — расписание/уведомления/селекторы.
- Таймзона стоит `Europe/Moscow` — поменяй при необходимости.
