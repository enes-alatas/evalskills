# The trial base image for --env-mode gvisor (#137, condition 3). It holds what SkillEvaluator d136a597's generated
# task Dockerfile would otherwise install in every trial: the apt line and the verifier's pip packages. Those now
# come from the Debian archive and from a hash-pinned lock, and are installed once, off-host, with network. Trial
# builds then start FROM this image and run only the template's fixed steps, with --network none.
#
# Build it off-host, on a machine with Docker, and load it on the host. See ../README.md. Never build it on the host.
FROM python:3.12-slim@sha256:0000000000000000000000000000000000000000000000000000000000000000

RUN apt-get -o Acquire::Retries=3 update && \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    bash curl git jq ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock \
    && python -c "from ragas.llms.base import llm_factory; from ragas.messages import AIMessage, HumanMessage; from ragas.metrics.collections import AgentGoalAccuracyWithReference"
