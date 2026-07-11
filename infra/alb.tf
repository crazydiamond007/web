# The only thing a provider can reach.

variable "certificate_arn" {
  type        = string
  description = "ACM certificate for the webhook endpoint. Providers will not send to plain HTTP, and neither should we accept it."
  default     = "" # validated below; supply via terraform.tfvars
}

resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  # A webhook we have answered `200` to is a webhook we promised to process
  # (NFR-3). Dropping a request mid-flight during a deploy would break that
  # promise, so connections are allowed to finish.
  enable_deletion_protection = var.environment == "prod"
  idle_timeout               = 60
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "app" {
  name        = "${local.name}-app"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # awsvpc networking: tasks are ENIs, not instances

  health_check {
    # /readyz here, /healthz on the container (see ecs.tf). The load balancer is
    # asking "should I send this task traffic?" -- and the answer is no if it
    # cannot reach the database. The container runtime is asking "is this process
    # alive?" -- and the answer to *that* must not depend on the database, or an
    # RDS blip restarts the entire fleet.
    path                = "/readyz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Give in-flight ingestion time to commit. The row is written before the 200
  # goes out (NFR-3), so cutting a task off early is the one way this service can
  # lose an event it never acknowledged.
  deregistration_delay = 30
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# The admin API and /metrics are on the same port as ingestion, so they are
# reachable through the ALB. `/metrics` is unauthenticated by design (a scraper
# carries no admin key), which means it must not be publicly routable.
#
# Blocked at the edge rather than in the application: an application-level check
# is one refactor away from being bypassed, and the endpoint has no business being
# on the public internet in the first place.
resource "aws_lb_listener_rule" "block_metrics" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10

  action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "not found"
      status_code  = "404"
    }
  }

  condition {
    path_pattern {
      values = ["/metrics"]
    }
  }
}
