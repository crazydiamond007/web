variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "environment" {
  type    = string
  default = "prod"

  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod."
  }
}

variable "image_tag" {
  type        = string
  description = "Immutable image tag to deploy. Never 'latest': you cannot roll back to a tag that moves."
  default     = "v0.1.0"

  validation {
    condition     = var.image_tag != "latest"
    error_message = "Refusing 'latest'. A moving tag makes the deployed version unknowable and rollback impossible."
  }
}

# --- Sizing ------------------------------------------------------------------
#
# The load test says the app is CPU-bound on one core and Postgres has ~80%
# headroom (docs/load-test.md). So the app scales on CPU, and the database starts
# small: sizing it for a bottleneck it is not yet reaching would be paying for a
# problem we do not have.

variable "app_cpu" {
  type    = number
  default = 512 # 0.5 vCPU
}

variable "app_memory" {
  type    = number
  default = 1024
}

variable "app_min_count" {
  type    = number
  default = 2 # two AZs; one task is a single point of failure with extra steps
}

variable "app_max_count" {
  type    = number
  default = 8
}

variable "worker_cpu" {
  type    = number
  default = 256
}

variable "worker_memory" {
  type    = number
  default = 512
}

variable "worker_count" {
  type        = number
  default     = 2
  description = "Workers coordinate through the database (SKIP LOCKED + advisory locks), so this is safe to raise with no other change."
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_multi_az" {
  type        = bool
  default     = false
  description = "Doubles the database cost. True for real production: a failover is retryable (57P01 is in RETRYABLE_SQLSTATES), so it costs a retry, not a backlog."
}

variable "log_retention_days" {
  type    = number
  default = 30
}
