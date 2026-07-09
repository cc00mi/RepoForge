# ============================================================================
# Repoforge — Docker Image
# ============================================================================
# Build:
#   docker build -t repoforge .
#
# Run (Dashboard only):
#   docker run -p 8000:8000 \
#     -e DEEPSEEK_API_KEY=sk-xxx \
#     repoforge dashboard
#
# Run (Webhook server):
#   Mount private key file and use GITHUB_APP_PRIVATE_KEY_PATH:
#     docker run -p 8000:8000 \
#       -e DEEPSEEK_API_KEY=sk-xxx \
#       -e GITHUB_APP_ID=123456 \
#       -e GITHUB_APP_PRIVATE_KEY_PATH=/app/github-app.pem \
#       -v /path/to/github-app.pem:/app/github-app.pem:ro \
#       -e GITHUB_WEBHOOK_SECRET=secret \
#       repoforge serve
#
#   Alternatively (not recommended, may trigger .env warnings):
#     -e GITHUB_APP_PRIVATE_KEY="$(cat key.pem)" \
# ============================================================================

FROM python:3.11-slim

LABEL org.opencontainers.image.title="Repoforge"
LABEL org.opencontainers.image.description="CI-native autonomous coding agent pipeline"

# System dependencies: git is required for cloning repos
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash agent

# Copy source and install in one step
WORKDIR /home/agent/app
COPY --chown=agent:agent . .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir . && \
    pip install --no-cache-dir gunicorn && \
    mkdir -p /home/agent/app/logs /home/agent/app/pipeline_repos /home/agent/app/benchmark_results

USER agent
EXPOSE 8000

# Default entrypoint: show help
ENTRYPOINT ["repoforge-pipe"]
CMD ["--help"]
