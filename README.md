# NTS Auto-Join (Windows) - МТС Линк

Windows/NSSM-сервис для автоподключения к занятиям МТС Линк по расписанию из `config/schedule.yaml`.

## Быстрый запуск

```powershell
cd <папка_проекта>
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
python -m playwright install chromium
python -m nts_autojoin.service_main
```

Логи пишутся в `logs/ntsbot.log`.

## Локальные секреты

Не храните токены и cookies в отслеживаемых файлах. Для локальных значений используйте `config/config.local.yaml`:

```yaml
notify:
  telegram_bot_token: "<token>"
  telegram_chat_id: "<chat_id>"
  allowed_user_ids:
  - 123456789
```

Cookies для ручного скрейпа Мосполитеха кладутся в `secrets/mospoly_cookies.json`. Папка `secrets/` исключена из Git.

## NSSM

Установка сервиса от имени администратора:

```powershell
.\scripts\install_service.ps1
```

Проверка параметров:

```powershell
nssm get NTS-AutoJoin Application
nssm get NTS-AutoJoin AppDirectory
nssm get NTS-AutoJoin AppParameters
nssm get NTS-AutoJoin AppStdout
nssm get NTS-AutoJoin AppStderr
```

Ожидаемо:

- `Application`: `<repo>\venv\Scripts\python.exe`
- `AppDirectory`: `<repo>`
- `AppParameters`: `-m nts_autojoin.service_main`

## Где править

- `config/schedule.yaml` - расписание, селекторы входа, healthcheck, Chromium.
- `config/config.local.yaml` - локальные Telegram-секреты.
- `assets/black.y4m` и `assets/silence.wav` - fake media для Chrome.
