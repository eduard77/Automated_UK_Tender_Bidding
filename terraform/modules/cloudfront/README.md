# cloudfront — placeholder

This module is intentionally empty.

The dashboard (`tender-agent-dashboard`) is **not** planned to be served from
its own CloudFront distribution. Instead, it will be integrated as part of the
**genera-system.com** property. The exact mechanism is TBD: it might be a path
prefix on the parent site, a subdomain pointing at Vercel/Netlify/an S3+CF
distribution managed by the genera-system stack, or something else.

Until that decision is made, this module exists as a **clearly-marked
placeholder** so:

1. It's obvious that the dashboard's hosting is the unresolved infrastructure
   question, not an oversight.
2. The first attempt to apply this module will fail — fast and loud — until an
   operator removes the placeholder error and replaces it with real resources.

## When to fill this in

After the genera-system integration call resolves with one of:

- "Path prefix on genera-system.com" → write the CloudFront / origin / cache
  rules here that delegate `/tenders/*` (etc.) to the dashboard origin.
- "Subdomain on genera-system.com, e.g. tender.genera-system.com" → write a
  full CloudFront distribution with ACM cert + Route53 record.
- "Hosted elsewhere (Vercel / Netlify / …)" → delete this module, document the
  decision in `docs/deploy.md`, and leave the dashboard outside the Terraform
  scope.

## Why a hard failure instead of silent no-op

A silent no-op risks the deploy succeeding without anyone realising the
dashboard isn't being served. The `null_resource` below has a `local-exec` that
prints a banner and exits non-zero, so anyone who accidentally instantiates
this module will know within seconds.
