from alibot_helpers.github_utilities import GithubCachedClient
import os

client = GithubCachedClient(api="https://api.github.com",
                            token=os.environ["GITHUB_TOKEN"])

client.loadCache(".test_cache")
print client.request("GET", "/rate_limit")
for x in client.request("GET", "/repos/alisw/AliPhysics/commits/31bd7c7/statuses?per_page=1"):
  print x["context"]
print client.request("GET", "/rate_limit")
print client.request("GET", "/rate_limit")
client.request("GET", "/teams/{team_id}/memberships/{user}", team_id=2293164, user="ktf")
client.request("GET", "/teams/{team_id}/memberships/{user}", team_id=2293164, user="ktf")
client.request("GET", "/teams/{team_id}/memberships/{user}", team_id=2293164, user="ktf")
client.request("GET", "/teams/{team_id}/memberships/{user}", team_id=2293164, user="ktf")
client.request("GET", "/teams/{team_id}/memberships/{user}", team_id=2293164, user="ktf")
client.request("GET", "/teams/{team_id}/memberships/{user}", team_id=2293164, user="ktf")
print client.request("GET", "/rate_limit")

print client.request("GET", "/rate_limit")
client.request("GET", "/repos/alisw/AliRoot/collaborators/ktf/permission", stable_api=False)
client.request("GET", "/repos/alisw/AliRoot/collaborators/bal/permission", stable_api=False)
client.request("GET", "/rate_limit")
client.request("GET", "/repos/alisw/AliRoot/collaborators/ktf/permission", stable_api=False)
client.request("GET", "/repos/alisw/AliRoot/collaborators/bal/permission", stable_api=False)
old_remaining = client.request("GET", "/rate_limit")["rate"]["remaining"]
client.dumpCache(".test_cache")

client2 = GithubCachedClient(api="https://api.github.com",
                             token=os.environ["GITHUB_TOKEN"])
client2.loadCache(".test_cache")
client2.request("GET", "/repos/alisw/AliRoot/collaborators/ktf/permission", stable_api=False)
client2.request("GET", "/repos/alisw/AliRoot/collaborators/bal/permission", stable_api=False)
new_remaining = client2.request("GET", "/rate_limit")["rate"]["remaining"]
print new_remaining, old_remaining

