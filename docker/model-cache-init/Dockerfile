FROM alpine:3.20

RUN apk add --no-cache \
    curl \
    wget \
    jq \
    coreutils \
    bash \
    aws-cli

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY metrics.sh /usr/local/bin/metrics.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/metrics.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
