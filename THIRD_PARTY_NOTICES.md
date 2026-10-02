# Third-party notices

The App is licensed under MIT No Attribution (see `LICENSE`).

## Bundled Workshop template

`upstream/` is a copy of **sample-eval-first-building-enterprise-agents-with-agentcore** by Amazon.com, Inc. or its affiliates, licensed under
**MIT No Attribution (MIT-0)**. The full licence text is `upstream/LICENSE`.

- Upstream project: https://github.com/aws-samples/sample-eval-first-building-enterprise-agents-with-agentcore
- Vendored commit: `245092299e97219e53cc6645d8a7b397e4e7222a` (Merge pull request #6 from phoenixyy/fix/english-doc-proofreading)
- The copy is unchanged; `template-lock.json` pins it, and the engine renders scenario packs onto it.

## Python packages

`app/requirements.txt` lists what KiroCrew installs for the App's backend: boto3 and botocore (Apache-2.0),
jsonschema (MIT), PyYAML (MIT), retrying (Apache-2.0). They are installed from PyPI and are not part of this repository.
