FROM registry.gitlab.com/packaging/signal-cli/signal-cli-native:latest

USER root

RUN apt-get update -qq && \
    apt-get install -y -qq python3 python3-pip python3-venv cron && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/accumulator-venv
COPY requirements.txt /opt/accumulator/
RUN /opt/accumulator-venv/bin/pip install --no-cache-dir -r /opt/accumulator/requirements.txt

COPY capture.py serve.py entrypoint.sh /opt/accumulator/
RUN chmod +x /opt/accumulator/entrypoint.sh

VOLUME /data
EXPOSE 8081

ENTRYPOINT ["/opt/accumulator/entrypoint.sh"]
