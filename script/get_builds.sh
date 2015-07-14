#!/bin/bash
curl -X POST http://elasticsearch.marathon.mesos:9200/_search/ -d'
{
  "size": 0,
  "aggs": {
    "2": {
      "terms": {
        "field": "data.buildNum",
        "size": 50,
        "order": {
          "2-orderAgg": "desc"
        }
      },
      "aggs": {
        "7": {
          "terms": {
            "field": "data.buildVariables.PACKAGE_NAME.raw",
            "size": 5,
            "order": {
              "_count": "desc"
            }
          },
          "aggs": {
            "6": {
              "terms": {
                "field": "data.buildVariables.ARCHITECTURE.raw",
                "size": 5,
                "order": {
                  "_count": "desc"
                }
              },
              "aggs": {
                "3": {
                  "terms": {
                    "field": "sources.tag",
                    "size": 50,
                    "order": {
                      "_count": "desc"
                    }
                  },
                  "aggs": {
                    "4": {
                      "terms": {
                        "field": "sources.hash",
                        "size": 5,
                        "order": {
                          "_count": "desc"
                        }
                      },
                      "aggs": {
                        "5": {
                          "terms": {
                            "field": "sources.repo.raw",
                            "size": 5,
                            "order": {
                              "_count": "desc"
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        },
        "2-orderAgg": {
          "avg": {
            "field": "data.buildNum"
          }
        }
      }
    }
  },
  "query": {
    "filtered": {
      "query": {
        "query_string": {
          "query": "*",
          "analyze_wildcard": true
        }
      },
      "filter": {
        "bool": {
          "must": [
            {
              "range": {
                "@timestamp": {
                  "gte": 1436194259883,
                  "lte": 1436799059883
                }
              }
            }
          ],
          "must_not": []
        }
      }
    }
  },
  "highlight": {
    "pre_tags": [
      "@kibana-highlighted-field@"
    ],
    "post_tags": [
      "@/kibana-highlighted-field@"
    ],
    "fields": {
      "*": {}
    },
    "fragment_size": 2147483647
  }
}
'
