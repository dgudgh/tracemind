# Engine package trust lifecycle (0.3 local contract)

Validated on 2026-10-27 against synthetic fixture keys. This is an offline verifier implementation and public handoff contract. It does not record an OPS/SEC production ceremony or E07 acceptance.

## Boundaries and bootstrap

- Engine metadata uses `tmengine.test` or `tmengine.production`; channels are respectively `test` and `production`. Neither namespace inherits the platform release/patch keys. Metadata signatures and package signatures are Ed25519 over canonical UTF-8 JSON (`sort_keys`, compact separators, no NaN, duplicate JSON keys refused).
- A client bootstraps only with an explicit root envelope **and a SHA-256 of its canonical envelope independently verified out of band**. Version must be 1, root's own signature threshold must pass, and the chosen namespace is fixed in its private local ledger. Importing a bundle cannot create a root. `fixture_only=true` is sticky in local status and does not represent production acceptance.
- The root defines public keys and role thresholds for `root`, `targets`, `snapshot`, `timestamp`, and `revocations`. Root version updates are consecutive (`n+1`) and each new root must satisfy both the preceding and incoming root quorums. Skipping a version and replacing a root with just a higher generation are rejected.
- Production root/HSM or equivalent custody and recovery material belong to OPS, with independent SEC approval. REL supplies the already tested payload digest, source/SBOM and signing request. Development, CI, this CLI, package archives, and this repository store **public** material only. Root threshold compromise freezes automatic updates and requires prearranged out-of-band rebootstrap, digest inventory, and independent approval; a larger signed version cannot repair trust.

## Wire format and checks

Every metadata envelope is `{ "signed": {...}, "signatures": [{"key_id": "tmengine.test:<sha256 of raw public bytes>", "value": "<base64 Ed25519 signature>"}] }`. The signed object always has `role`, `namespace`, positive integer `version`, and UTC `expires` (`YYYY-MM-DDTHH:MM:SSZ`). Every signature covers only its complete `signed` object. Files are capped at 1 MiB, nesting at 32, keys/entries at stated implementation limits. Unknown fields are rejected for all metadata roles.

- `root.signed`: `keys` maps Key ID to base64 32-byte public key; `roles` maps the five roles to `{key_ids, threshold}`.
- `targets.signed`: `release_keys` maps Key ID to `{public_key,publisher,engine_scopes,purpose,channel}` and `packages` contains exact `{digest,signing_key_id,publisher,engine_id,purpose,package_kind}` records. Purpose is always `tmengine.manifest-signing.v1`, package kind `tmengine`. Release key and root metadata key reuse is rejected. Publisher and engine wildcards are not accepted.
- `revocations.signed`: `key_ids`, `digests`, and a nonempty `reason`. It is root-delegated and recorded as cumulative sets. Later metadata cannot clear an earlier revocation. Key revocation here governs package release keys; root threshold compromise requires the out-of-band process above.
- `snapshot.signed.meta`: `{targets_version,targets_digest,revocations_version,revocations_digest}`. `timestamp.signed.snapshot`: `{version,digest}`. Digests are SHA-256 of canonical whole envelopes including signatures. Each role is verified against the current root before version/binding checks. Same-version equivocation and version rollback are rejected; timestamp version must strictly increase per update.

One `TrustStore.update` transaction validates the full bundle at a fixed start time and commits root, metadata, cumulative revocations and `last_seen` together under a cross-process lock and SQLite FULL-sync transaction. A crash before commit leaves the prior complete state. Every authorization verifies the stored chain again, checks expiry against one UTC `now`, refuses a clock earlier than the persisted `last_seen`, and writes the new `last_seen`. The host must establish a trusted local clock and protected state/backup baseline. Rolling back or replacing the entire local database by an OS administrator is outside this module's monotonic guarantee; an external monotonic checkpoint or protected restore procedure is required for production.

`authorize_package(manifest, package_digest, now, rollback=False)` only returns the current scoped `TrustedEngineKey` when the manifest canonical digest exactly matches a current target entry, publisher/engine/purpose/namespace/channel match, and neither key nor digest has ever been revoked. A former package must be freshly present in current targets; its old signature or a package self-reported signing time does not reauthorize it. The caller must then verify its detached manifest signature and complete archive bytes using that returned key. Use `guard_package(...)` as a context manager around archive verification and the installer/activation durable commit to hold the trust lock so a concurrent revocation cannot overtake approval; acquire repository lock first consistently. Authorization must run again at installation, activation, and rollback. `rollback=True` does not relax any trust rule; compatibility, retention, P/P-1, S/S-1 and OS/ABI gates remain separate callers' duties. No safe candidate means unavailable/blocked.

## Public production handoff record (template)

```text
handoff_id: <external controlled reference>
namespace: tmengine.production
root_version: <integer>
root_envelope_sha256: sha256:<64 hex>
root_public_key_ids_and_fingerprints: <public only>
root_threshold: <m-of-n>
targets/snapshot/timestamp/revocations_role_key_ids_and_thresholds: <public only>
release_key_ids_and_scopes: <publisher, engine IDs, purpose, channel>
tested_payload_digest_and_test_receipt_refs: <immutable refs>
production_envelope_digest_and_verifier_receipt: <immutable refs>
pre/post envelope payload_digest_equal: <true with receipt>
controlled_verify_install_smoke_receipt: <immutable ref>
OPS_custody_and_recovery_proof_ref: <controlled ref, no secret>
SEC_independent_approval_ref: <controlled ref>
REL_signing_request_ref: <controlled ref>
effective_time_and_clock_baseline_ref: <UTC and controlled ref>
revocation_and_root_compromise_recovery_runbook_ref: <controlled ref>
E07_status: waiting_external | production-trust-accepted
```

E07 may become `production-trust-accepted` only after real OPS custody, independent SEC approval, exact tested payload production wrapping, production verifier, byte-digest equality, and controlled verify/install smoke. A synthetic `tmengine.production` fixture remains `fixture_only` and cannot sign E07.

## Native first-install and separate package purposes

The native platform assembly must carry the **public** production root envelope, independently approved root digest, verifier code, namespace policy, and initial metadata bundle as controlled immutable inputs. The platform installer verifies its own platform package using the platform release purpose, then separately bootstraps the engine trust ledger and verifies an engine archive with engine metadata and `tmengine.manifest-signing.v1`. The two signature stores, scopes, state directories and receipt identities are separate. A missing, expired or unendorsed engine bundle fails engine install explicitly; it cannot be silently accepted because the enclosing platform installer was trusted. Test-root first installs remain test channel only. This document is an assembly contract; actual native packaging and first-install smoke need their own executable evidence.

## Builder/release key incident response

Freeze new signing and installation, identify affected key IDs and exact digests across catalog, installed packages, active generations and pinned tasks, and publish root-authorized revocation metadata with a strictly newer timestamp. Quarantine unconfirmed packages; stop or reconcile side-effecting tasks at their effect boundary. Rebuild in a clean builder, inspect source/toolchain/SBOM/opaque bytes, assign a new release key, rerun qualification, and reendorse exact digests in current targets. Existing signed packages do not regain authority merely because they have a known version or historical signature. Root quorum compromise triggers out-of-band rebootstrap instead of this online flow.

## Reproducible local check

```bash
.venv-mac-primary/bin/python -m pytest -q backend/tests/test_engine_trust_metadata.py
.venv-mac-primary/bin/python scripts/engine_trust_cli.py --state-dir /private/controlled/engine-trust --namespace tmengine.test status
```

The CLI has explicit `bootstrap`, `update`, `authorize`, and `status` commands; it never generates or reads private keys. `authorize` only selects a key and prints a warning that the package signature and archive bytes still require verification. This local test suite creates ephemeral fixture keys in process and does not access a database server, SSH target, model, global key store, or production private key.
