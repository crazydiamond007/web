# Fargate + RDS, as code (NFR-10).
#
# STATUS: WRITTEN AND VALIDATED, NOT APPLIED. There is no AWS account behind this
# and nothing has ever run from it. `terraform validate` passes; `terraform plan`
# has never been executed, because a plan needs credentials. It is included
# because "how would you deploy this?" deserves an answer you can review line by
# line rather than a paragraph of prose -- and because a README that claimed a
# deployment that never happened would be the one thing in this repo that was not
# true.
#
# Read `infra/README.md` first: it says what this costs and what it is missing.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is local here because there is no account to hold a bucket. A real
  # deployment moves it to S3 + DynamoDB locking on day one: local state means one
  # laptop's `terraform apply` can silently clobber another's.
  #
  # backend "s3" {
  #   bucket         = "webhook-receiver-tfstate"
  #   key            = "prod/terraform.tfstate"
  #   region         = "eu-west-1"
  #   dynamodb_table = "webhook-receiver-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "webhook-receiver"
      ManagedBy = "terraform"
      Env       = var.environment
    }
  }
}

locals {
  name = "webhook-receiver-${var.environment}"

  # The app and the worker run the SAME image with different commands -- exactly
  # as they do in docker-compose. One image means the worker cannot drift from the
  # code that was tested, and a rollback is one image tag, not two.
  image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
}
