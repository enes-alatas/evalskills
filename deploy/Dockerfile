# evalskills: app server + SkillEvaluator in one image.
# Build:  docker build -f deploy/Dockerfile -t evalskills .
FROM python:3.13-slim

ARG SKILLEVALUATOR_REF=d136a597c54784ad8a0ce28542c208602b397a5c
ARG UV_VERSION=0.13.7

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==${UV_VERSION}" \
    && useradd --create-home --uid 10001 app

USER app
WORKDIR /home/app
ENV PATH=/home/app/.local/bin:$PATH \
    SKILLEVALUATOR_DIR=/home/app/SkillEvaluator \
    UV_PYTHON_PREFERENCE=only-system

# Pinned evaluator, with all extras (tier1 scanners, tier2, tier3/Harbor) installed into its own venv.
RUN git clone https://github.com/NVIDIA/SkillEvaluator.git "$SKILLEVALUATOR_DIR" \
    && git -C "$SKILLEVALUATOR_DIR" checkout -q "$SKILLEVALUATOR_REF" \
    && cd "$SKILLEVALUATOR_DIR" && uv sync --python 3.13 --extra all --frozen \
    # Warm the pinned SkillSpector + Gitleaks downloads so the first evaluation does not fetch them.
    && uv run --python 3.13 --extra security skillevaluator tier1 \
         src/skillevaluator/tier3/reference_skills/text-analyzer --no-llm -r json -o /tmp/warm || true \
    && rm -rf /tmp/warm \
    # Fail the build if the scanners were not provisioned (they must not be fetched at runtime).
    && ls /home/app/.local/share/skillevaluator/runtimes/skillspector /home/app/.local/share/skillevaluator/runtimes/gitleaks

COPY --chown=app:app pipeline/ /home/app/inventory/pipeline/
COPY --chown=app:app web/ /home/app/inventory/web/
COPY --chown=app:app docs/ /home/app/inventory/docs/
COPY --chown=app:app catalog/ /home/app/inventory/catalog/
WORKDIR /home/app/inventory

# data/ (SQLite), reports/ and cache/ live on a volume.
VOLUME ["/home/app/inventory/data", "/home/app/inventory/reports", "/home/app/inventory/cache"]
EXPOSE 8080
HEALTHCHECK CMD curl -fs http://localhost:8080/api/meta || exit 1
CMD ["python3", "-I", "pipeline/server.py", "--port", "8080"]
