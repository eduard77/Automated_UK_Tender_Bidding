terraform {
  backend "s3" {
    bucket         = "tender-agent-tfstate"
    key            = "envs/prod/terraform.tfstate"
    region         = "eu-west-2"
    dynamodb_table = "tender-agent-tfstate-lock"
    encrypt        = true
  }
}
