FROM alpine:3.20

RUN apk add --no-cache \
    curl \
    wget \
    jq \
    coreutils \
    bash \
    aws-cli

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
