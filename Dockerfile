FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No CMD needed - Railway cron services run on schedule

# Idle process: jobs are run by Coolify scheduled tasks via exec
CMD ["sleep", "infinity"]