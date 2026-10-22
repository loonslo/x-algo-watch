from copy import deepcopy
from urllib.parse import unquote, urlsplit

from x_algo_watch.github import BOT, GitHub


class FakeGitHub(GitHub):
    def __init__(self):
        super().__init__("owner/x-algo-watch", "test-token")
        self.rows = []
        self.comment_rows = {}
        self.labels = []
        self.calls = []

    def request(self, method, path, data=None):
        self.calls.append((method, path, deepcopy(data)))
        path = urlsplit(path).path
        if path == "/labels":
            if method == "GET":
                return self.labels.copy()
            self.labels.append(data)
            return data
        if path == "/issues":
            if method == "GET":
                return deepcopy(self.rows)
            row = {"number": len(self.rows) + 1, "state": "open", "user": {"login": BOT, "type": "Bot"}, **data}
            row["labels"] = [{"name": name} for name in data["labels"]]
            self.rows.append(row)
            return deepcopy(row)
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "issues":
            number = int(parts[1]); row = self.rows[number - 1]
            if len(parts) == 2:
                if method == "PATCH":
                    row.update(data)
                return deepcopy(row)
            if parts[2] == "labels":
                if method == "DELETE":
                    name = unquote(parts[3])
                    row["labels"] = [label for label in row["labels"] if label["name"] != name]
                    return deepcopy(row["labels"])
                row["labels"] += [{"name": name} for name in data["labels"]]
                return deepcopy(row["labels"])
            if parts[2] == "comments":
                comments = self.comment_rows.setdefault(number, [])
                if method == "GET":
                    return deepcopy(comments)
                comment = {"id": sum(map(len, self.comment_rows.values())) + 1, "user": {"login": BOT, "type": "Bot"}, **data}
                comments.append(comment)
                return deepcopy(comment)
        raise AssertionError((method, path, data))
