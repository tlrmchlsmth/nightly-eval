FROM alpine:3.20

RUN apk add --no-cache \
    bash \
    curl \
    git \
    helm \
    jq \
    kubectl \
    python3 \
    py3-pip

RUN pip3 install --break-system-packages matplotlib numpy

# Install yq
RUN curl -fsSL https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 \
    -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq

# Install just
RUN curl -fsSL https://just.systems/install.sh | bash -s -- --to /usr/local/bin

WORKDIR /workspace
