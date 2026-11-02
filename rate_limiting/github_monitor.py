import requests, time
class GitHubRateLimitMonitor:
    def __init__(self, token=None):
        self.headers={"Accept":"application/vnd.github+json"}
        if token: self.headers["Authorization"]=f"Bearer {token}"
    def status(self):
        response=requests.get("https://api.github.com/rate_limit",headers=self.headers,timeout=15)
        response.raise_for_status()
        core=response.json().get("resources",{}).get("core",{})
        return {"limit":core.get("limit"),"remaining":core.get("remaining"),
                "reset":core.get("reset"),"observed_at":time.time()}
