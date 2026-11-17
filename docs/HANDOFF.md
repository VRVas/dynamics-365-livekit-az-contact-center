# Deployment handover

Start with [the modular deployment guide](deployment.md). The repository is a
neutral template; environment-specific handover material belongs outside Git.

For each deployment retain:

- Reviewed source revision, prepared file hashes and selected modules.
- Private profile, generated content/data approval and customization evidence.
- Azure/Dataverse targets, built images and immutable component receipts.
- Credential-store locations and rotation owners, without secret values.
- Separate configuration, runtime-identity, UI and caller-acceptance results.
- Open human gates, uncertain operations, limits and a scoped rollback plan.

Use the prepared workspace's `template_setup.py status` command to resume.
Read current state and the original component journals before another write.
A coordination record is not an ownership receipt or proof of a successful call.
