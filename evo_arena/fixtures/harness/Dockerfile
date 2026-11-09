FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep procps ca-certificates && rm -rf /var/lib/apt/lists/*
RUN npm install --global @deepseek-ai/dsh@0.1.2-rc.1 && dsh --version
RUN npm install --global @earendil-works/pi-coding-agent@0.82.1 && pi --version
COPY --chmod=755 order.cjs /usr/local/bin/arena-order
COPY relay.cjs /usr/local/lib/evoarena-relay.cjs
WORKDIR /work
