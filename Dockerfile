# An explicit build, because the inferred one had a step we could not fix.
#
# Railpack detects Python, installs it through mise, and mounts the host's
# environment variables into the build as BuildKit secrets. One malformed
# variable is enough to fail that step before any of this project's code is
# touched — and the error names neither the variable nor a way to skip it.
#
# Nothing here needs a variable at build time. Dependencies come from
# requirements.txt and everything else is read at startup, so the build has
# no secrets to mount and no step that can fail on one.
FROM python:3.11-slim

# Line-buffered rather than block-buffered: a platform log collector is a
# pipe, and logs that arrive after the container dies are the ones you needed.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt itself
# changes, so a code-only deploy does not reinstall httpx and anthropic.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# `docs/index.html` has to be in the image or the control room serves a 500
# while /api/health still returns 200 — up by every automated measure, broken
# to anyone who opens it. Fail here instead, where someone is reading.
RUN test -f docs/index.html || (echo "docs/index.html is missing from the image" && exit 1)

# The same command as Procfile and railway.json. main.py puts src on the path.
CMD ["python", "main.py"]
