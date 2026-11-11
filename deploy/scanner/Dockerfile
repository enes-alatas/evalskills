# evalskills static scanner (#65): SkillEvaluator's own Tier 1 image, pinned by digest, plus a small driver.
# It runs with no network, no secrets and no Docker socket (deploy/compose.yaml); scanner.py describes the protocol.
# The digest must match .github/workflows/skills.yml and deploy/compose.yaml (tests/test_deploy_config.py checks it).
# Build: docker build -f deploy/scanner/Dockerfile -t evalskills-scanner .
ARG TIER1_IMAGE=ghcr.io/nvidia/skillevaluator-tier1@sha256:9d5483243918937be4f2178f74cf7c622a531dee41e3176906994d52fa14950a
FROM ${TIER1_IMAGE}
ARG TIER1_IMAGE
ENV SCANNER_IMAGE=${TIER1_IMAGE} \
    SCANNER_IN=/scan/in \
    SCANNER_OUT=/scan/out

USER root
COPY deploy/scanner/scanner.py /opt/evalskills/scanner.py
# Mount points exist in the image, owned by the scanner's user, so new named volumes take that owner (#71).
# The app runs as the same uid (10001), so both sides can read what the other writes.
RUN python -m compileall -q /opt/evalskills && install -d -o evaluator -g evaluator /scan/in /scan/out
USER evaluator
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/evalskills/scanner.py"]
