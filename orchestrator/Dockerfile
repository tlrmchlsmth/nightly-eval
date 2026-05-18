FROM ghcr.io/neuralmagic/nyann-bench:consolidate-workers-flag AS nyann-bench

FROM alpine:3.20

RUN apk add --no-cache \
    bash \
    curl \
    gettext \
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

COPY --from=nyann-bench /nyann-bench /usr/local/bin/nyann-bench

WORKDIR /workspace
