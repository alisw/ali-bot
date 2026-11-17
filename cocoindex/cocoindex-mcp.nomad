## cocoindex-mcp: MCP server over the published cocoindex snapshots, for users.
## SKETCH -- not registered.
#
#   cd <this directory>                               # file() paths are relative to CWD
#   nomad job validate cocoindex-mcp.nomad
#   nomad-rw job run cocoindex-mcp.nomad              # register (human)
#
# A separate job from the indexer (cocoindex.nomad), on purpose:
#   * it serves only what the indexer PUBLISHED: it downloads latest.json's run
#     from S3 and opens it read-only, so it never sees a half-written pass, and
#     stopping, resetting or re-bootstrapping the indexer does not take it down;
#   * it is stateless apart from a cache it can re-download (~0.5 GB), so it can
#     run anywhere, with count > 1, and needs no sticky disk to be correct;
#   * its update cadence (a tool change) is unrelated to the indexer's.
#
# Clients: Claude Code / any MCP client, Streamable HTTP. Humans log in with CERN
# SSO (OAuth, PKCE; ALICE members only), automation uses a static bearer token:
#   claude mcp add --transport http --client-id ali-ci-mcp --callback-port <port> \
#       ali-ci https://ali-ci.cern.ch/mcp
#   claude mcp add --transport http ali-ci https://ali-ci.cern.ch/mcp \
#       --header "Authorization: Bearer <token>"
# See DESIGN.md, "Auth (HTTP)", for the Application Portal registration.
#
# Tokens: Vault kv/data/cocoindex-mcp, field "tokens" = JSON {"<name>": "<sha256 hex>"}.
# Only HASHES are stored; the human mints a token, hands it to the user, and
# adds its hash (see DESIGN.md, "Adding a user"). The template re-renders on a
# Vault change and the server re-reads the file, so no restart.
#
# Before registering (in addition to cocoindex.nomad's list):
#   * the nomad Vault role must be able to read kv/data/cocoindex-mcp;
#   * register the SSO client in the Application Portal and fill in the OIDC_*
#     DECISIONs below (client id = audience? role name?) from a real token;
#   * decide the public URL (DECISION below) and whether it is reachable from
#     outside CERN.

job "cocoindex-mcp" {
  datacenters = ["meyrin"]
  type        = "service"

  meta {
    redeploy = "2026-11-14-1"
  }

  update {
    max_parallel     = 1
    health_check     = "checks"
    min_healthy_time = "30s"
    healthy_deadline = "15m"
    # The first start downloads the snapshot; a new version only goes live
    # once /healthz answers, so readers never hit a replica with no data.
    progress_deadline = "30m"
    auto_revert       = true
  }

  constraint {
    attribute = "${attr.kernel.name}"
    value     = "linux"
  }

  constraint {
    attribute = "${attr.driver.docker}"
    value     = "1"
  }

  group "mcp" {
    count = 1

    restart {
      attempts = 3
      interval = "30m"
      delay    = "30s"
      mode     = "delay"
    }

    network {
      port "http" {}
    }

    ephemeral_disk {
      # The snapshot cache (commits.db ~0.5 GB; the old file
      # lingers while connections still read it). Sticky only so a restart
      # does not re-download; losing it costs one download, nothing else.
      size    = 3000
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
    # Same two routes as the indexer: aigw (here only for QUERY embeddings, one
    # short text per search) and s3 (reading snapshots). The s3 route is an
    # allowlist of the one bucket. The MCP clients never see either credential:
    # they hold an MCP bearer token, which reaches this server and nothing else.
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
            {"name": "s3", "upstream": "https://s3.cern.ch",
             "s3": {"buckets": {
                      "alice-cocoindex": {"access_key": {"ingest": "s3-access"},
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
          # DECISION: this job only READS the bucket. Best is a read-only keypair
          # (kv/data/cocoindex-reader); until one exists it can share the
          # indexer's. Same field names either way.
          python3 /local/vault-field.py aws_access_key_id "" kv/data/s3-services \
            | "$BIN/security-proxy-push" s3-access --socket "$SOCK" || return 1
          python3 /local/vault-field.py aws_secret_access_key "" kv/data/s3-services \
            | "$BIN/security-proxy-push" s3-secret --socket "$SOCK" || return 1
        }

        # Reprovision whenever the proxy rebinds its ingest socket (inode change),
        # exactly as the CI workers' bootstrap does.
        provisioned=""
        while :; do
          current=$(stat -c %i "$SOCK" 2>/dev/null || echo "")
          if [ -n "$current" ] && [ "$current" != "$provisioned" ] && push_all; then
            provisioned=$current
            echo "provisioned grid-cert, aigw-key and s3 keys"
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

    # ---- the MCP server -------------------------------------------------------
    task "mcp" {
      driver = "docker"

      kill_signal  = "SIGTERM"
      kill_timeout = "10s"

      vault {
        # Its own role (jwt-nomad/role/cocoindex), bound to the cocoindex and
        # cocoindex-mcp jobs, with policy "cocoindex": kv/data/cocoindex,
        # kv/data/cocoindex-mcp and kv/data/s3-services, read only. Not the
        # shared "nomad" role, which any job can use.
        role = "cocoindex"
      }

      config {
        # Same image (and digest) as the indexer, see cocoindex.nomad: its
        # python loads SQLite extensions (sqlite-vec), and its venv holds mcp,
        # pyjwt, sqlite-vec, boto3 and uvicorn from requirements-linux.lock.
        image        = "registry.cern.ch/alisw/cocoindex@sha256:2643b976055e724f1dac6a3b8fdf2dc1261f02f4e5103637a8a145683449399e"
        network_mode = "host"
        ports        = ["http"]
        entrypoint   = ["/bin/bash", "-c"]
        args = [<<-EOA
          set -e
          mkdir -p /alloc/data/mcp/home
          chown 1000:1000 /alloc/data/mcp /alloc/data/mcp/home
          exec setpriv --reuid=1000 --regid=1000 --groups=8485 -- /bin/bash /local/start.sh
        EOA
        ]
      }

      env {
        HOME       = "/alloc/data/mcp/home"
        COCO_CACHE = "/alloc/data/mcp/cache"
        # DECISION: bucket, as in cocoindex.nomad.
        COCO_STORE          = "s3://alice-cocoindex/cocoindex"
        SECURITY_PROXY_SOCK = "/alloc/security-proxy/agent/agent.sock"
        # Must be the model the indexer embedded with, or distances are noise.
        EMBED_MODEL        = "openai/embeddinggemma-300m"
        SYNC_INTERVAL      = "300"
        MCP_MAX_CONCURRENT = "4"
        # Query embeddings and sql() run in pools of their own, so a slow
        # embedder or heavy SQL never holds the MCP_MAX_CONCURRENT slots.
        MCP_MAX_EMBEDS      = "4"
        MCP_EMBED_TIMEOUT_S = "20"
        MCP_MAX_SQL         = "2"
        SQL_TIMEOUT_S      = "10"
        # Static tokens (automation). Remove for SSO only.
        MCP_TOKENS_FILE = "/secrets/mcp-tokens.json"
        # CERN SSO. Remove OIDC_ISSUER for tokens only.
        OIDC_ISSUER = "https://auth.cern.ch/auth/realms/cern"
        # The aud CERN puts in our access tokens: the Application Portal client
        # id, ali-ci-mcp (one application per ALICE MCP, so a token for another
        # one is refused here). VERIFY with a real token at deployment.
        OIDC_AUDIENCE = "ali-ci-mcp"
        # The portal role mapped to the e-group alice-member (claim cern_roles).
        OIDC_REQUIRED_ROLE = "alice-member"
        # Exactly the URL clients use: the RFC 9728 metadata's "resource".
        MCP_PUBLIC_URL = "https://ali-ci.cern.ch/mcp"
        # DECISION: the public host name, also in the Traefik rules below.
        MCP_ALLOWED_HOSTS = "ali-ci.cern.ch"
      }

      template {
        destination = "local/start.sh"
        perms       = "0755"
        change_mode = "noop"
        data        = <<-EOS
        #!/bin/bash
        set -euo pipefail
        exec /opt/cocoindex/venv/bin/python /local/mcp_server.py --http --host 0.0.0.0 --port "$NOMAD_PORT_http"
        EOS
      }

      template {
        destination     = "local/mcp_server.py"
        change_mode     = "restart"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("mcp_server.py")
      }

      template {
        destination     = "local/reader.py"
        change_mode     = "restart"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("reader.py")
      }

      # The embeddinggemma task prefixes, shared by the flows and reader.py.
      template {
        destination     = "local/embedtext.py"
        change_mode     = "restart"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("embedtext.py")
      }

      # Error-text normalisation for ci_signature (the same code the indexer
      # signs errors with). Its error patterns (report-pr-errors) are not needed
      # here and are not shipped.
      template {
        destination     = "local/logscan.py"
        change_mode     = "restart"
        left_delimiter  = "[[%%"
        right_delimiter = "%%]]"
        data            = file("../ci-mcp/logscan.py")
      }

      # Hashes only. noop: the server notices the new mtime on the next request.
      template {
        destination = "secrets/mcp-tokens.json"
        change_mode = "noop"
        # Owned by the server's uid on EVERY render, including a re-render after
        # a Vault change (a chown in the entrypoint would only cover the first).
        perms = "0400"
        uid   = 1000
        gid   = 1000
        data  = <<-EOT
        {{ with secret "kv/data/cocoindex-mcp" }}{{ .Data.data.tokens }}{{ end }}
        EOT
      }

      service {
        name = "cocoindex-mcp"
        port = "http"
        tags = [
          # https://ali-ci.cern.ch/mcp, passed through as it is: the server
          # answers on /mcp. Apache on the frontends serves ali-ci.cern.ch and
          # must list /mcp in public_paths (DESIGN.md, 8).
          "traefik.http.routers.cocoindex-mcp.rule=Host(`ali-ci.cern.ch`) && (Path(`/mcp`) || PathPrefix(`/mcp/`))",
          # RFC 9728 metadata for https://ali-ci.cern.ch/mcp lives at the host
          # root, outside /mcp: route that one path too.
          "traefik.http.routers.cocoindex-mcp-prm.rule=Host(`ali-ci.cern.ch`) && Path(`/.well-known/oauth-protected-resource/mcp`)",
        ]

        check {
          type     = "http"
          path     = "/healthz"
          port     = "http"
          interval = "30s"
          timeout  = "5s"
        }
      }

      resources {
        cpu    = 2000
        memory = 2048
      }

      logs {
        max_files     = 3
        max_file_size = 20
      }
    }
  }
}
