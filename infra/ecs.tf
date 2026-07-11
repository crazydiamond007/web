# The three things that run: app, worker, and a one-shot migration task.
#
# All three are the SAME image with different commands, exactly as in
# docker-compose. One image means the worker cannot drift from the code that was
# tested, and a rollback is one tag rather than two.

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE" # a tag that moves makes rollback a guess

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "main" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

# --- IAM ---------------------------------------------------------------------

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  # Used by the ECS *agent* to pull the image and fetch secrets -- before any of
  # our code runs. Kept separate from the task role so the application itself
  # never holds permission to read the secrets it was handed.
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.webhook_secrets.arn,
      aws_secretsmanager_secret.admin_api_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${local.name}-read-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_role" "task" {
  # The role the application code runs as. It has NO policies attached, and that
  # is correct: the service talks to Postgres and to nothing else in AWS. An empty
  # role is the smallest possible blast radius, and adding a permission should
  # require someone to justify it here.
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

# --- Shared container config -------------------------------------------------

locals {
  secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "WEBHOOK_SECRETS", valueFrom = aws_secretsmanager_secret.webhook_secrets.arn },
    { name = "ADMIN_API_KEY", valueFrom = aws_secretsmanager_secret.admin_api_key.arn },
  ]

  environment = [
    { name = "ENVIRONMENT", value = var.environment },
    { name = "LOG_LEVEL", value = "INFO" },
  ]

  log_config = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.main.name
      "awslogs-region"        = var.region
      "awslogs-stream-prefix" = "ecs"
    }
  }
}

# --- Migration: a one-shot task, never an app startup step --------------------
#
# Registered here and RUN BY THE PIPELINE before the services are updated:
#
#   aws ecs run-task --cluster ... --task-definition ...-migrate --launch-type FARGATE
#
# Not an entrypoint step, and not a sidecar. Two app tasks starting at once would
# both run `alembic upgrade head` against the same database, and two concurrent
# migrations is a real way to corrupt a schema. One task, to completion, before
# anything else starts -- the same reason `migrate` is its own service in
# docker-compose.

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name             = "migrate"
    image            = local.image
    command          = ["alembic", "upgrade", "head"]
    essential        = true
    secrets          = local.secrets
    environment      = local.environment
    logConfiguration = local.log_config
  }])
}

# --- App ---------------------------------------------------------------------

resource "aws_ecs_task_definition" "app" {
  family                   = "${local.name}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.app_cpu
  memory                   = var.app_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "app"
    image       = local.image
    essential   = true
    secrets     = local.secrets
    environment = local.environment

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    # /healthz, never /readyz. A container that reports itself unhealthy on a
    # database blip gets KILLED; one that reports itself unready gets drained.
    # Wiring liveness to the database means a brief RDS hiccup restarts the whole
    # fleet -- turning a ten-second blip into an outage.
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)\""]
      interval    = 10
      timeout     = 3
      retries     = 3
      startPeriod = 10
    }

    logConfiguration = local.log_config
  }])
}

resource "aws_ecs_service" "app" {
  name            = "${local.name}-app"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.app_min_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
    # No public IP. The image comes from the ECR endpoint, not the internet.
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8000
  }

  # The ALB uses /readyz, so a task is only sent traffic once it can actually
  # reach the database -- while the container healthcheck above uses /healthz, so
  # it is not killed when it briefly cannot. Two probes, two questions.
  health_check_grace_period_seconds = 30

  deployment_circuit_breaker {
    enable   = true
    rollback = true # a deploy that cannot pass its own healthcheck rolls itself back
  }

  lifecycle {
    ignore_changes = [desired_count] # autoscaling owns this, not terraform
  }

  depends_on = [aws_lb_listener.https]
}

# The load test found the app to be CPU-bound at ~390 req/s on one core, while
# Postgres sat at 18% (docs/load-test.md). So it scales on CPU, and it scales the
# *tier* rather than the process: uvicorn --workers would be cheaper, but
# prometheus_client keeps a per-process registry, so a multi-worker container
# would under-report its own metrics.
resource "aws_appautoscaling_target" "app" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.app_min_count
  max_capacity       = var.app_max_count
}

resource "aws_appautoscaling_policy" "app_cpu" {
  name               = "${local.name}-app-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    # 60, not 90: the load test showed latency degrading badly once the process
    # saturates, and by the time CPU reads 90% the p99 has already gone.
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

# --- Worker ------------------------------------------------------------------

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "worker"
    image       = local.image
    command     = ["python", "-m", "webhook_receiver.worker.main"]
    essential   = true
    secrets     = local.secrets
    environment = local.environment

    portMappings = [{ containerPort = 9100, protocol = "tcp" }]

    # The worker's own /metrics, which is also the only honest liveness signal it
    # has: it is served from the worker process itself, so a reply proves that
    # process is alive. The image's default healthcheck probes :8000 -- the app's
    # port -- which the worker never listens on, so it MUST be overridden here or
    # ECS kills the worker forever. (It did exactly that in docker-compose.)
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:9100/metrics',timeout=2)\""]
      interval    = 15
      timeout     = 3
      retries     = 3
      startPeriod = 10
    }

    logConfiguration = local.log_config
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  # No load balancer, and no ingress rule. Nothing reaches a worker; it pulls its
  # work from the database. Scaling it needs no coordination -- SKIP LOCKED keeps
  # workers off each other's rows and the advisory lock keeps them off each
  # other's entities (FR-7, FR-9) -- so `worker_count` can be raised on its own.

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
