# evalskills: the app server (zone A). It runs no evaluator: static scans go to the scanner sidecar (#65,
# deploy/scanner/). git clones skills; curl serves the healthcheck.
# Build:  docker build -f deploy/Dockerfile -t evalskills .
# By digest, so a rebuild can't change the base image without a commit; Dependabot proposes the bumps (#178).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

USER app
WORKDIR /home/app
COPY --chown=app:app pipeline/ /home/app/inventory/pipeline/
COPY --chown=app:app web/ /home/app/inventory/web/
COPY --chown=app:app docs/ /home/app/inventory/docs/
COPY --chown=app:app catalog/ /home/app/inventory/catalog/
WORKDIR /home/app/inventory

# data/ (SQLite), reports/ and cache/ live on a volume. A new named volume takes its owner from the image directory
# it mounts on, and Docker creates a missing one as root, which the non-root app cannot write (#71). So every mount
# point in deploy/compose.yaml exists here, owned by app.
USER root
RUN install -d -o app -g app /home/app/inventory/data /home/app/inventory/reports /home/app/inventory/cache \
        /home/app/inventory/scan-in /home/app/inventory/scan-out
USER app
VOLUME ["/home/app/inventory/data", "/home/app/inventory/reports", "/home/app/inventory/cache"]
EXPOSE 8080
HEALTHCHECK CMD curl -fs http://localhost:8080/api/meta || exit 1
CMD ["python3", "-I", "pipeline/server.py", "--port", "8080"]
