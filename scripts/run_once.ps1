# Разовый запуск для теста
py -3.10 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python -m nts_autojoin.service_main
