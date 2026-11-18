# How fast does a CPU embed search queries? (2026-11-18)
#
# The MCP server sends every search's query to a remote embedding server
# (ALICE's pcapiserv12, or aigw) and waits ~5 s, mostly in their queues. Could
# it embed queries itself, on its node's CPU, as a last fallback? This job
# measures it on the node the MCP server runs on (alimetal01, AMD EPYC 7302,
# AVX2): llama-server, CPU only, the same GGUF as pcapiserv12
# (ggml-org embeddinggemma-300M Q8_0), with 4 and with 8 threads; queries of
# ~15 and ~200 tokens at 1, 4 and 16 at once, on CPU shares next to the CI builds; and the cosine of its vector to
# aigw's reference (the guard needs >= 0.999).
#
# Run from cocoindex/ (it reads embed-reference.json and the client script):
#
#   nomad-rw job run bench/embed-cpu-bench.nomad
#   nomad alloc logs -job embed-cpu-bench client | grep -E 'RESULT|cosine|up after|DONE'
#   nomad-rw job stop -purge embed-cpu-bench      # when read
#
# Takes a few minutes; downloads the 334 MB model from Hugging Face once per
# server task, and pulls the llama.cpp image from ghcr.io.

locals {
  # llama.cpp b12030 (2026-11-01), CPU build of llama-server.
  llama_image = "ghcr.io/ggml-org/llama.cpp@sha256:e67d88af487390098c76d7982b1761484339fa147b7e41d3c64ae1318759f78b"
  model_url   = "https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/main/embeddinggemma-300M-Q8_0.gguf"
  model_sum   = "sha256:b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63"
}

job "embed-cpu-bench" {
  datacenters = ["meyrin"]
  type        = "batch"

  constraint {
    attribute = "${node.unique.name}"
    value     = "alimetal01.cern.ch"
  }

  group "bench" {
    restart {
      attempts = 0
      mode     = "fail"
    }
    reschedule {
      attempts  = 0
      unlimited = false
    }

    network {
      port "t4" {}
      port "t8" {}
    }

    # ---- two CPU-only servers: 4 and 8 threads, 4 parallel slots each -------
    # CPU shares, not reserved cores: alimetal01's cores are all held by the CI
    # builders (2026-11-18), and the MCP server runs on shares too, next to
    # the builds. So this measures what it would see, load included.
    task "server-t4" {
      driver = "docker"
      lifecycle {
        hook    = "prestart"
        sidecar = true
      }
      artifact {
        source      = local.model_url
        destination = "local/model.gguf"
        mode        = "file"
        options {
          checksum = local.model_sum
        }
      }
      config {
        image        = local.llama_image
        network_mode = "host"
        ports        = ["t4"]
        args = ["-m", "/local/model.gguf", "--embeddings", "--host", "127.0.0.1",
                "--port", "${NOMAD_PORT_t4}", "-t", "4", "-tb", "4",
                "--parallel", "4", "-c", "4096", "-ub", "1024", "-b", "1024", "-ngl", "0"]
      }
      resources {
        cpu    = 6000
        memory = 1536
      }
    }

    task "server-t8" {
      driver = "docker"
      lifecycle {
        hook    = "prestart"
        sidecar = true
      }
      artifact {
        source      = local.model_url
        destination = "local/model.gguf"
        mode        = "file"
        options {
          checksum = local.model_sum
        }
      }
      config {
        image        = local.llama_image
        network_mode = "host"
        ports        = ["t8"]
        args = ["-m", "/local/model.gguf", "--embeddings", "--host", "127.0.0.1",
                "--port", "${NOMAD_PORT_t8}", "-t", "8", "-tb", "8",
                "--parallel", "4", "-c", "4096", "-ub", "1024", "-b", "1024", "-ngl", "0"]
      }
      resources {
        cpu    = 12000
        memory = 1536
      }
    }

    # ---- the client ---------------------------------------------------------
    task "client" {
      driver = "docker"
      template {
        data        = file("embed-reference.json")
        destination = "local/reference.json"
        left_delimiter  = "[[["
        right_delimiter = "]]]"
      }
      template {
        data            = file("bench/embed-cpu-bench.py")
        destination     = "local/bench.py"
        left_delimiter  = "[[["
        right_delimiter = "]]]"
      }
      config {
        # The security-proxy image, for its python3 (as cocoindex-mcp.nomad's
        # bootstrap uses it); the client needs only the standard library.
        image        = "registry.cern.ch/alisw/security-proxy@sha256:39464dfbd28952c43d17ea24fac9bc570be6a48195e71e637abefdacfab66f9c"
        network_mode = "host"
        entrypoint   = ["python3", "/local/bench.py", "/local/reference.json",
                        "cpu-4-threads=http://127.0.0.1:${NOMAD_PORT_t4}",
                        "cpu-8-threads=http://127.0.0.1:${NOMAD_PORT_t8}"]
      }
      resources {
        cpu    = 1000
        memory = 256
      }
    }
  }
}
