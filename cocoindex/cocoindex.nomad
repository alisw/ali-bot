## CocoIndex indexer: ALICE commit history -> commits.db, pull requests and
## their review discussion -> prs.db, CI build logs (s3://alice-build-logs) ->
## ci-failures.db (SQLite + sqlite-vec), snapshotted to S3 after every pass.
## SKETCH -- not registered.
#
#   cd <this directory>                               # file() paths are relative to CWD
#   nomad job validate cocoindex.nomad                # parse, submit nothing
#   nomad-rw job run cocoindex.nomad                  # register (human)
#
# One SERVICE job that loops internally (runner.sh), like ci-linux-x86's claim
# loop, rather than a periodic batch:
#   * the state is a warm local disk (LMDB per flow + git mirrors), which a
#     long-lived allocation owns in its sticky ephemeral_disk -- no host volume,
#     no node pinning, nothing to create by hand;
#   * the S3 snapshot taken after every pass is what makes the disk disposable:
#     a new node (or a purged job) restores the last published state+DB pair;
#   * the proxy gate token rotates daily; runner.sh re-resolves it every pass.
#
# No Postgres. Outputs are SQLite files, one per flow (commits.db, prs.db,
# ci-failures.db), published as immutable snapshots that readers download and
# open read-only. See DESIGN.md.
#
# Before registering:
#   * the S3 bucket named in STORE_URL and in the proxy route must exist, and
#     the keys the bootstrap pushes must be able to read/write/delete in it;
#   * kv/data/cocoindex must hold aigw_key (the aigw API key, bare) and
#     github_token (a PAT for the prs flow; public repos, so no scopes needed);

job "cocoindex" {
  datacenters = ["meyrin"]
  type        = "service"

  meta {
    # Bump to force a new job version when the spec is otherwise unchanged
    # (same reason as in ci-linux-x86.nomad).
    redeploy = "2026-11-13-1"
  }

  update {
    max_parallel = 1
    # Healthy = tasks have RUN this long. Long enough to get past the
    # restore, which is where a broken spec fails.
    min_healthy_time  = "3m"
    healthy_deadline  = "30m"
    progress_deadline = "1h"
    # Never revert silently to a previous spec (same reasoning as the CI jobs).
    auto_revert = false
  }

  constraint {
    attribute = "${attr.kernel.name}"
    value     = "linux"
  }

  constraint {
    attribute = "${attr.driver.docker}"
    value     = "1"
  }

  group "cocoindex" {
    count = 1

    # As the CI workers: a runner that dies comes back after a delay instead of
    # failing the allocation (and its proxy sidecar) for good. runner.sh exits
    # non-zero on purpose when it cannot restore from an existing snapshot, so
    # this is also the retry loop for a transient S3 failure at startup.
    restart {
      attempts = 3
      interval = "30m"
      delay    = "1m"
      mode     = "delay"
    }

    ephemeral_disk {
      # /alloc/data holds everything that is expensive to rebuild:
      #   cocoindex/flows/<flow>/state   LMDB (COCOINDEX_DB), ~1.5x the DB
      #   cocoindex/flows/<flow>/<db>    the output SQLite file
      #   cocoindex/mirrors/*.git        blobless bare mirrors, branches only
      #   cocoindex/sources/*            shallow sparse trees (C++ and CMake only) and
      #                                  templates.json, for srctemplates.py (~0.5 GB)
      #   cocoindex/snap                 scratch for the snapshot being published
      # Estimated total ~5 GB for commits; 10 GB leaves room for ci-failures.
      # sticky: an updated allocation lands back on this node and keeps it.
      # migrate = false: if it cannot, S3 restore is cheaper than a copy, and
      # the snapshot is the authoritative copy anyway. Size is a placement
      # input only (see ci-linux-x86.nomad).
      size    = 10000
      sticky  = true
      migrate = false
    }

    volume "grid-hostcert" {
      type      = "host"
      source    = "grid-hostcert"
      read_only = true
    }

    volume "grid-hostkey" {
      type      = "host"
      source    = "grid-hostkey"
      read_only = true
    }

    # ---- security-proxy sidecar ---------------------------------------------
    # Three routes: aigw (embeddings), github (GraphQL for the prs flow; REST
    # status history and GraphQL PR files for the ci-failures flow) and s3. The
    # s3 route is an ALLOWLIST of two buckets, with no "" entry and no flat
    # default keypair, so a leaked gate token reaches these and nothing else:
    #   alice-cocoindex   snapshots (read, write, delete: prune)
    #   alice-build-logs  CI build logs, READ by the ci-failures flow (GET and
    #                     ListObjectsV2 only; the flow never writes). The proxy
    #                     cannot restrict a bucket to reads, so with the CI's own
    #                     keypair the restriction is the code's; a read-only
    #                     keypair for this bucket is the open decision in DESIGN.md.
    # No vault route: the bootstrap reads Vault directly.
    task "proxy" {
      driver = "docker"

      lifecycle {
        hook    = "prestart"
        sidecar = true
      }

      config {
        # Pinned by digest -- this container holds credentials. The amd64
        # "latest" of 2026-10-27 (docks ALI_BOT_REF f7f4df6c): newer than
        # queue-metrics.nomad's be422fab, which predates the per-bucket S3
        # keypairs ("s3": {"buckets": ...}, ali-bot 2732d48b) the s3 route needs.
        image        = "registry.cern.ch/alisw/security-proxy@sha256:39464dfbd28952c43d17ea24fac9bc570be6a48195e71e637abefdacfab66f9c"
        network_mode = "host"
        args         = ["/usr/local/lib/security-proxy/venv/bin/security-proxy", "--config", "/local/config.json"]
      }

      template {
        destination = "${NOMAD_ALLOC_DIR}/cern-ca-bundle.pem"
        data        = <<-EOC
        -----BEGIN CERTIFICATE-----
        MIIGqTCCBJGgAwIBAgIQAojDcLlcbrhBX0qrEka4mzANBgkqhkiG9w0BAQ0FADBK
        MQswCQYDVQQGEwJjaDENMAsGA1UEChMEQ0VSTjEsMCoGA1UEAxMjQ0VSTiBSb290
        IENlcnRpZmljYXRpb24gQXV0aG9yaXR5IDIwHhcNMTMwMzE5MTI1NTM2WhcNMzMw
        MzE5MTMwNTM0WjBKMQswCQYDVQQGEwJjaDENMAsGA1UEChMEQ0VSTjEsMCoGA1UE
        AxMjQ0VSTiBSb290IENlcnRpZmljYXRpb24gQXV0aG9yaXR5IDIwggIiMA0GCSqG
        SIb3DQEBAQUAA4ICDwAwggIKAoICAQDxqYPFW2qVVi3Rw1NKlEf7x70xF+6a8uE/
        Tu4ZVQF/K2RXI95QLkYfKItZvy9Az3ib/VlUho5f8fBaqy4n70uwC7+qd3Aq1/xQ
        ysykPCbBBAsOSQQpTlhrMD2V5Ya9zrirphOhutddiqV96zBCyMM+Gz5uYv9u+cm4
        tg1EOmAMGh2UNxfTFNVmXKkk7eFTSC1+zgb28H6nd3xzV27sn9bfOfGh//ZPy5gm
        Qx0Oh/tc6WMreWzRZBQm5SJiK0QOzPv09p5WmdY2WxZoqNTFBDACQO7ysFOktc74
        fPVFX/lmt4jFNSZRIOvvaACI/qlEaAJTR4FHIY9uSMsV8DrtzhI1Ucyv3kqlQpbF
        jDouq44IryA/np4s/124bW+x8+n/v+at/AxPjvHBLiGhB+J38Z6KcJogoDnGzIXR
        S+YUr/vGz34jOmkRuDN5STuuAXzyCKFXaoAm0AwjTziIv3E0jxC1taw6FpKevnd1
        CLsTLAEUiEjzStFkDhd/Hpipc57zmMFY8VYet2wVqSFjnt2REWOVbZlbCiMHmSeD
        u5EuZLiU8xlkiaCfn4A5XZ6X0qprbgDviGJtwxzNvTg7Hn0ziW5/ELryfQXCwZJ+
        FVne8Zu8sbgy/sDkX+pyFuyB4XgiM0eMNkoexIXJaRdlMWDIL5ysiIXQKjhynAv5
        KLHbRjciVwIDAQABo4IBiTCCAYUwCwYDVR0PBAQDAgGGMA8GA1UdEwEB/wQFMAMB
        Af8wHQYDVR0OBBYEFPp7+96bDaPyUrds7VsPC6KmpvgEMBAGCSsGAQQBgjcVAQQD
        AgEAMIIBMgYDVR0gBIIBKTCCASUwggEhBgorBgEEAWAKBAEBMIIBETCBwgYIKwYB
        BQUHAgIwgbUegbIAQwBFAFIATgAgAFIAbwBvAHQAIABDAGUAcgB0AGkAZgBpAGMA
        YQB0AGkAbwBuACAAQQB1AHQAaABvAHIAaQB0AHkAIAAyACAAQwBlAHIAdABpAGYA
        aQBjAGEAdABlACAAUABvAGwAaQBjAHkAIABhAG4AZAAgAEMAZQByAHQAaQBmAGkA
        YwBhAHQAZQAgAFAAcgBhAGMAdABpAGMAZQAgAFMAdABhAHQAZQBtAGUAbgB0MEoG
        CCsGAQUFBwIBFj5odHRwOi8vY2FmaWxlcy5jZXJuLmNoL2NhZmlsZXMvY3AtY3Bz
        L2Nlcm4tcm9vdC1jYTItY3AtY3BzLnBkZjANBgkqhkiG9w0BAQ0FAAOCAgEAo0Px
        l4CZ6C6bDH+b6jV5uUO0NIHtvLuVgQLMdKVHtQ2UaxeIrWwD+Kz1FyJCHTRXrCvE
        OFOca9SEYK2XrbqZGvRKdDRsq+XYts6aCampXj5ahh6r4oQJ8U7aLVfziKTK13Gy
        dYFoAUeUrlNklICt3v2wWBaa1tg2oSlU2g4iCg9kYpRnIW3VKSrVsdVk2lUa4EXs
        nTEJ30OS7rqX3SdqZp8G+awtBEReh2XPhRgJ6w3xiScP/UdWYUam2LflCGX3RibB
        /DZhgGHRRoE4/D0kQMP2XTz6cClbNklECTlp0qZIbiaf350HbcDEFzYRSSIi0emv
        kRGcMgsi8yTTU87q8Cr4hETxAF3ZbSVNC0ZaTZ8RBbM9BXguhYzKkVBgG/cMpUjs
        B6tY2HMZbAZ3TKQRb/bRyUigM9DniKWeXkeL/0Nsno+XbcpAqLjtVIRwCg6jTLUi
        1NRsl3BP6C824dVaoI8Ry7m+o6O+mtocw4BMhHfTcoWCO8CWjT0ME67JzaAYa5eM
        +OqoWtgbgweBlfO0/3GMnVGMAmI4FlhH2oWKWQgWdgr0Wgh9K05VcxSpJ87/zjhb
        MQn/bEojWmp6eUppPaqNFcELvud41qoe6hLsOYQVUQ1sHi7n6ouhg4BAbwS2iyD2
        uiA6FHTCeLreFGUzs5osPKiz3GE5D6V9she9xIQ=
        -----END CERTIFICATE-----
        -----BEGIN CERTIFICATE-----
        MIIJnDCCB4SgAwIBAgIKYQQltAAAAAAACzANBgkqhkiG9w0BAQ0FADBKMQswCQYD
        VQQGEwJjaDENMAsGA1UEChMEQ0VSTjEsMCoGA1UEAxMjQ0VSTiBSb290IENlcnRp
        ZmljYXRpb24gQXV0aG9yaXR5IDIwHhcNMjIwMzI5MDgyNDIyWhcNMzIwMzI5MDgz
        NDIyWjBWMRIwEAYKCZImiZPyLGQBGRYCY2gxFDASBgoJkiaJk/IsZAEZFgRjZXJu
        MSowKAYDVQQDEyFDRVJOIEdyaWQgQ2VydGlmaWNhdGlvbiBBdXRob3JpdHkwggIi
        MA0GCSqGSIb3DQEBAQUAA4ICDwAwggIKAoICAQDS9Ypy1csm0aZA4/QnWe2oaiQI
        LqfeekV8kSSvOhW2peo5cLNIKbXATOo1l2iwIbCWV8SRU2TLKxHIL8fAOJud5n9K
        mEKBew7nzubl1wG93B4dY0KREdb3/QB/7OkG8ZZvLqrvQZVGT1CgJ+NFFUiJ315D
        FWkKctZv27LjQamzCxpX+gZSsmwZmSReY67cnm6P7z+/3xVNhwb+4Z+1Ww4vHhMc
        dh1Dsrkv9vXU01UN752QtQ6l56uQLYEB2+vaHB6IpyC9zAQ/33GulCq8Gbj7ykPd
        9AcRVBeJAErSK+oMHThtdLD7mhTkZivakaNe4O1EhPFH0rWwV45IFN7ipELA5qDx
        djdzo6JtLJQMaSV/TV+amEf2CaKlD0giqGhjfSNiOX5HCmpqV14kbl+7Qho6ykZy
        b1DGpf70yILnX+AUtdpd8lulTu1yg1Bg5cFQskUIk5+s4nsC1VpmeNxYaeFEcYZj
        Ph2mdD7zLo889MtF7kZv7+6J6p4NBL3fQ9Os8/h8XVlfDatzbpVH4jYKKAd4nwJb
        knJaKPE0LzLzVfJBwnDxqe8hb64gI8Frludp+jaOYzvMqlzAe9z4a9971iXIWaaG
        unbAoEkXj69y7MsvCjWXB7o9HdBaS9FL+ZtXTKCyXl+XLFseYQoQburKr+eTcRed
        KLJNj4tRF1799PO69wIDAQABo4IEdjCCBHIwEAYJKwYBBAGCNxUBBAMCAQEwIwYJ
        KwYBBAGCNxUCBBYEFGPCgXhtlBTXUVYziSFk8YWmsNHgMB0GA1UdDgQWBBSloP1m
        WP253Xrhsp2fo9HlUBiU5zCCAS4GA1UdIASCASUwggEhMIIBHQYKKwYBBAFgCgQB
        ATCCAQ0wgb4GCCsGAQUFBwICMIGxHoGuAEMARQBSAE4AIABHAHIAaQBkACAAQwBl
        AHIAdABpAGYAaQBjAGEAdABpAG8AbgAgAEEAdQB0AGgAbwByAGkAdAB5ACAAQwBl
        AHIAdABpAGYAaQBjAGEAdABlACAAUABvAGwAaQBjAHkAIABhAG4AZAAgAEMAZQBy
        AHQAaQBmAGkAYwBhAHQAZQAgAFAAcgBhAGMAdABpAGMAZQAgAFMAdABhAHQAZQBt
        AGUAbgB0MEoGCCsGAQUFBwIBFj5odHRwOi8vY2FmaWxlcy5jZXJuLmNoL2NhZmls
        ZXMvY3AtY3BzL2Nlcm4tZ3JpZC1jYS1jcC1jcHMucGRmADAZBgkrBgEEAYI3FAIE
        DB4KAFMAdQBiAEMAQTALBgNVHQ8EBAMCAYYwDwYDVR0TAQH/BAUwAwEB/zAfBgNV
        HSMEGDAWgBT6e/vemw2j8lK3bO1bDwuipqb4BDCCAUQGA1UdHwSCATswggE3MIIB
        M6CCAS+gggErhlJodHRwOi8vY2FmaWxlcy5jZXJuLmNoL2NhZmlsZXMvY3JsL0NF
        Uk4lMjBSb290JTIwQ2VydGlmaWNhdGlvbiUyMEF1dGhvcml0eSUyMDIuY3JshoHU
        bGRhcDovLy9DTj1DRVJOJTIwUm9vdCUyMENlcnRpZmljYXRpb24lMjBBdXRob3Jp
        dHklMjAyLENOPUNFUk5QS0lST09UMDIsQ049Q0RQLENOPVB1YmxpYyUyMEtleSUy
        MFNlcnZpY2VzLENOPVNlcnZpY2VzLENOPUNvbmZpZ3VyYXRpb24sREM9Y2VybixE
        Qz1jaD9jZXJ0aWZpY2F0ZVJldm9jYXRpb25MaXN0P2Jhc2U/b2JqZWN0Q2xhc3M9
        Y1JMRGlzdHJpYnV0aW9uUG9pbnQwggFEBggrBgEFBQcBAQSCATYwggEyMGcGCCsG
        AQUFBzAChltodHRwOi8vY2FmaWxlcy5jZXJuLmNoL2NhZmlsZXMvY2VydGlmaWNh
        dGVzL0NFUk4lMjBSb290JTIwQ2VydGlmaWNhdGlvbiUyMEF1dGhvcml0eSUyMDIu
        Y3J0MIHGBggrBgEFBQcwAoaBuWxkYXA6Ly8vQ049Q0VSTiUyMFJvb3QlMjBDZXJ0
        aWZpY2F0aW9uJTIwQXV0aG9yaXR5JTIwMixDTj1BSUEsQ049UHVibGljJTIwS2V5
        JTIwU2VydmljZXMsQ049U2VydmljZXMsQ049Q29uZmlndXJhdGlvbixEQz1jZXJu
        LERDPWNoP2NBQ2VydGlmaWNhdGU/YmFzZT9vYmplY3RDbGFzcz1jZXJ0aWZpY2F0
        aW9uQXV0aG9yaXR5MA0GCSqGSIb3DQEBDQUAA4ICAQAv56iMPo0VUkrHxPYLjfyW
        IL/TmYxxYldO8kCTKXyaRO4ZmwD6JjLaclTgSHz7gOKFL35ZF0Rv4nWk/ZJBl+dU
        1udgBjF/uKK0v0m+7iEIOG0HORCCQCDgayuiLomI5eQp8KTgHrswHWL+ESxa3Hdv
        vr7GBG/7EhrYwstm/tOJ8cKaeiooSxHw5Lgsqq229SxfO8fSyS8DAa5eUdWT/dVU
        RDR8lGQShx4R9JOHSDg0y6rE7V0cw/BO3NQuaxMunFXkQprtWneJfR4uugMOKk/v
        tMhQGCDB7o3CVhLGSb+76Tny+eSa2g+Zv17PGVfhnF9oynkCII+shX9TmOUsDEnS
        7MWES58YwnpBZrxdeJVPEzVVuYEZP4QsLrIL1ynFqBwFAnPU48Hs6s+kOI/9BFJz
        v+Fp/iw8BZSOclpJzA5rkW6yQ7LVfjFBV1CgyhO8GH5jhYBd5ZLvG8eLNm8Gpt+H
        n30awoaDoMuHcGS5B6NOZLfwE+suTxMw8pjHhKXx7RkSoeZy72PinlbWn1tWLiPa
        UMdkrb/WHOdMKaadQTDO/VyibBL49iJ8BAlERgIl9QaRDLjAIdD45rLdBe95HxSl
        zpZqsxuI09eJ8+iLFJhTDH2BODoEuqbn6PB/5z2d5zuG5sr85Vzn81ddapuUT9Ra
        /dB5eJQeFZ0WjtUOO3gS/A==
        -----END CERTIFICATE-----
        EOC
      }

      # JSON has no comments: explanations stay above, never inside the heredoc.
      template {
        destination = "local/config.json"
        change_mode = "restart"
        data        = <<-EOH
        {
          "cert": {"ingest": "grid-cert"},
          "cafile": "/alloc/cern-ca-bundle.pem",
          "agent_socket":  "/alloc/security-proxy/agent/agent.sock",
          "ingest_socket": "/alloc/security-proxy/ingest/ingest.sock",
          "agent_socket_group": "securityproxy_clients",
          "secret_rotation_seconds": 86400,
          "routes": [
            {"name": "aigw", "prefix": "/aigw/",
             "upstream": "https://aigw.cern.ch",
             "inject_headers": {"Authorization": {"ingest": "aigw-key"}}},
            {"name": "github", "prefix": "/github/",
             "upstream": "https://api.github.com",
             "inject_headers": {"Authorization": {"ingest": "github-token"}}},
            {"name": "monit-otlp", "prefix": "/otlp/",
             "upstream": "https://monit-otlp.cern.ch:4319",
             "inject_headers": {"Authorization": {"ingest": "monit-otlp-auth"}}},
            {"name": "s3", "upstream": "https://s3.cern.ch",
             "s3": {"buckets": {
                      "alice-cocoindex": {"access_key": {"ingest": "s3-access"},
                                          "secret_key": {"ingest": "s3-secret"}},
                      "alice-build-logs": {"access_key": {"ingest": "s3-access"},
                                           "secret_key": {"ingest": "s3-secret"}}}}}
          ]
        }
        EOH
      }

      resources {
        cpu    = 200
        memory = 256
      }
    }

    # ---- slot provisioning (sidecar: refills the proxy if it restarts) ------
    task "bootstrap" {
      driver = "docker"

      lifecycle {
        hook    = "prestart"
        sidecar = true
      }

      vault {
        # Its own role (jwt-nomad/role/cocoindex), bound to the cocoindex and
        # cocoindex-mcp jobs, with policy "cocoindex": kv/data/cocoindex,
        # kv/data/cocoindex-mcp and kv/data/s3-services, read only. Not the
        # shared "nomad" role, which any job can use.
        role = "cocoindex"
      }

      user = "root"

      config {
        image        = "registry.cern.ch/alisw/security-proxy@sha256:39464dfbd28952c43d17ea24fac9bc570be6a48195e71e637abefdacfab66f9c"
        network_mode = "host"
        entrypoint   = ["/bin/sh"]
        args         = ["/local/bootstrap.sh"]
      }

      volume_mount {
        volume      = "grid-hostcert"
        destination = "/etc/grid-security/hostcert.pem"
        read_only   = true
      }

      volume_mount {
        volume      = "grid-hostkey"
        destination = "/etc/grid-security/hostkey.pem"
        read_only   = true
      }

      template {
        destination = "local/bootstrap.sh"
        perms       = "0755"
        data        = <<-EOH
        #!/bin/sh -e
        SOCK=/alloc/security-proxy/ingest/ingest.sock
        BIN=/usr/local/lib/security-proxy/venv/bin

        push_all () {
          cat /etc/grid-security/hostcert.pem /etc/grid-security/hostkey.pem \
            | "$BIN/security-proxy-push" grid-cert --socket "$SOCK" || return 1
          # inject_headers has no templating: the slot holds the whole header.
          python3 /local/vault-field.py aigw_key "Bearer " kv/data/cocoindex \
            | "$BIN/security-proxy-push" aigw-key --socket "$SOCK" || return 1
          # DECISION: a PAT of its own, not kv/data/ci's: the prs bootstrap spends
          # ~5.5k GraphQL points (PRS_RATE_RESERVE keeps 500 back), which would
          # come out of the CI's hourly budget if the token were shared.
          python3 /local/vault-field.py github_token "Bearer " kv/data/cocoindex \
            | "$BIN/security-proxy-push" github-token --socket "$SOCK" || return 1
          # DECISION: the keypair that owns the snapshot bucket. kv/data/s3-services
          # is what the CI jobs already use (alibuild-repo, alice-build-logs); a
          # dedicated pair would live in kv/data/cocoindex under the same names.
          python3 /local/vault-field.py aws_access_key_id "" kv/data/s3-services \
            | "$BIN/security-proxy-push" s3-access --socket "$SOCK" || return 1
          python3 /local/vault-field.py aws_secret_access_key "" kv/data/s3-services \
            | "$BIN/security-proxy-push" s3-secret --socket "$SOCK" || return 1
          # Optional: the MONIT (Grafana) credential, for metrics.py. Its own
          # field in kv/data/cocoindex if set there, else the one queue-metrics
          # uses in kv/data/ci (if this job's role may read it). Without it the
          # monit-otlp route answers 503 and no metrics are sent; nothing else
          # changes.
          if key=$(python3 /local/vault-field.py monit_otlp_auth "" kv/data/cocoindex 2>/dev/null) ||
             key=$(python3 /local/vault-field.py queue_metrics_otlp_auth "" kv/data/ci 2>/dev/null); then
            printf '%s' "$key" | "$BIN/security-proxy-push" monit-otlp-auth --socket "$SOCK" || return 1
          fi
          unset key
        }

        # Reprovision whenever the proxy rebinds its ingest socket (inode change),
        # exactly as the CI workers' bootstrap does.
        provisioned=""
        while :; do
          current=$(stat -c %i "$SOCK" 2>/dev/null || echo "")
          if [ -n "$current" ] && [ "$current" != "$provisioned" ] && push_all; then
            provisioned=$current
            echo "provisioned grid-cert, aigw-key, github-token, s3 keys and monit-otlp-auth (if set)"
          fi
          sleep 5
        done
        EOH
      }

      template {
        destination = "local/vault-field.py"
        perms       = "0755"
        data        = <<-EOP
        """Print one field of a Vault kv path, optionally prefixed. Never logs it.

        Usage: vault-field.py FIELD PREFIX PATH
        """
        import json, ssl, sys, urllib.request
        field, prefix, path = sys.argv[1], sys.argv[2], sys.argv[3]
        ctx = ssl.create_default_context()
        ctx.load_verify_locations("/alloc/cern-ca-bundle.pem")
        ctx.load_cert_chain("/etc/grid-security/hostcert.pem",
                            "/etc/grid-security/hostkey.pem")
        token = open("/secrets/vault_token").read().strip()
        req = urllib.request.Request("https://alivault.cern.ch/v1/" + path,
                                     headers={"X-Vault-Token": token})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            data = json.load(r)["data"]["data"]
        value = data.get(field, "").strip()
        if not value:
            sys.exit("%s has no %s" % (path, field))
        sys.stdout.write(prefix + value)
        EOP
      }

      resources {
        cpu    = 100
        memory = 128
      }
    }

    # ---- the indexer ----------------------------------------------------------
    task "index" {
      driver = "docker"

      # runner.sh traps TERM and forwards INT to cocoindex; LMDB and SQLite are
      # crash-safe if it does not finish in time. Nomad clients cap this at
      # their max_kill_timeout (30s by default).
      kill_signal  = "SIGTERM"
      kill_timeout = "30s"

      config {
        # alisw/docks cocoindex/: python 3.12 (loadable SQLite extensions, for
        # sqlite-vec), git, setpriv, and the hash-locked venv of
        # requirements-linux.lock in /opt/cocoindex/venv. Runtime only: the
        # code comes from the templates below. Built 2026-11-16 by docks
        # workflow run 43866765519 (docks db77616d, ALI_BOT_REF 7c56be43).
        # A change to requirements-linux.lock needs a new image and this digest.
        image        = "registry.cern.ch/alisw/cocoindex@sha256:2643b976055e724f1dac6a3b8fdf2dc1261f02f4e5103637a8a145683449399e"
        network_mode = "host"
        entrypoint   = ["/bin/bash", "-c"]
        # Root only long enough to hand /alloc/data/cocoindex to an unprivileged
        # uid; group 8485 = securityproxy_clients in the proxy image, which owns
        # the agent socket (as queue-metrics' collector does with setpriv).
        args = [<<-EOA
          set -e
          mkdir -p /alloc/data/cocoindex/home
          chown 1000:1000 /alloc/data/cocoindex /alloc/data/cocoindex/home
          exec setpriv --reuid=1000 --regid=1000 --groups=8485 -- /bin/bash /local/driver.sh
        EOA
        ]
      }

      env {
        HOME      = "/alloc/data/cocoindex/home"
        COCO_ROOT = "/alloc/data/cocoindex"
        CODE_DIR  = "/local"
        # driver.sh runs runner.sh from the code the Nomad variable
        # nomad/jobs/cocoindex (item "ref": a sha or branch on ali-bot master)
        # names, rendered into DEPLOY_ENV, and reloads it between flows when
        # the variable changes; without the variable, from SHIPPED (the files
        # this jobspec ships). Ordinary code changes are deployed with
        #   nomad-rw var put -force nomad/jobs/cocoindex ref=<sha>
        # (effective at the next flow boundary, no flow lost). `nomad-rw job
        # run` only for what cannot wait or cannot go that way: an urgent fix
        # (set the variable first: the restart runs its ref), a change to this
        # jobspec, or to the image's venv. See DESIGN.md, "Deploying code
        # without restarting the job".
        DEPLOY_ENV  = "/local/deploy.env"
        SHIPPED     = "/local"
        # The image's venv; runner.sh must not build its own.
        VENV      = "/opt/cocoindex/venv"
        SKIP_VENV = "1"
        NODE_NAME = "${node.unique.name}"

        # Flows run one at a time, each to completion (or PASS_TIMEOUT) before
        # the next, in an order shuffled every pass (runner.sh pass_order).
        FLOWS = "commits,prs,ci-failures"
        FLOW_ORDER = "random"   # shuffled every pass; "fixed": as FLOWS lists them

        PASS_SLEEP            = "3600"
        PASS_TIMEOUT          = "50m"
        SNAPSHOT_KEEP         = "36"   # run ids: one per flow publish, ~3 a pass
        SNAPSHOT_MIN_INTERVAL = "0"
        # DECISION: bucket name. Must match the proxy route's allowlist above.
        STORE_URL = "s3://alice-cocoindex/cocoindex"

        PROXY_SOCK  = "/alloc/security-proxy/agent/agent.sock"
        PROXY_TOKEN = "/local/proxy-token.py"

        # Throughput into aigw: see commits_flow.py. None is in a memo key
        # (LiteLLMEmbedder's memo key is the model and its kwargs only).
        # EMBED_TIMEOUT_S bounds one request of <= 64 texts, retries of fast
        # failures included; a slow request is not retried, its component
        # fails and the next pass redoes it. Seconds normally; 60 s, not 300,
        # so a slow aigw does not park all 16 component slots for 5 minutes
        # each (2026-11-17: commits and prs both sat at "16 short" for a whole
        # PASS_TIMEOUT).
        # The deadline also runs while a batch waits for one of the
        # EMBED_MAX_INFLIGHT_REQUESTS permits, but fewer components did not
        # help: with 6 (2026-11-18, 09:17-09:47) 44% of commits' requests still
        # timed out (46% with 16) and throughput fell to 747 texts in 30 min.
        # aigw itself was slow: the idle probe averaged 19 s, up to 57 s.
        COCOINDEX_MAX_INFLIGHT_COMPONENTS = "16"
        EMBED_MAX_INFLIGHT_REQUESTS       = "4"
        EMBED_TIMEOUT_S                   = "60"
        EMBED_MODEL                       = "openai/embeddinggemma-300m"
        # Between passes, one short embedding per route, timed, every
        # PROBE_INTERVAL seconds (metrics.py probe): the embedder's own speed,
        # apart from the indexer's load on it.
        EMBED_PROBE_ROUTES                = "aigw"
        PROBE_INTERVAL                    = "300"
        METRICS_SERVICE                   = "cocoindex"
        DIFF_EMBED_SINCE                  = "2025-01-01"
        # prs flow: see prs_flow.py. PRS_FETCH_BUDGET_S bounds the GitHub half of
        # each pass, so a bootstrap spreads over a few passes and still embeds.
        PRS_FETCH_BUDGET_S                = "1200"
        # The token is alibuild's, whose 5000 GraphQL points an hour every CI
        # claim worker also lists its work with. Stop (until the next pass)
        # with 1500 still left for them, not 500: GitHub reported 477-498
        # left at the start of several prs passes on 2026-11-18, and once 0.
        PRS_RATE_RESERVE                  = "1500"
        PRS_DIFFS                         = "1"
        # ci-failures flow: see ci_failures_flow.py. The download budget spreads
        # the bootstrap (~10k logs, ~65 GB) over a few passes; failed builds first.
        CIF_FETCH_BUDGET_S                = "1200"
        CIF_FETCH_THREADS                 = "4"
        CIF_GH_BUDGET_S                   = "600"
        CIF_BUCKET                        = "alice-build-logs"
        MIRROR_FILTER                     = "blob:none"
        COCOINDEX_DISABLE_USAGE_TRACKING  = "1"
      }

      # The key of the filler hashes in the published ci-failures.db
      # (srctemplates.py): it groups the values of a message without letting a
      # reader confirm a guess at them. Not an upstream credential, so it does
      # not go through the proxy. Missing key = no filler hashes published.
      #   vault-admin kv patch kv/cocoindex filler_key=$(openssl rand -hex 32)
      vault {
        role        = "cocoindex"
        change_mode = "noop"
      }
      template {
        destination = "secrets/filler.env"
        env         = true
        change_mode = "noop"
        data        = <<-EOT
          {{ with secret "kv/data/cocoindex" }}{{ with .Data.data.filler_key }}CIF_FILLER_KEY={{ . }}{{ end }}{{ end }}
        EOT
      }

      # The code ships inside the jobspec (file() reads it at submit time), so
      # a code change is a new job version and nothing is fetched at runtime.
      # The deploy ref (driver.sh). A job reads its own nomad/jobs/<job>
      # variables without any ACL policy. No variable, or no "ref" item: an
      # empty file, and the shipped code runs. "noop": driver.sh and runner.sh
      # read the file at their safe points; nothing is restarted.
      template {
        destination = "local/deploy.env"
        change_mode = "noop"
        data        = <<-EOT
        {{ if nomadVarExists "nomad/jobs/cocoindex" }}{{ with nomadVar "nomad/jobs/cocoindex" }}{{ with .ref }}DEPLOY_REF={{ . }}{{ end }}{{ end }}{{ end }}
        EOT
      }

      # Delimiters are set to something no file contains, so consul-template
      # passes the files through untouched.
      template {
        destination     = "local/driver.sh"
        perms           = "0755"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("driver.sh")
      }

      template {
        destination     = "local/runner.sh"
        perms           = "0755"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("runner.sh")
      }

      template {
        destination     = "local/s3snap.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("s3snap.py")
      }

      template {
        destination     = "local/commits_flow.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("commits_flow.py")
      }

      template {
        destination     = "local/prs_flow.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("prs_flow.py")
      }

      template {
        destination     = "local/ci_failures_flow.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("ci_failures_flow.py")
      }

      # What a published ci-failures.db may not contain (secrets, machine
      # names): s3snap.py runs it on the copy it uploads.
      template {
        destination     = "local/sanitize.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("sanitize.py")
      }
      # Public templates of runtime error messages, from the sources runner.sh
      # keeps (sanitize.py matches them at publish time).
      template {
        destination     = "local/srctemplates.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("srctemplates.py")
      }

      # The embeddinggemma task prefixes, shared by the flows and reader.py.
      template {
        destination     = "local/embedtext.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("embedtext.py")
      }

      # Gauges to MONIT (Grafana) through the monit-otlp route: passes, flows,
      # embedding requests, the ci-failures sync, publishes, embedder probes.
      template {
        destination     = "local/metrics.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("metrics.py")
      }

      # The log extraction shared with ci-mcp, and the error regexes it lifts
      # (by AST, never executed) out of report-pr-errors: one definition of
      # "what an error looks like" for the PR comment, ci-mcp and this index.
      template {
        destination     = "local/logscan.py"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("../ci-mcp/logscan.py")
      }

      template {
        destination     = "local/report-pr-errors"
        change_mode     = "noop"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("../report-pr-errors")
      }

      # Minimal client for the proxy's agent socket: "<route>\n" in, one JSON
      # line out. Usage: proxy-token.py <socket> <route> port|host|token
      template {
        destination = "local/proxy-token.py"
        perms       = "0755"
        data        = <<-EOP
        import json, socket, sys
        sock, route, want = sys.argv[1], sys.argv[2], sys.argv[3]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock)
        s.sendall((route + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        reply = json.loads(buf.decode() or "{}")
        if want not in reply:
            sys.exit("proxy gave no %s for route %r: %r" % (want, route, reply))
        sys.stdout.write(str(reply[want]))
        EOP
      }

      resources {
        cpu    = 2000
        memory = 4096
      }

      logs {
        max_files     = 3
        max_file_size = 50
      }
    }
  }
}
