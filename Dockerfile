FROM python:3.12.3-slim
LABEL authors="katehok"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ .

ENTRYPOINT ["python", "-m", "main"]