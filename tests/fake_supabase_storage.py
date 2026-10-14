"""In-memory stand-in for the Supabase Storage REST API (httpx.MockTransport).

Implements the calls backend/document_storage.py makes -- bucket lookup/creation, object upload
(with x-upsert), download and prefix delete -- checks the service-role headers on every request,
and can be told to fail uploads, downloads or deletes so error paths can be tested.
"""

import json

import httpx

SERVICE_KEY = "test-service-role-key-not-real"
PROJECT_URL = "https://test-project.supabase.co"


class FakeSupabaseStorage:
    def __init__(self, bucket_public: bool = False, bucket_exists: bool = True):
        self.buckets: dict[str, dict] = {}
        self.objects: dict[tuple[str, str], bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_uploads = 0
        self.fail_downloads = 0
        self.fail_deletes = 0
        self.bucket_public = bucket_public
        self.bucket_exists = bucket_exists
        self.transport = httpx.MockTransport(self._handle)

    def env(self) -> dict:
        return {"SUPABASE_URL": PROJECT_URL, "SUPABASE_SERVICE_ROLE_KEY": SERVICE_KEY}

    def count(self, method: str, prefix: str = "/storage/v1/object/") -> int:
        return sum(1 for verb, path in self.requests if verb == method and path.startswith(prefix))

    def object_keys(self, bucket: str) -> set[str]:
        return {key for name, key in self.objects if name == bucket}

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        authorization = request.headers.get("authorization")
        if request.headers.get("apikey") != SERVICE_KEY or authorization not in (None, f"Bearer {SERVICE_KEY}"):
            return httpx.Response(401, json={"error": "unauthorized"})
        if not path.startswith("/storage/v1/"):
            return httpx.Response(404)
        route = path[len("/storage/v1/"):]
        if route.startswith("bucket"):
            return self._bucket(request, route)
        if route.startswith("object/"):
            return self._object(request, route[len("object/"):])
        return httpx.Response(404)

    def _bucket(self, request, route):
        if request.method == "POST" and route == "bucket":
            body = json.loads(request.content)
            self.buckets[body["id"]] = {"id": body["id"], "name": body["name"], "public": bool(body.get("public"))}
            return httpx.Response(200, json={"name": body["name"]})
        name = route.split("/", 1)[1] if "/" in route else ""
        if request.method == "GET":
            if name not in self.buckets and self.bucket_exists:
                self.buckets[name] = {"id": name, "name": name, "public": self.bucket_public}
            if name not in self.buckets:
                return httpx.Response(400, json={"statusCode": "404", "error": "Bucket not found"})
            return httpx.Response(200, json=self.buckets[name])
        return httpx.Response(405)

    def _object(self, request, route):
        if request.method == "DELETE" and "/" not in route:
            if self.fail_deletes:
                self.fail_deletes -= 1
                return httpx.Response(500, json={"error": "internal"})
            for key in json.loads(request.content).get("prefixes", []):
                self.objects.pop((route, key), None)
            return httpx.Response(200, json=[])
        bucket, key = route.split("/", 1)
        if request.method == "POST":
            if self.fail_uploads:
                self.fail_uploads -= 1
                return httpx.Response(500, json={"error": "internal"})
            if (bucket, key) in self.objects and request.headers.get("x-upsert") != "true":
                return httpx.Response(400, json={"error": "Duplicate"})
            self.objects[(bucket, key)] = request.content
            return httpx.Response(200, json={"Key": f"{bucket}/{key}"})
        if request.method == "GET":
            if self.fail_downloads:
                self.fail_downloads -= 1
                return httpx.Response(500, json={"error": "internal"})
            if (bucket, key) not in self.objects:
                return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
            return httpx.Response(200, content=self.objects[(bucket, key)])
        return httpx.Response(405)
