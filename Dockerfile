# Build a wheel, then install it into a slim, non-root runtime image.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY workspaces_euc_mcp_server ./workspaces_euc_mcp_server
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/bengroeneveldsg/aws-workspaces-euc-mcp"
LABEL org.opencontainers.image.description="Admin MCP server for the Amazon WorkSpaces EUC portfolio"
LABEL org.opencontainers.image.licenses="Apache-2.0"

RUN useradd --create-home --uid 1000 mcp
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

USER mcp
# The server speaks MCP over stdio; run with `docker run -i`.
ENTRYPOINT ["workspaces-euc-mcp-server"]
