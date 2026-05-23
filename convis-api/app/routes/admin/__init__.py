"""Admin-only operational endpoints. Each route requires a JWT with role=admin
(enforced via require_admin in the route body). Keep this surface small —
this is a recovery / migration toolbox, not an extension point."""
