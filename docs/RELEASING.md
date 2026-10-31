# Releasing

labctl is published to PyPI as
[`embedded-lab-control`](https://pypi.org/project/embedded-lab-control/)
by `.github/workflows/release.yml`, using
[trusted publishing](https://docs.pypi.org/trusted-publishers/): there are
no API tokens. Pushing a tag `vX.Y.Z` runs:

1. **CI gates**: `ci.yml` (tests on 3.10-3.13, flake8/black/isort,
   gitleaks over the full history), called as a reusable workflow.
2. **build**: checks the tag is `v` + `labctl.__version__` and that
   `CHANGELOG.md` has a `## [X.Y.Z]` section, builds the sdist and wheel
   once, runs `twine check --strict`, and keeps them as the `dist`
   artifact.
3. **publish-testpypi** (environment `testpypi`): uploads to TestPyPI.
4. **publish-pypi** (environment `pypi`): waits for your approval, then
   uploads the same files to PyPI.
5. **github-release**: creates the GitHub Release for the tag with the
   files and the CHANGELOG section as notes.

Running the workflow by hand (Actions tab, or `gh workflow run Release`
on a branch) does steps 1-2 only; it never publishes.

## One-time setup

**PyPI and TestPyPI**: add a *pending publisher* on each (they are
separate accounts: <https://pypi.org/manage/account/publishing/> and
<https://test.pypi.org/manage/account/publishing/>), with:

| Field | PyPI | TestPyPI |
|-------|------|----------|
| PyPI project name | `embedded-lab-control` | `embedded-lab-control` |
| Owner | `johnjezl` | `johnjezl` |
| Repository name | `Embedded-Lab-Control` | `Embedded-Lab-Control` |
| Workflow name | `release.yml` | `release.yml` |
| Environment name | `pypi` | `testpypi` |

The first successful upload creates the project and turns the pending
publisher into a normal one.

**GitHub environments** (Settings → Environments):

- `pypi`: add yourself as a required reviewer (leave "Prevent
  self-review" off: you push the tag and approve the deployment).
  Under deployment branches and tags, choose "Selected branches and tags"
  and add the tag rule `v*`.
- `testpypi`: no reviewer needed, but add the same `v*` tag rule, so only a
  tag (never a branch) can deploy to it.

Optionally, a tag ruleset (Settings → Rules) restricting who may create
`v*` tags.

## Release steps

1. In a PR: set `__version__` in `src/labctl/__init__.py`, and in
   `CHANGELOG.md` rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
   (the date you will tag), add a short summary at the top of the
   section, add a fresh empty `## [Unreleased]` above it, and update the
   link definitions at the end of the file. Merge it.
2. Tag the merge commit on `main` and push the tag:

   ```bash
   git switch main && git pull --ff-only
   git tag -a vX.Y.Z -m "embedded-lab-control X.Y.Z"
   git push origin vX.Y.Z
   ```

3. Watch the run (`gh run watch`, or the Actions tab) until
   `publish-testpypi` succeeds and `publish-pypi` is waiting.
4. Check the TestPyPI upload in a scratch venv. Take only labctl's wheel
   from TestPyPI and every dependency from PyPI (anyone can register a
   dependency's name on TestPyPI):

   ```bash
   python3 -m venv /tmp/labctl-rc
   /tmp/labctl-rc/bin/pip download --no-deps --no-cache-dir -d /tmp/labctl-rc/dl \
       --index-url https://test.pypi.org/simple/ "embedded-lab-control==X.Y.Z"
   /tmp/labctl-rc/bin/pip install --no-cache-dir \
       "$(ls /tmp/labctl-rc/dl/embedded_lab_control-X.Y.Z-*.whl)[web,mcp]"
   /tmp/labctl-rc/bin/labctl --version
   /tmp/labctl-rc/bin/labctl --help
   ```

5. Approve the `pypi` deployment in the run ("Review deployments"). The
   GitHub Release is created after the PyPI upload.

## If a step fails

A version's file names can be uploaded only once to each index, even
after deleting them (yanking doesn't free them either).

- **CI or build fails** (nothing uploaded): fix it on `main` through a
  PR, then move the tag: `git push --delete origin vX.Y.Z`,
  `git tag -d vX.Y.Z`, and tag and push again.
- **A publish job fails** (before uploading, or partway: re-runs skip files
  already uploaded) (e.g. a pending-publisher or
  environment name mismatch: the error names the claims PyPI received):
  fix the settings and use "Re-run failed jobs" on the same run.
- **TestPyPI has it but it's broken**: reject the `pypi` deployment.
  X.Y.Z is used up on TestPyPI, so fix it and release the next patch
  version (X.Y.Z+1) with its own CHANGELOG section.
- **PyPI has a bad release**: yank it on PyPI (with a reason) and release
  the next patch version. Use a post release (`X.Y.Z.post1`) only for a
  packaging or metadata fix with no code change.
- **github-release fails**: re-run the job, or create the release by hand
  from the run's `dist` and `release-notes` artifacts:
  `gh release create vX.Y.Z dist/* --verify-tag --title vX.Y.Z --notes-file release-notes.md`
  (add `--prerelease` for an a/b/rc/dev version).
