FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9000

CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-9000} --log-config logging.yaml"]