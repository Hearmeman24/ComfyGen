# Publishing ComfyGen

ComfyGen publishes to PyPI as `comfy-gen`. The GitHub Actions workflow uses PyPI Trusted Publishing, so release jobs do not need a PyPI API token.

## One-time PyPI setup

1. Create or log in to the PyPI account that owns the package.
2. Create a pending Trusted Publisher for:
   - PyPI project name: `comfy-gen`
   - GitHub owner: `Hearmeman24`
   - GitHub repository: `ComfyGen`
   - Workflow filename: `publish-pypi.yml`
   - Environment name: `pypi`
3. Confirm that the GitHub repository has an environment named `pypi`. Add required reviewers if releases should require manual approval.

## Release process

1. Update `version` in `pyproject.toml`.
2. Run the local PyPI package release checks:

   ```bash
   uv run --extra dev pytest \
     tests/test_version_cli.py \
     tests/test_install_token_resolution.py \
     tests/test_install_preset_cli.py \
     tests/test_progress_format.py
   uv run --with build python -m build
   uv run --with twine python -m twine check dist/*
   tmpdir=$(mktemp -d)
   python3.12 -m venv "$tmpdir/venv"
   "$tmpdir/venv/bin/python" -m pip install --upgrade pip
   "$tmpdir/venv/bin/python" -m pip install dist/*.whl
   "$tmpdir/venv/bin/comfy-gen" --help >/dev/null
   ```

   The full test suite also covers the separate serverless runtime checkout used by RunPod workers. Those tests require that runtime checkout to be present locally; it is not a PyPI package dependency.

3. Commit the version change.
4. Create and push a matching semver tag:

   ```bash
   git tag v0.2.0
   git push origin main --tags
   ```

The `Publish Python package to PyPI` workflow builds the source distribution and wheel, verifies that the wheel installs cleanly, then publishes the artifacts to PyPI.

## BlockFlow compatibility

BlockFlow should depend on a released `comfy-gen` version from PyPI, not a mutable GitHub branch. Prefer a compatible lower bound plus an upper bound for breaking changes, for example:

```toml
dependencies = [
    "comfy-gen>=0.2,<0.3",
]
```

The RunPod worker runtime is deployed separately from the PyPI package. ComfyGen configures new endpoints with `RUNTIME_REPO_URL`, so BlockFlow should only depend on the `comfy-gen` CLI package and should not vendor or install the worker runtime itself.

When ComfyGen changes CLI behavior that BlockFlow depends on, release ComfyGen first, update BlockFlow's bound or lock, and verify `comfy-gen --help` from BlockFlow's managed environment.
