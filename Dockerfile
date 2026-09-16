FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer — only rebuilds if requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code -- ALL modules, not just bot.py. config.py, stats.py,
# roster_read.py and requirements.py are separate modules bot.py imports
# at startup; shipping only bot.py crashes immediately on import.
COPY *.py .

CMD ["python", "-u", "bot.py"]
