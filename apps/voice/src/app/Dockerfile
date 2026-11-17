FROM node:22-bookworm-slim AS calling-sdk
WORKDIR /sdk
COPY backend/native_rtc/package.json backend/native_rtc/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY backend/native_rtc/ ./
RUN npm run build

FROM python:3.12-slim AS builder
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
RUN apt-get update && apt-get install -y \
  build-essential \
  curl \
  git \
  && find /var/lib/apt/lists -maxdepth 1 -type f -delete
# Create a virtualenv to keep dependencies together
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY ./requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
ARG FILENAME
ARG PORT=8000
ARG HOST=0.0.0.0
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN python -m playwright install --with-deps --only-shell chromium \
  && find /var/lib/apt/lists -maxdepth 1 -type f -delete
COPY . .
COPY --from=calling-sdk /sdk/dist/gateway.bundle.js ./backend/native_rtc/dist/gateway.bundle.js
EXPOSE $PORT
ENTRYPOINT [ "gunicorn", "app:create_app", "-b", "0.0.0.0:8000", "--worker-class", "aiohttp.GunicornWebWorker", "--access-logfile", "-" ]