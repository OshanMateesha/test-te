FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps webkit \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["python", "final.py"]
