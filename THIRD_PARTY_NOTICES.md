# Third-party notices

The App is licensed under MIT No Attribution (see `LICENSE`).

## Bundled Workshop template

`upstream/` is a copy of **sample-eval-first-building-enterprise-agents-with-agentcore** by Amazon.com, Inc. or its affiliates, licensed under
**MIT No Attribution (MIT-0)**. The full licence text is `upstream/LICENSE`.

- Upstream project: https://github.com/aws-samples/sample-eval-first-building-enterprise-agents-with-agentcore
- Vendored commit: `245092299e97219e53cc6645d8a7b397e4e7222a` (Merge pull request #6 from phoenixyy/fix/english-doc-proofreading)
- The copy is unchanged; `template-lock.json` pins it, and the engine renders scenario packs onto it.

## Vendored Python module

`engine/vendor/retrying.py` is **retrying 1.4.2** by Ray Holder and contributors, unchanged, licensed under the
**Apache License 2.0** (`engine/vendor/retrying-LICENSE.txt`, `engine/vendor/retrying-NOTICE.txt`;
https://github.com/groodt/retrying). The release's `knowledge-base/create_kb.py` imports it.

## Python packages used from KiroCrew

boto3 and botocore (Apache-2.0), jsonschema (MIT) and PyYAML (MIT) come with KiroCrew's own Python; they are not part
of this repository.
