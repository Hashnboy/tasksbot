import json
import os

CREDENTIALS_FILE = "credentials.json"  # или "/etc/secrets/credentials.json" если через Secret Files

print("=== Проверка файла ===")
print("Путь:", os.path.abspath(CREDENTIALS_FILE))

try:
    with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
        creds = json.load(f)
        print("client_email:", creds.get("client_email"))
        print("project_id:", creds.get("project_id"))
except Exception as e:
    print("Ошибка чтения файла:", e)
