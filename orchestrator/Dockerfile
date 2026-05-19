FROM golang:1.25-alpine AS nyann-bench-build
WORKDIR /src
RUN apk add --no-cache git
RUN git clone --depth=1 -b per-stage-summary \
    https://github.com/neuralmagic/nyann-bench.git .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /nyann-bench ./cmd/nyann-bench/

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
    py3-matplotlib \
    py3-numpy

# Install yq (multiarch)
ARG TARGETARCH
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${TARGETARCH}" \
    -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq

COPY --from=nyann-bench-build /nyann-bench /usr/local/bin/nyann-bench

WORKDIR /workspace
