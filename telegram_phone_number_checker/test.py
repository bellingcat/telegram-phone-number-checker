import subprocess
import time

phones_file_path = r''

with open(phones_file_path, 'r', encoding='utf-8') as file:
    phone_numbers = [line.strip() for line in file if line.strip()]

api_id = ''
api_hash = ''
api_phone_number = ''

python_executable = r'python.exe'

base_command = [
    python_executable, 'main.py',
    '--api-id', api_id,
    '--api-hash', api_hash,
    '--api-phone-number', api_phone_number,
]

for number in phone_numbers:
    command = base_command + ['-p', number]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    print(f"результат для номера {number}:")

    if result.stdout:
        print(f"ответ: {result.stdout}")
    if result.stderr:
        print(f"ошибка: {result.stderr}")

    time.sleep(60)