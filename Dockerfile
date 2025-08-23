FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim
WORKDIR /app
COPY . .
RUN uv sync
EXPOSE 8000
CMD ["uv", "run", "fastapi", "run", "main.py"]