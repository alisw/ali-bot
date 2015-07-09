#!/usr/bin/env python
from elasticsearch import Elasticsearch
from elasticsearch_dsl import Search, Q
from pprint import pprint
from yaml import dump
from sys import argv

if __name__ == "__main__":
  client = Elasticsearch(["elasticsearch.marathon.mesos"])

  s = Search(using=client) \
          .query("match",
                 projectName = "build-any-ib") \
          .sort({"timestamp": {"order": "desc"} })

  response = s.execute()

  results = []
  for x in response:
    bv = x["data"]["buildVariables"]
    data = x["data"]
    obj = {
      "architecture": str(bv["ARCHITECTURE"]),
      "alibuild": str(bv["ALIBUILD_REPO"]),
      "alidist": str(bv["ALIDIST_REPO"]),
      "package": str(bv.get("PACKAGE_NAME", "")),
      "jenkins_build_nr": int(data["id"]),
      "result": str(data["result"]),
      "timestamp": str(data["timestamp"])
    }
    results.append(obj)
  print dump(results)
