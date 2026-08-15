FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY bot.py .
RUN uv sync --frozen --no-dev
CMD ["uv", "run", "python", "bot.py"]
