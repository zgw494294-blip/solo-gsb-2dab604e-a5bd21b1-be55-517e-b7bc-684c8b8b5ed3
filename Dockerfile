FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DB_PATH=/app/data/timetable.db \
    SAMPLE_TIMETABLE=/app/sample_data/timetable.json \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
