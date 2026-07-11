# Deployment: Fargate + RDS

> ## Status: **written and validated, never applied.**
>
> `terraform validate` and `terraform fmt -check` pass. `terraform plan` has **never been run**,
> because a plan needs credentials and there is no AWS account behind this. Nothing here has ever
> created a resource.
>
> It is in the repo because *"how would you deploy this?"* deserves an answer somebody can review line
> by line, and because a README claiming a deployment that never happened would be the one thing in
> this project that wasn't true.

## What it builds

```
  provider ──HTTPS──▶ ALB (public subnets)
                       │  health check: /readyz
                       ▼
                    app tasks (private, no public IP, autoscaled on CPU 2→8)
                       │
                       ▼
                    RDS Postgres 16 (private, encrypted, no public access)
                       ▲
                       │
                    worker tasks (private, no ingress at all, no load balancer)
```

One image, three commands — the same shape as `docker-compose.yml`:

| | command |
|---|---|
| `app` | `uvicorn ... --factory` (image default) |
| `worker` | `python -m webhook_receiver.worker.main` |
| `migrate` | `alembic upgrade head` — a **one-shot task**, run to completion *before* the services update |

One image means the worker cannot drift from the code that was tested, and a rollback is one tag
rather than two.

## The decisions worth reviewing

**Migrations are a separate task, not an app startup step.** Two app tasks booting at once would both
run `alembic upgrade head` against the same database, and two concurrent migrations is a real way to
corrupt a schema. The pipeline runs the task, waits for it to exit 0, *then* updates the services.

**The container healthcheck probes `/healthz`; the load balancer probes `/readyz`.** These answer
different questions and must not be conflated. The container runtime is asking *"is this process
alive?"* — and if that answer depended on the database, a ten-second RDS blip would kill and restart
the entire fleet. The load balancer is asking *"should I send this task traffic?"* — and there the
answer absolutely is no if it cannot reach the database.

**The worker's healthcheck had to be overridden.** The image's default probes `:8000`, which is the
*app's* port; the worker never listens there. Leaving it would mean ECS kills every worker, forever.
(This is not hypothetical — it is exactly what happened in `docker-compose` until `--wait` finally
asked the worker whether it was well.)

**No NAT gateway.** ~$32/month per AZ before a byte moves, and the tasks only need to reach AWS
itself — ECR, CloudWatch, Secrets Manager. VPC endpoints do that privately, cost less, and mean a
task has **no route to the internet at all**: a compromised container cannot phone home. The moment a
handler needs to call a third-party API this must be revisited. Today none does.

**`/metrics` is 404'd at the load balancer.** It is unauthenticated by design (a Prometheus scraper
carries no admin key), so it must not be publicly routable. Blocked at the edge rather than in the
app, because an application-level check is one refactor away from being bypassed.

**Secrets are ECS `secrets`, not `environment`.** An `environment` value is visible in the task
definition to anyone with `ecs:DescribeTaskDefinition`. And `WEBHOOK_SECRETS` is created **empty** —
Terraform state is plaintext, so a signing key written into a `.tf` is a signing key in every state
backup and every CI log that ever printed a plan. Populate it out of band:

```bash
aws secretsmanager put-secret-value \
  --secret-id webhook-receiver-prod/webhook-secrets \
  --secret-string '{"stripe":"whsec_..."}'
```

**The app autoscales on CPU at a 60% target, not 90%.** The load test showed latency degrading
sharply once the process saturates (`docs/load-test.md`) — by the time CPU reads 90%, the p99 has
already gone. And it scales the *tier*, not the process: `uvicorn --workers` would be cheaper but
`prometheus_client` keeps a per-process registry, so a multi-worker container would under-report its
own metrics by the worker count.

**The task role has no policies.** The service talks to Postgres and to nothing else in AWS. An empty
role is the smallest blast radius there is, and adding a permission should require someone to justify
it in a diff.

## What it costs

Roughly, `eu-west-1`, at rest with no traffic:

| | ~$/month |
|---|---|
| ALB | 18 |
| Fargate: 2 app (0.5 vCPU) + 2 worker (0.25 vCPU) | 35 |
| RDS `db.t4g.micro`, single-AZ, 20 GB gp3 | 15 |
| VPC interface endpoints (4 × 2 AZs) | 58 |
| **≈** | **≈ $125** |

`db_multi_az = true` roughly doubles the RDS line. It is a **cost** decision rather than a
correctness one, and that is not an accident: an RDS failover surfaces as SQLSTATE `57P01`, which is
in `RETRYABLE_SQLSTATES` (ADR-0005), so the events in flight are rescheduled with backoff instead of
dead-lettered. A failover costs a retry, not a backlog.

The interface endpoints are the surprise on that bill — they cost more than the compute. Swapping
them for a single NAT gateway (~$32) is cheaper *and* worse: it gives every task a route to the
internet.

## What is missing, and I'd rather say so

- **Never applied.** No `plan`, no `apply`, no drift, no idea what AWS would actually complain about.
  A first `apply` always finds something.
- **State is local.** A real deployment moves it to S3 + DynamoDB locking on day one; the backend
  block is written and commented out. Local state means one laptop's `apply` can silently clobber
  another's.
- **`certificate_arn` has no default.** Providers will not send webhooks to plain HTTP, and neither
  should we accept them, so an ACM certificate has to exist first.
- **No worker autoscaling.** Scaling on queue depth needs a custom CloudWatch metric published from
  `fn_queue_lag()` or `v_queue_health`. The worker tier scales fine — `SKIP LOCKED` and the advisory
  locks mean `worker_count` can be raised with no other change — it just does not do it *by itself*.
- **No WAF, no rate limiting.** Signature verification is the real gate (an unsigned request is a
  401 before it touches the database), but a flood of *unsigned* requests still costs CPU.
- **No alarms.** The metrics exist (FR-19) and `fn_queue_lag()` is the number to page on; nothing
  wires them to a pager yet.

## Running it, if there were an account

```bash
cd infra
terraform init
terraform plan  -var="certificate_arn=arn:aws:acm:..." -var="image_tag=v0.1.0"
terraform apply -var="certificate_arn=arn:aws:acm:..." -var="image_tag=v0.1.0"

# Build and push the image
docker build -t "$(terraform output -raw ecr_repository):v0.1.0" ..
docker push "$(terraform output -raw ecr_repository):v0.1.0"

# Migrate FIRST, to completion
aws ecs run-task --cluster webhook-receiver-prod \
  --task-definition "$(terraform output -raw migrate_task_definition)" \
  --launch-type FARGATE --network-configuration '...'

# Then the signing keys, out of band
aws secretsmanager put-secret-value --secret-id ... --secret-string '{"stripe":"whsec_..."}'
```
