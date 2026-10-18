# Contributing

Thanks for helping improve ARC Intel. Contributions that fix bugs, improve policy
coverage, clarify deployment guidance, or strengthen tests are welcome.

## Before opening a pull request

- Check the existing issues and pull requests for related work.
- For larger changes, open an issue first to discuss the proposed approach.
- Keep changes focused and update the relevant documentation when behavior or
  deployment steps change.
- Never commit credentials, tokens, private keys, or environment-specific secrets.

## Development and tests

The MCP server requires Python 3.11 or newer. Install its development dependencies
and run the test suite from the repository root:

```bash
python -m pip install -e "./mcp-server[dev]"
python -m pytest mcp-server/tests
```

If you change the offline cluster fixture, regenerate it with:

```bash
python mcp-server/tests/fixtures.py demo/offline/cluster.yaml
```

For deployment or policy changes, describe the affected components and any
cluster-specific validation that could not be run locally.

## Pull requests

- Use a clear title and explain the problem and solution.
- Include relevant test results and note any configuration or security impact.
- Keep unrelated formatting or generated-file changes out of the pull request.
