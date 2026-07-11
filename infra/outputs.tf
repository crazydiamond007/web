output "webhook_url" {
  description = "Where the provider points its webhook."
  value       = "https://${aws_lb.main.dns_name}/v1/webhooks/{source}"
}

output "ecr_repository" {
  description = "Push the image here before deploying."
  value       = aws_ecr_repository.app.repository_url
}

output "migrate_task_definition" {
  description = "Run this to completion BEFORE updating the services."
  value       = aws_ecs_task_definition.migrate.family
}

output "database_endpoint" {
  description = "Private. Reachable only from the app and worker security groups."
  value       = aws_db_instance.main.endpoint
}

output "webhook_secrets_arn" {
  description = "Created EMPTY. Populate it out of band -- terraform state is plaintext, and a signing key in state is a signing key in every state backup."
  value       = aws_secretsmanager_secret.webhook_secrets.arn
}

output "admin_api_key_secret_arn" {
  description = "Read it with `aws secretsmanager get-secret-value`. It is never printed here."
  value       = aws_secretsmanager_secret.admin_api_key.arn
}
