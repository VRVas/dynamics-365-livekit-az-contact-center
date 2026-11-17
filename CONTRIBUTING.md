# Contributing

This project welcomes contributions and suggestions. Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Reporting issues

Search the existing issues first. For a deployment problem, include:

- the selected modules and the deployment stage;
- the command you ran and its error output;
- the readback or status output that shows the state after the failure.

Don't post secrets, tenant or resource IDs, phone numbers, customer data, or screenshots
that show them. Report security vulnerabilities as described in [SECURITY.md](SECURITY.md),
not in a public issue.

## Pull requests

- Keep the template neutral. Don't add customer names, branding, records, phone numbers,
  or tenant or resource IDs; use the placeholders in the existing examples.
- Add or update tests for the behavior you change, and run the suites for the apps you
  touched plus the infrastructure checks:

  ```bash
  python -m pip install -r infra/scripts/requirements.txt
  PYTHONPATH=infra/scripts python -m unittest discover -s infra/tests
  python infra/scripts/check_template.py
  ```

- If you change a deployment step, update [the deployment guide](docs/deployment.md) and the
  deployment skills in [.github/skills](.github/skills).
