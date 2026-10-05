FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY telegram_phone_number_checker ./telegram_phone_number_checker

EXPOSE 8010

CMD ["python", "-m", "telegram_phone_number_checker.web"]
