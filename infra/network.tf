# Two AZs, public subnets for the load balancer, private for everything that matters.
#
# The database and the tasks have NO route from the internet. The only thing a
# provider can reach is the ALB, on 443.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs             = slice(data.aws_availability_zones.available.names, 0, 2)
  public_subnets  = ["10.0.0.0/24", "10.0.1.0/24"]
  private_subnets = ["10.0.10.0/24", "10.0.11.0/24"]
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_subnets[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public-${local.azs[count.index]}" }
}

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_subnets[count.index]
  availability_zone = local.azs[count.index]

  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# The private subnets have NO NAT gateway, on purpose.
#
# A NAT gateway is ~$32/month per AZ before a byte of traffic moves, and the only
# thing the tasks need to reach outside the VPC is AWS itself: ECR to pull the
# image, CloudWatch to ship logs, Secrets Manager to read the signing keys. VPC
# endpoints do that privately, cost less, and -- the part that matters -- mean a
# task has no route to the internet at all. A compromised container cannot phone
# home.
#
# The moment a handler needs to call a third-party API, this decision has to be
# revisited and a NAT gateway added. Today no handler does.

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- VPC endpoints -----------------------------------------------------------

resource "aws_vpc_endpoint" "s3" {
  # ECR stores image layers in S3. Without this, an image pull fails in a subnet
  # with no NAT -- which is the single most common way this architecture is got
  # wrong, and it fails at task start with a message that does not mention S3.
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(["ecr.api", "ecr.dkr", "logs", "secretsmanager"])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
}

# --- Security groups ---------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public entry point. The only thing with an ingress rule from the internet."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from anywhere -- webhook providers have no fixed IPs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "The ingestion API. Reachable only from the load balancer."
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Everything it needs is inside the VPC (endpoints + RDS)."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
}

resource "aws_security_group_rule" "app_from_alb" {
  # A separate rule, not inline: `app` and `alb` reference each other, and inline
  # rules on both sides is a cycle Terraform cannot resolve.
  type                     = "ingress"
  security_group_id        = aws_security_group.app.id
  from_port                = 8000
  to_port                  = 8000
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
  description              = "Only the ALB may reach the app."
}

resource "aws_security_group" "worker" {
  name        = "${local.name}-worker"
  description = "No ingress at all. The worker is reached by nothing; it pulls work from the database."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
}

resource "aws_security_group" "database" {
  name        = "${local.name}-db"
  description = "Postgres. Reachable only from the app and the worker."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id, aws_security_group.worker.id]
  }
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-endpoints"
  description = "VPC interface endpoints, reachable from inside the VPC only."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
}
