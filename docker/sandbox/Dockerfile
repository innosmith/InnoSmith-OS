FROM python:3.12-slim

LABEL maintainer="InnoSmith GmbH"
LABEL description="TaskPilot Code Sandbox — isolierte Python-Umgebung für generierte Scripts"

RUN groupadd -r sandbox && useradd -r -g sandbox -d /workspace -s /bin/false sandbox

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/sandbox/requirements.txt
RUN pip install --no-cache-dir -r /opt/sandbox/requirements.txt

# /daten ist der Einhängepunkt des Datenraums (schreibgeschützt, vom Executor bei
# jedem Lauf gesetzt). Explizit anlegen, damit er auch bei read-only Root-FS steht.
RUN mkdir -p /input /daten /workspace /tmp && \
    chown sandbox:sandbox /workspace /tmp

COPY seccomp-profile.json /opt/sandbox/seccomp-profile.json
COPY entrypoint.sh /opt/sandbox/entrypoint.sh
RUN chmod +x /opt/sandbox/entrypoint.sh

# Fuer read-only Root-FS + non-root Betrieb: Caches/Config in beschreibbare Pfade
# (der Executor mountet /tmp als tmpfs und ueberschreibt HOME zusaetzlich per -e).
ENV MPLCONFIGDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

USER sandbox
WORKDIR /workspace

ENTRYPOINT ["/opt/sandbox/entrypoint.sh"]
