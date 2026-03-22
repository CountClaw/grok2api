FROM python:3.13-slim


ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    VIRTUAL_ENV=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=7860

ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*


RUN useradd -m -u 1000 user

WORKDIR /app

RUN git clone --depth 1 https://github.com/CountClaw/grok2api .


RUN python -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir uv

RUN uv sync --frozen --no-dev --no-install-project --active




# Pre-install Playwright Chromium + OS deps to make auto-register/solver usable in Docker
# without doing `apt-get` at runtime.
RUN python -m playwright install --with-deps chromium

# When building on Windows, shell scripts may be copied with CRLF endings and
# without executable bit. Normalize both to keep ENTRYPOINT reliable.
RUN sed -i 's/\r$//' /app/scripts/*.sh || true \
    && chmod +x /app/scripts/*.sh || true

RUN mkdir -p /app/data /app/data/tmp /app/logs \
    && chown -R user:user /app /ms-playwright

USER user



EXPOSE 7860

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["/app/scripts/start.sh"]
