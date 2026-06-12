"""Thin Gitea REST client (stdlib urllib — no extra dependency).

Gitea speaks a GitHub-shaped API, so switching to real github.com later is a base-URL/token
change. Labels are created on demand and cached name->id per repo (Gitea attaches labels by id).
"""
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode


class GiteaError(RuntimeError):
    pass


class GiteaClient:
    def __init__(self, url: str, token: str, owner: str, *, git_user: str = "evo"):
        self._url = url.rstrip("/")
        self.base = self._url + "/api/v1"
        self.token = token
        self.owner = owner
        self.git_user = git_user
        self._label_cache: dict[str, dict[str, int]] = {}

    def git_remote(self, repo: str) -> str:
        """Token-authed clone/push URL for git over HTTP (basic auth user:token)."""
        scheme, rest = self._url.split("://", 1)
        return f"{scheme}://{self.git_user}:{self.token}@{rest}/{self.owner}/{repo}.git"

    # --- transport -------------------------------------------------------
    def _req(self, method: str, path: str, body=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        # Retry transient connection errors (Gitea restart/blip on a busy host). HTTPError (a real
        # API response like 404/409/405) is NOT retried — those are surfaced immediately.
        import time as _t
        last = None
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"token {self.token}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                raise GiteaError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from None
            except (urllib.error.URLError, OSError) as e:
                last = e
                _t.sleep(min(2 ** attempt, 20))
        raise GiteaError(f"{method} {path} -> connection failed after retries: {last}")

    def _paginate(self, path: str, params=None) -> list:
        """GET every page. Gitea caps page size at ~50 and silently ignores limit>50, so a single
        GET would truncate; loop pages until a short page is returned."""
        params = dict(params or {})
        params["limit"] = 50
        out, page = [], 1
        while True:
            params["page"] = page
            batch = self._req("GET", path, params=params) or []
            out.extend(batch)
            if len(batch) < 50:
                return out
            page += 1

    # --- repos -----------------------------------------------------------
    def ensure_repo(self, name: str, *, auto_init: bool = True) -> str:
        """Create the org repo if absent; return its name."""
        try:
            self._req("POST", f"/orgs/{self.owner}/repos",
                      {"name": name, "auto_init": auto_init, "private": False, "default_branch": "main"})
        except GiteaError as e:
            if "-> 409" not in str(e) and "already exists" not in str(e):
                raise
        return name

    # --- labels (created on demand, cached name->id) ---------------------
    def _fetch_labels(self, repo: str) -> dict:
        return {label["name"]: label["id"]
                for label in self._paginate(f"/repos/{self.owner}/{repo}/labels")}

    def ensure_label(self, repo: str, name: str, color: str = "#ededed") -> int:
        cache = self._label_cache.get(repo)
        if cache is None:
            cache = self._fetch_labels(repo)
            self._label_cache[repo] = cache
        if name not in cache:
            # Re-fetch before creating: per-role clients have separate caches, and Gitea does NOT
            # 409 on a duplicate label name — a blind POST would make a SECOND same-name label with
            # a different id, corrupting removal (add/remove by id, reads by name). The re-fetch
            # catches a label another client created since we seeded.
            cache = self._fetch_labels(repo)
            self._label_cache[repo] = cache
            if name not in cache:
                label = self._req("POST", f"/repos/{self.owner}/{repo}/labels", {"name": name, "color": color})
                cache[name] = label["id"]
        return cache[name]

    def _label_ids(self, repo: str, names) -> list[int]:
        return [self.ensure_label(repo, n) for n in (names or [])]

    # --- issues ----------------------------------------------------------
    def create_issue(self, repo: str, *, title: str, body: str = "", labels=None) -> int:
        iss = self._req("POST", f"/repos/{self.owner}/{repo}/issues",
                        {"title": title, "body": body, "labels": self._label_ids(repo, labels)})
        return iss["number"]

    def get_issue(self, repo: str, number: int) -> dict:
        return self._req("GET", f"/repos/{self.owner}/{repo}/issues/{number}")

    def list_issues(self, repo: str, *, labels=None, state: str = "open") -> list:
        params = {"state": state, "type": "issues"}
        if labels:
            params["labels"] = ",".join(labels)
        return self._paginate(f"/repos/{self.owner}/{repo}/issues", params)

    def issue_labels(self, repo: str, number: int) -> list[str]:
        return [label["name"] for label in (self.get_issue(repo, number).get("labels") or [])]

    def add_labels(self, repo: str, number: int, names) -> None:
        self._req("POST", f"/repos/{self.owner}/{repo}/issues/{number}/labels",
                  {"labels": self._label_ids(repo, names)})

    def remove_labels(self, repo: str, number: int, names) -> None:
        for lid in self._label_ids(repo, names):
            try:
                self._req("DELETE", f"/repos/{self.owner}/{repo}/issues/{number}/labels/{lid}")
            except GiteaError:
                pass

    # --- comments --------------------------------------------------------
    def comment(self, repo: str, number: int, body: str) -> dict:
        return self._req("POST", f"/repos/{self.owner}/{repo}/issues/{number}/comments", {"body": body})

    def comments(self, repo: str, number: int) -> list:
        return self._req("GET", f"/repos/{self.owner}/{repo}/issues/{number}/comments") or []

    # --- pull requests ---------------------------------------------------
    def create_pr(self, repo: str, *, head: str, base: str = "main", title: str, body: str = "", labels=None) -> int:
        pr = self._req("POST", f"/repos/{self.owner}/{repo}/pulls",
                       {"head": head, "base": base, "title": title, "body": body,
                        "labels": self._label_ids(repo, labels)})
        return pr["number"]

    def get_pr(self, repo: str, number: int) -> dict:
        return self._req("GET", f"/repos/{self.owner}/{repo}/pulls/{number}")

    def list_prs(self, repo: str, *, state: str = "open") -> list:
        return self._paginate(f"/repos/{self.owner}/{repo}/pulls", {"state": state})

    def merge_pr(self, repo: str, number: int, *, method: str = "merge") -> dict:
        return self._req("POST", f"/repos/{self.owner}/{repo}/pulls/{number}/merge", {"Do": method})

    # --- milestones / CI status -----------------------------------------
    def create_milestone(self, repo: str, title: str) -> dict:
        return self._req("POST", f"/repos/{self.owner}/{repo}/milestones", {"title": title})

    def set_commit_status(self, repo: str, sha: str, *, state: str, context: str, description: str = "") -> dict:
        return self._req("POST", f"/repos/{self.owner}/{repo}/statuses/{sha}",
                         {"state": state, "context": context, "description": description})

    def combined_status(self, repo: str, ref: str) -> str:
        data = self._req("GET", f"/repos/{self.owner}/{repo}/commits/{ref}/status")
        return (data or {}).get("state", "pending")
