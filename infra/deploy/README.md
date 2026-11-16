# Single-host cloud deployment

These scripts roll out the existing Docker API image to **one preprovisioned Linux
VM** using AWS Systems Manager or Azure Run Command. They do not provision a VM,
disk, registry, database, certificate, reverse proxy or identity. All identifiers
in the examples are placeholders. The scripts have offline tests and syntax
checks; they have not been executed against a live cloud account.

## Host contract

| Requirement | Configuration |
|---|---|
| Compute | One x86-64 Linux EC2 instance or Azure VM, with a supported Docker Engine and Python 3. |
| Management | SSM Agent and scoped instance profile on AWS; Azure VM Agent and system-assigned managed identity on Azure. No inbound SSH is required by these scripts. |
| Disk | Encrypted EBS or persistent Azure managed disk mounted locally, with ext4 or XFS. Keep the disk across instance replacement according to the recovery plan. |
| State | `/var/lib/datamigration/state`, UID/GID `10001:10001`, mode `0700`; bind-mounted as `/state`. Only one API process/container operates this workspace. |
| Runtime configuration | `/etc/datamigration/runtime.env`, root-owned and mode `0600`; supplied to Docker without logging its content. |
| Ingress | Docker publishes only `127.0.0.1:8000`. Provision a TLS reverse proxy and the intended authenticated/private ingress separately. |
| Tools | `docker`, `curl`, `python3`, `flock`, `findmnt`, `stat`, Bash and standard GNU utilities; AWS CLI or Azure CLI on its corresponding host. |
| Registry | Existing ECR in the configured region, or existing commercial Azure ACR. Pull identity has read access only. |
| Capacity | The sample container limit is 2 CPUs and 2 GiB. Size the VM and workload budgets with representative migration data before use. |

The filesystem check rejects non-ext4/XFS mounts; platform administrators must
also verify that the underlying block storage is persistent and encrypted. This
check alone cannot attest cloud disk encryption. Do not substitute EFS, Azure
Files, NFS, SMB or an ephemeral VM resource disk. SQLite's locking and I/O
assumptions make network filesystem deployments unsuitable for this workbench;
see [SQLite's network filesystem guidance](https://sqlite.org/useovernet.html).

The deployment image runs as UID 10001 with a read-only root filesystem, a small
temporary `/tmp`, dropped Linux capabilities and no-new-privileges. The host is
still a privileged security boundary: SSM/Run Command and Docker access can read
runtime configuration and control the service. Restrict both to authorized
operators and the protected deployment identity.

## Networking and identities

On AWS, place the instance in the intended private subnet and permit management
and registry egress through the reviewed NAT/VPC endpoint design. Its role needs
SSM managed-node permissions and ECR image-read permissions. If the application
uses instance credentials, configure IMDSv2 and the hop limit/network path needed
for the container, and tightly scope its native engine permissions. The GitHub
deployment role is separate and can send/poll a command only for the selected
host. See [Run Command](https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command.html).

On Azure, enable the VM Agent and system-assigned managed identity. Give that
identity read access to the registry using the role appropriate for the ACR
permissions mode. The host executes `az login --identity` with a temporary Azure
CLI configuration directory and `az acr login`; temporary registry credentials
are removed at script exit. Permit the required outbound VM Agent and registry
endpoints. See [Linux Run Command](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/run-command)
and [managed identity registry authentication](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication-managed-identity).

For either cloud, keep database ports private and limit source reads/destination
writes to the intended network and service identities. A private ALB/Application
Gateway can reach a host TLS reverse proxy, which forwards locally to port 8000;
it cannot reach the loopback-only Docker port directly. Enforce the API's bearer
token/RBAC contract and any additional organization SSO at the chosen gateway.
The repository does not supply an OIDC-to-application-role gateway integration.
Do not open port 8000 or a Docker socket to the network.

## Runtime configuration

Use the approved Secrets Manager/Key Vault/host configuration process to create
`/etc/datamigration/runtime.env`. At minimum it must configure the API's documented
authentication settings; the compatibility admin token `MIGRATION_API_TOKEN` must
have at least 24 characters. Use the role-separated token configuration from the
API guide for a shared pilot. Set source/target URL environment variables only
for approved connectors. Credentials belong here or in workload identity/secret
references, never in migration manifests, repository files, CI arguments or logs.

The script forces `MIGRATION_STATE_DIR=/state`. Install this release's chosen
connector extras in the image before deployment; a registry pull does not install
database drivers. ODBC/native drivers require corresponding image packages.
The same applies to local model/agent tool configuration.

Create the local disk mount and protected directories through the host setup
process. Do not format or replace an existing disk in a deployment job. Back up
the complete workspace, including checkpoints, operation receipts and submission
ledgers. Validate a restore on an isolated host. A missing ledger can lead to
duplicate external operations, so the adapters deliberately refuse to treat
unknown submissions as safe new work.

## Rollout behavior

1. Validate the digest, registry, region, required tools, local filesystem and
   runtime configuration permissions; acquire a host deployment file lock.
2. Authenticate using the host identity and pull the image before maintenance.
3. Require the existing service to be healthy and outside maintenance. Refuse to
   overwrite an existing maintenance session or an unexpected second container.
4. Run `datamigrate maintenance on --workspace /state`. The CLI takes the same
   submission lock used by the API/CLI and rejects new operations atomically.
5. Poll `/health` until `draining=true` and `active_runs=0`. After ten minutes,
   abort the replacement and keep the current service. A crashed operation with
   a `RUNNING` receipt conservatively blocks deployment until investigated.
6. Stop the drained current container, retain it as `datamigration-previous`,
   and start the reviewed image using the same local workspace.
7. Verify API health and a closed, idle gate. Record the prepared image digest,
   then open the gate. Emit an exact success marker only after this succeeds.

`active_runs` covers executing control-plane invocations; it does not wait for a
long-running DMS/ADF/Glue/Databricks operation to finish. Their durable operation
IDs and provider receipts remain in the workspace and native engine. Review any
application update that changes those receipt/checkpoint contracts.

Failures before opening the gate try to restore the previous container with the
same state. The script never restores an old database snapshot automatically.
If unpause has been attempted, the response can be uncertain and new work can
start: the script leaves that service running and requires inspection. Old
versions must be compatible with current checkpoint/ledger formats for image
rollback to be valid. Plan state schema changes and backups separately.

An SSM or Run Command timeout, runner cancellation or lost response leaves an
**unknown remote outcome**. Inspect the command and host before repeating it;
do not start a second host against the disk. Azure command provisioning success
alone is insufficient, so the wrapper also checks the final application-health
marker. The scripts suppress raw command output in CI; authorized operators can
inspect restricted cloud/host logs when needed.

## Invocation

The preferred path is the protected GitHub workflow after the
[CI/CD configuration](../../docs/cicd.md) is complete. For an explicitly approved
operator deployment, the same wrappers accept an immutable image:

```bash
python infra/deploy/aws_ssm.py \
  --instance-id i-0123456789abcdef0 --region eu-west-1 \
  --image 123456789012.dkr.ecr.eu-west-1.amazonaws.com/datamigration@sha256:REPLACE_WITH_64_HEX_DIGEST

python infra/deploy/azure_vm.py \
  --resource-group migration-rg --vm-name migration-pilot --acr-name migrationregistry \
  --image migrationregistry.azurecr.io/datamigration@sha256:REPLACE_WITH_64_HEX_DIGEST
```

The placeholders intentionally fail validation. Provisioned native workers,
approved migration plans, source snapshot/CDC setup, reconciliation and cutover
are independent of this application rollout.

## Future container platforms

ECS Fargate and Azure Container Apps are future deployment targets. First replace
local SQLite checkpoints, submission ledgers, workspace state files and file locks
with a coherent shared-state implementation, distributed fencing/maintenance and
durable artifact storage. A PostgreSQL checkpointer alone is not sufficient while
the other state and locks remain local. No EFS/Azure Files workaround or
multi-replica claim is included in this release.
