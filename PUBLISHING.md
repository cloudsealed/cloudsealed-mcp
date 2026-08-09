# Publishing checklist

Two things this repo cannot do on its own — both require a human in the loop.

## 1. Publish to PyPI

No PyPI credentials are available in the environment that built this repo.
From a machine with `twine` and a PyPI account with rights to the
`cloudsealed-mcp` name:

```bash
git clone https://github.com/cloudsealed/cloudsealed-mcp
cd cloudsealed-mcp
python -m build
twine upload dist/*
```

Verify at https://pypi.org/project/cloudsealed-mcp/. Once published,
`uvx cloudsealed-mcp` (already documented in README.md) works without any
extra step.

## 2. Publish to the official MCP Registry

`server.json` at the repo root is already filled in and matches the
`mcp-name: io.github.cloudsealed/cloudsealed-mcp` marker in README.md (the
PyPI ownership-verification token the registry checks for). Once step 1 is
done:

```bash
# install mcp-publisher (macOS/Linux)
curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" | tar xz mcp-publisher

# authenticate — opens a device-flow login at github.com/login/device
./mcp-publisher login github

# publish (validates server.json against the now-live PyPI package)
./mcp-publisher publish
```

Verify with:

```bash
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.cloudsealed/cloudsealed-mcp"
```

This is what makes the server discoverable through the registry that MCP
clients query directly — the highest-leverage step for AI-agent discovery,
and the reason this checklist exists as its own file instead of being buried
in the README.
