# Vault policy "cocoindex", for the cocoindex and cocoindex-mcp Nomad jobs through
# their own role auth/jwt-nomad/role/cocoindex (vault-cocoindex.role.json).
# Apply with: vault-admin policy write cocoindex vault-cocoindex.policy.hcl

# aigw_key (both jobs), github_token (the indexer).
path "kv/data/cocoindex" {
  capabilities = ["read"]
}

# tokens: sha256 hashes of the MCP server's static bearer tokens.
path "kv/data/cocoindex-mcp" {
  capabilities = ["read"]
}

# The ali-bot S3 key pair, which signs for s3://alice-cocoindex (bucket policy
# alice-cocoindex.policy.json) and reads s3://alice-build-logs. Shared with the CI.
path "kv/data/s3-services" {
  capabilities = ["read"]
}
