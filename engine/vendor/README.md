# Vendored modules

Pure-Python modules the release's own scripts import, kept here because the KiroCrew desktop app installs no packages
for an App (its bundled interpreter is inside the signed application bundle). `direct.aws.with_vendor` puts this
directory on the `PYTHONPATH` of those scripts.

| Module | Version | License | Upstream | Used by |
|---|---|---|---|---|
| `retrying.py` | 1.4.2, unchanged | Apache-2.0 (`retrying-LICENSE.txt`, `retrying-NOTICE.txt`) | https://github.com/groodt/retrying | the release's `knowledge-base/create_kb.py` (direct rehearsals: create and delete the knowledge base) |
