FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer — only rebuilds if requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .

CMD ["python", "-u", "bot.py"]
