# Cloud infrastructure examples

The examples use the proposed kdc API to create a VM, private managed PostgreSQL,
and TCP 5432 access between them. The repository currently contains designs and
schemas, so these are design fixtures. Running them requires a compatible kdc
controller and its CRD, plus the cloud provider controllers and credentials.

| Cloud | Controller | VM address | Database networking | Access rule |
| --- | --- | --- | --- | --- |
| [GCP](../examples/gcp/) | Config Connector | Reserved internal `10.20.1.10` | Cloud SQL PSC endpoint `10.20.2.10` in a separate subnet | VM-tagged egress allow, followed by network-wide endpoint deny |
| [AWS](../examples/aws/) | ACK EC2 and RDS | Fixed private `10.30.1.10` | Private RDS subnet group with two database subnets | Database security group allows the VM's `/32` |
| [Azure](../examples/azure/) | Azure Service Operator | Static private NIC `10.40.1.10` | Flexible Server in a separate delegated subnet | Database subnet NSG allows the VM's `/32` |

VMs have no public IPs. GCP has a standalone internal address reservation; AWS's
fixed address belongs to the instance's primary network interface, and Azure's
static address belongs to its NIC. The latter two are not independent address
reservation resources. The AWS fixed-address approach was selected instead of
an Elastic IP requiring an external association step.

Each cloud has three files containing only kdc wrappers: `network.yaml` holds
networks, subnets, firewall rules, and shared DNS or resource-group infrastructure;
`vm.yaml` holds the VM and its address or NIC; `database.yaml` holds the database,
its endpoint or subnet group, users, and connection outputs. Each `settings.yaml` is an
ordinary ConfigMap holding nonsecret configuration. Wrappers reference actual
provider targets, use explicit readiness predicates, and have stable names using
the naming algorithm in the API design. The final `kdc-<cloud>-connection`
ConfigMap exposes the database host, port, database name, username, and SSL mode.
It contains no password and uses target readiness `None`, because ConfigMaps
do not report a readiness condition.

## Preparing an example

1. Choose a cloud and install its controllers and the CRD versions used in its
   manifests. Configure provider credentials and resource permissions.
2. Create the corresponding namespace: `kdc-example-gcp`, `kdc-example-aws`, or
   `kdc-example-azure`. Configure kdc's allowed target kinds and source kinds.
3. Edit that cloud's `settings.yaml`, replacing every marked value. Select an
   available regional VM image, machine size, and database version as necessary.
4. Create a `database-credentials` Secret in that namespace with a `password`
   key. Use a password accepted by the selected managed database. For example,
   provide it from an existing protected file:

   ```sh
   kubectl -n kdc-example-gcp create secret generic database-credentials \
     --from-file=password=/path/to/password-file
   ```

   Use a file without a trailing newline. Substitute the AWS or Azure namespace
   when using those examples. Provider controllers read this Secret directly;
   kdc neither resolves nor copies the password into the wrapper or its outputs.
5. With a compatible kdc implementation installed, apply settings before wrappers:

   ```sh
   kubectl apply -f examples/gcp/settings.yaml
   kubectl apply -f examples/gcp/network.yaml \
     -f examples/gcp/vm.yaml \
     -f examples/gcp/database.yaml
   ```

   Substitute `aws` or `azure` for the other clouds. Applying all wrappers together
   is supported by the proposed dependency model. Follow each provider target's
   status while prerequisites reconcile.

No guest applications or PostgreSQL client packages are installed. Database
reachability must be tested from the VM using your chosen access method and a
PostgreSQL client. VMs have no inbound SSH rule or internet egress path in these
examples. Use an existing private administration path or a provider console
facility; additional access infrastructure is outside these examples.

All wrappers use `deletionPolicy: abandon`. Deleting them retains their targets
and cloud resources. Change policy deliberately when testing cleanup; inspect
provider deletion behavior and remove dependents before their prerequisites.

## GCP details

Use an existing project with Compute Engine and Cloud SQL Admin APIs enabled.
Set `projectId` and a unique `databaseInstanceName` in
[`examples/gcp/settings.yaml`](../examples/gcp/settings.yaml). Resources use
`europe-west4`; the VM uses `europe-west4-a`.

The VPC has VM subnet `10.20.1.0/24` and database endpoint subnet `10.20.2.0/24`.
Cloud SQL itself runs in Google's managed infrastructure. Private Service Connect
places its consumer endpoint in the database subnet of the same VPC as the VM.
This distinguishes the endpoint's location from the managed server's location.
The instance has public IPv4 disabled, PSC enabled, and the project allowlisted.
The endpoint must report `pscConnectionStatus: ACCEPTED` as well as `Ready=True`.
See [Cloud SQL PSC networking](https://docs.cloud.google.com/sql/docs/postgres/configure-private-service-connect).

An egress allow at priority 1000 permits VMs tagged `kdc-postgres-client` to reach
the endpoint's `/32` on TCP 5432. A network-wide egress deny at priority 2000
blocks other VM access to that endpoint, overriding implied egress allow. Treat
the client tag as privileged when granting VM management permissions.

The `SQLUser` reads the password Secret for `appuser`. Connect to the endpoint IP
and the existing `postgres` database. TLS is enforced on Cloud SQL and the output
requests `sslmode=require`; configure CA verification for your application.
Readiness uses Config Connector's `Ready` condition and requires its observed
generation to match.

## AWS details

Configure both ACK controllers for `eu-west-1`. Set a valid x86-64 AMI ID for that
region in [`examples/aws/settings.yaml`](../examples/aws/settings.yaml). The
example uses `t3.micro`, and RDS uses the region's default PostgreSQL engine
version unless you add an explicit supported `engineVersion`.

VPC `10.30.0.0/16` contains VM subnet `10.30.1.0/24` and database subnets
`10.30.2.0/24` in `eu-west-1a` and `10.30.3.0/24` in `eu-west-1b`. RDS requires a
DB subnet group spanning at least two availability zones even for the example's
single-AZ instance. The VM subnet is excluded from that group. There is no
internet gateway, NAT gateway, or public address, and RDS has
`publiclyAccessible: false`. See [RDS VPC requirements](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html).

The VM security group allows outbound TCP 5432 to the database subnets. The
database security group permits inbound TCP 5432 only from the VM's fixed private
IP, selected from the referenced Instance spec. Return traffic uses stateful
security-group behavior. Connect using the RDS DNS endpoint, which can change its
resolved private IP during maintenance or failover. The instance creates `appdb`
and uses `appuser` with the password Secret.

Readiness uses `ACK.ResourceSynced`; the VM must also report state `running` and
RDS must report `dbInstanceStatus: available`. `observedGeneration: IfPresent`
checks generations only when the controller publishes them. These ACK resources
do not provide the same strict observed-generation contract as KCC and ASO.

## Azure details

Install ASO CRDs for `resources.azure.com`, `network.azure.com`,
`compute.azure.com`, and `dbforpostgresql.azure.com`. Set a globally unique server
name and your SSH RSA public key in
[`examples/azure/settings.yaml`](../examples/azure/settings.yaml). The examples
use `westeurope`, a resource group owned by the example, and ASO API versions
available in the v2.13.0 release.

VNet `10.40.0.0/16` contains VM subnet `10.40.1.0/24` and PostgreSQL subnet
`10.40.2.0/24`. The database subnet is delegated to
`Microsoft.DBforPostgreSQL/flexibleServers`. The server uses private VNet
integration with `publicNetworkAccess: Disabled`. Its private DNS zone is linked
to the VNet so the VM resolves the server's FQDN to its internal address.

An NSG attached to the database subnet permits the VM's `/32` on TCP 5432 at
priority 100. A priority-110 rule permits intra-database-subnet TCP 5432 for managed
service operation. Priority 200 denies other inbound TCP 5432, overriding the
default VNet allow rule. The default outbound rules remain available for DNS and
service traffic, and the subnet declares a Microsoft.Storage service endpoint.
See [PostgreSQL private networking requirements](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private).

If you change the NIC's `10.40.1.10` address, update the allow rule's matching
`10.40.1.10/32` source. The allow rule waits for the NIC, avoiding a dependency
cycle between VM and database provisioning. ASO readiness requires `Ready=True`
for the current generation. Connect to the server FQDN and the existing
`postgres` database using `appuser` and the password Secret.

## Local validation

Run the included wrapper and graph checks from the repository root:

```sh
python3 -m venv /tmp/kdc-example-validation
/tmp/kdc-example-validation/bin/pip install 'jsonschema[format]' PyYAML
/tmp/kdc-example-validation/bin/python examples/validate.py
```

The validator rejects duplicate YAML keys, validates every wrapper against the
resource schema, verifies stable wrapper names and declared aliases, resolves
references within each example, and rejects reference cycles.

Provider specs were also checked against official CRD schemas with sample
resolved reference values and unknown fields rejected. That check verifies field
structure, types, and enums; it does not establish cloud availability, provider
admission success, permissions, live readiness, or end-to-end connectivity.
No cloud resources were deployed during validation.

Provider schema sources inspected:

- [Config Connector CRDs at e705256](https://github.com/GoogleCloudPlatform/k8s-config-connector/tree/e7052568ddaea1cfdc7ba2ef0809dc581bcc2af5/config/crds/resources).
- [ACK EC2 CRDs at 00301e9](https://github.com/aws-controllers-k8s/ec2-controller/tree/00301e92b4450dd3c8a7e60bc02c31007776267f/config/crd/bases).
- [ACK RDS CRDs at ce8970e](https://github.com/aws-controllers-k8s/rds-controller/tree/ce8970e92f1dc150e3d4b5076efeebdf35e8d936/config/crd/bases).
- [ASO v2.13.0 released CRDs](https://github.com/Azure/azure-service-operator/releases/download/v2.13.0/azureserviceoperator_customresourcedefinitions_v2.13.0.yaml).
