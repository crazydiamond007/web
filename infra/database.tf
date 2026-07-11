# RDS Postgres 16, and the secrets the tasks read at boot.

resource "random_password" "db" {
  length  = 32
  special = false # RDS rejects several punctuation characters in a master password
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16.4" # pinned: the app is tested against 16, and 17 is not a patch
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_allocated_storage * 5 # autoscale storage, not cost
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "webhook_receiver"
  username = "webhook"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false

  multi_az = var.db_multi_az

  # A failover is *retryable* by design: 57P01 (admin_shutdown) is in
  # RETRYABLE_SQLSTATES, so the events in flight are rescheduled with backoff
  # rather than dead-lettered (ADR-0005). That is why multi_az is a cost decision
  # here and not a correctness one.

  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:00-sun:05:00"
  copy_tags_to_snapshot   = true

  # A queue is not a cache. The whole promise of this service is that nothing
  # acknowledged is ever lost (NFR-3), so the database it is stored in does not
  # get deleted without a snapshot, and not by accident.
  deletion_protection       = var.environment == "prod"
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-final"

  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql"]

  apply_immediately = false
}

# --- Secrets -----------------------------------------------------------------
#
# Injected into the task as ECS `secrets`, not `environment`. The difference is
# not cosmetic: an `environment` value is visible in the task definition, in the
# console, and to anyone with `ecs:DescribeTaskDefinition`. A `secrets` value is
# fetched by the agent at start and never written into the definition.

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${local.name}/database-url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql+asyncpg://%s:%s@%s/%s",
    aws_db_instance.main.username,
    random_password.db.result,
    aws_db_instance.main.endpoint,
    aws_db_instance.main.db_name,
  )
}

resource "aws_secretsmanager_secret" "webhook_secrets" {
  # The per-source HMAC signing keys, as JSON: {"stripe": "whsec_..."}.
  #
  # Deliberately NOT given a value here. Terraform state is plaintext, so a
  # provider's signing key written into a .tf file is a signing key in every
  # state backup and every CI log that ever printed a plan. It is created empty
  # and populated out of band:
  #
  #   aws secretsmanager put-secret-value \
  #     --secret-id webhook-receiver-prod/webhook-secrets \
  #     --secret-string '{"stripe":"whsec_..."}'
  name                    = "${local.name}/webhook-secrets"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "admin_api_key" {
  name                    = "${local.name}/admin-api-key"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "admin_api_key" {
  secret_id     = aws_secretsmanager_secret.admin_api_key.id
  secret_string = random_password.admin_api_key.result
}

resource "random_password" "admin_api_key" {
  length  = 48
  special = false
}
